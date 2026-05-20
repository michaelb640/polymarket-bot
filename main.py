#!/usr/bin/env python3
"""Polymarket BTC 5-minute market trading bot."""

import argparse
import os
import signal
import time
import sys
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")
_WEEKEND_DAYS = frozenset({4, 5, 6})  # Fri=4, Sat=5, Sun=6 — no new entries

import config
import database
import price_feed
import polymarket
import strategy
import risk
import arb_monitor
from logger import logger

# ---------------------------------------------------------------------------
# Single-instance guard via PID file
# ---------------------------------------------------------------------------

_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.pid")


def _acquire_single_instance() -> None:
    """Kill any previously running bot instance, then write our PID."""
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(1)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                logger.info(f"Killed stale bot process pid={old_pid}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    def _cleanup(signum=None, frame=None):
        try:
            os.remove(_PID_FILE)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

# ---------------------------------------------------------------------------
# 5-minute window tracking
# ---------------------------------------------------------------------------

# Maps market_id -> {"ts": float, "btc_price": float | None}
_known_markets: dict[str, dict] = {}

# Pending maker orders not yet filled: market_id -> order context dict
# Positions are only written to DB after a fill is confirmed.
_pending_orders: dict[str, dict] = {}


def _is_new_market(market: dict, now: float, btc_price: float | None) -> bool:
    mid = market["condition_id"]
    if mid not in _known_markets:
        _known_markets[mid] = {"ts": now, "btc_price": btc_price}
        logger.info(f"New market window: {mid} opening_price={btc_price}")
        return True
    return False


def _seconds_since_market_opened(market: dict, now: float) -> float:
    window_start = market.get("window_start_ts") or _known_markets.get(market["condition_id"], {}).get("ts", now)
    return now - window_start


def _get_opening_price(market: dict) -> float | None:
    return _known_markets.get(market["condition_id"], {}).get("btc_price")


# ---------------------------------------------------------------------------
# P&L / resolution tracking
# ---------------------------------------------------------------------------

def _derive_window_ts(entry_time_str: str) -> int | None:
    try:
        dt = datetime.fromisoformat(entry_time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (int(dt.timestamp()) // 300) * 300
    except Exception:
        return None


def _fix_push_positions() -> None:
    """Every loop: retroactively resolve any positions still sitting at exit_price=0.5."""
    push_positions = database.get_push_positions()
    for pos in push_positions:
        wts = pos.get("window_start_ts") or _derive_window_ts(pos.get("entry_time", ""))
        if wts is None:
            continue
        resolution = polymarket.get_market_resolution(pos["market_id"], pos["side"], wts)
        if resolution is not None:
            pnl = strategy.compute_pnl(pos, resolution)
            database.update_position_closed(pos["id"], resolution, pnl)
            logger.info(
                f"Push fixed: id={pos['id']} side={pos['side']} resolution={resolution} pnl=${pnl:.2f}"
            )


def _check_resolutions(open_positions: list[dict]) -> None:
    """
    Check if any open positions have resolved.
    Uses window_start_ts for accurate timing when available (market ends at window_start + 300s).
    Falls back to entry_time + 5 min heuristic for legacy rows.
    """
    now = time.time()
    for pos in open_positions:
        window_start_ts = pos.get("window_start_ts")
        if window_start_ts:
            window_end = window_start_ts + 300
            if now < window_end + 30:  # wait 30s after market close for settlement
                continue
            # Force-close 5 min after market end if Gamma API still silent
            force_at = window_end + 330
        else:
            entry_ts = _parse_ts(pos["entry_time"])
            age_seconds = now - entry_ts
            if age_seconds < 300:
                continue
            force_at = _parse_ts(pos["entry_time"]) + 540

        resolution = polymarket.get_market_resolution(
            pos["market_id"], pos["side"], window_start_ts
        )

        if resolution is None:
            if now >= force_at:
                logger.warning(
                    f"Resolution still unavailable after deadline for market={pos['market_id']} — will keep retrying"
                )
            continue

        pnl = strategy.compute_pnl(pos, resolution)
        won = pnl > 0
        logger.info(
            f"Position resolved: market={pos['market_id']} side={pos['side']} "
            f"resolution={resolution} pnl=${pnl:.4f}"
        )
        database.update_position_closed(pos["id"], resolution, pnl)
        risk.record_trade_result(won)


def _parse_ts(iso_str: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


# ---------------------------------------------------------------------------
# Maker order management
# ---------------------------------------------------------------------------

def _extract_order_id(resp: dict) -> str | None:
    """Pull order ID from a CLOB response regardless of key name used."""
    return (resp.get("orderID") or resp.get("order_id")
            or resp.get("id") or resp.get("orderId"))


def _manage_pending_orders(current_signal: str, daily_trades: int) -> int:
    """
    Called every loop: checks fill/cancel status for all pending maker orders.
    - Filled (MATCHED): record position in DB, increment daily_trades.
    - Window expired or signal reversed (SKIP / opposite): cancel the order.
    - WEEKEND signal does NOT count as a reversal — don't cancel existing pending orders.
    Returns updated daily_trades.
    """
    if not _pending_orders:
        return daily_trades

    now = time.time()
    to_remove: list[str] = []

    for market_id, order in list(_pending_orders.items()):
        order_id = order["order_id"]
        window_start_ts = order.get("window_start_ts")

        # Entry window expired → cancel
        if window_start_ts and (now - window_start_ts) > config.ENTRY_WINDOW_SECONDS:
            logger.info(
                f"Maker order window expired ({now - window_start_ts:.0f}s > {config.ENTRY_WINDOW_SECONDS}s) "
                f"— cancelling {order_id[:16]}"
            )
            polymarket.cancel_order(order_id)
            to_remove.append(market_id)
            continue

        # Signal reversed: SKIP or opposite direction (WEEKEND is not a reversal)
        order_signal = order["signal"]
        if current_signal == "SKIP" or (
            current_signal in ("UP", "DOWN") and current_signal != order_signal
        ):
            logger.info(
                f"Signal reversed ({order_signal}→{current_signal}) "
                f"— cancelling maker order {order_id[:16]}"
            )
            polymarket.cancel_order(order_id)
            to_remove.append(market_id)
            continue

        # Check fill status
        status = polymarket.get_order_status(order_id)
        if status == "MATCHED":
            logger.info(
                f"Maker order FILLED: market={market_id} side={order['side']} "
                f"price={order['entry_price']:.4f} size={order['size']:.2f}sh"
            )
            database.insert_position(
                market_id, order["side"], order["entry_price"], order["size"],
                order.get("btc_price"), order.get("market_name"), window_start_ts,
            )
            daily_trades += 1
            to_remove.append(market_id)
        elif status == "CANCELLED":
            logger.info(f"Maker order cancelled externally: {order_id[:16]}")
            to_remove.append(market_id)
        else:
            logger.debug(f"Maker order still pending: {order_id[:16]} status={status}")

    for mid in to_remove:
        _pending_orders.pop(mid, None)

    return daily_trades


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status_table(
    btc_price: float | None,
    open_positions: list[dict],
    daily_pnl: float,
    next_poll: datetime,
    signal: str,
    daily_trades: int,
    hourly_trend: str | None = None,
    pending_orders: int = 0,
) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    price_str = f"${btc_price:,.2f}" if btc_price else "N/A"

    lines = [
        "",
        "=" * 62,
        f"  Polymarket BTC 5-Min Bot  |  {now_str}",
        "=" * 62,
        f"  BTC Price         : {price_str}",
        f"  Hourly Trend      : {hourly_trend or 'FLAT'}",
        f"  Last Signal       : {signal}",
        f"  Open Positions    : {len(open_positions)}",
        f"  Pending Orders    : {pending_orders}",
        f"  Daily P&L         : ${daily_pnl:.2f}",
        f"  Trades Today      : {daily_trades}/{config.MAX_DAILY_TRADES}",
        f"  Next Poll         : {next_poll.strftime('%H:%M:%S UTC')}",
        "=" * 62,
    ]
    if open_positions:
        lines.append(f"  {'Market':<35} {'Side':<5} {'Entry':<7} {'Size':<6} {'Age'}")
        lines.append(f"  {'-'*35} {'-'*5} {'-'*7} {'-'*6} {'-'*8}")
        for p in open_positions:
            age = int((time.time() - _parse_ts(p["entry_time"])))
            lines.append(
                f"  {p['market_id'][:33]:<35} {p['side']:<5} "
                f"{p['entry_price']:<7.4f} {p['size']:<6.2f} {age}s"
            )
    lines.append("")
    print("\n".join(lines), flush=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_bot() -> None:
    _acquire_single_instance()
    logger.info("Starting Polymarket BTC 5-Min Bot" + (" [DRY RUN]" if config.DRY_RUN else " [LIVE]"))
    database.init_db()
    polymarket.cancel_all_open_orders()
    price_feed.start_price_sampler()
    arb_monitor.start_arb_monitor()

    # Wait for the buffer to fill enough to generate signals
    logger.info("Warming up price buffer (need 12 samples = ~2 minutes)...")
    while len(price_feed.get_price_buffer()) < 12:
        time.sleep(10)
    logger.info("Buffer ready.")

    last_day = datetime.now(_PACIFIC).date()
    last_signal = "SKIP"
    daily_trades = 0

    while True:
        loop_start = time.time()

        try:
            today = datetime.now(_PACIFIC).date()
            if today != last_day:
                logger.info("New day — writing summary and resetting counters.")
                _write_daily_summary()
                risk.reset_daily_counters()
                daily_trades = 0
                last_day = today

            btc_price, daily_vol = price_feed.get_btc_data()
            prices = price_feed.get_price_buffer()
            hourly_trend = price_feed.get_hourly_trend()

            # Check resolutions for any open positions
            open_positions = database.get_open_positions()
            _check_resolutions(open_positions)

            # Retroactively fix any positions still showing 0.5 PUSH
            _fix_push_positions()

            # Re-fetch after resolution updates
            open_positions = database.get_open_positions()

            # Generate base signal (without opening price — used as a fast pre-filter)
            last_signal, _ = strategy.generate_signal(prices, hourly_trend=hourly_trend, realized_vol=daily_vol)

            # Weekend block: no new entries Fri/Sat/Sun (Pacific time)
            if today.weekday() in _WEEKEND_DAYS:
                last_signal = "WEEKEND"

            # Manage pending maker orders (check fills, cancel on reversal/expiry)
            daily_trades = _manage_pending_orders(last_signal, daily_trades)

            # Entry logic
            if last_signal not in ("SKIP", "WEEKEND"):
                active_markets = polymarket.get_active_btc_markets()
                now = time.time()

                # Prune stale market entries (older than 10 min) to prevent unbounded growth
                for stale in [k for k, v in _known_markets.items() if now - v.get("ts", 0) > 600]:
                    del _known_markets[stale]

                for market in active_markets:
                    mid = market["condition_id"]

                    # Record opening price on first sight of this window
                    is_new = _is_new_market(market, now, btc_price)
                    age = _seconds_since_market_opened(market, now)
                    if age > config.ENTRY_WINDOW_SECONDS:
                        if is_new:
                            logger.debug(f"Market {mid} already {age:.0f}s old — skipping entry window")
                        continue

                    # Treat pending maker orders like open positions for MAX_OPEN_POSITIONS gate
                    if _pending_orders:
                        continue

                    if not risk.can_open_position(mid):
                        continue

                    # Re-score with opening price + hourly trend veto + vol context
                    opening_price = _get_opening_price(market)
                    signal, score = strategy.generate_signal(prices, opening_price, hourly_trend, realized_vol=daily_vol)
                    if signal == "SKIP":
                        logger.debug(f"Signal filtered to SKIP after opening price/trend context for {mid}")
                        continue

                    side = strategy.get_entry_side(signal, market)
                    if side is None:
                        continue

                    token = polymarket.get_token_for_signal(market, signal)
                    if token is None:
                        logger.debug(f"No token found for signal={signal} in market {mid}")
                        continue

                    token_id = token.get("token_id", mid)

                    # Need both sides to calculate mid price; also check spread quality
                    clob_bid, clob_ask = polymarket.get_token_spread(token_id)
                    logger.debug(f"Token: bid={clob_bid} ask={clob_ask} gamma={token.get('price')}")

                    if clob_bid is None or clob_ask is None or clob_bid >= clob_ask:
                        logger.debug(f"Incomplete orderbook for {mid[:16]}... — skipping")
                        continue

                    spread = clob_ask - clob_bid
                    if spread > config.MAX_SPREAD:
                        logger.debug(f"Spread {spread:.4f} > MAX_SPREAD {config.MAX_SPREAD} — skipping {mid}")
                        continue

                    if config.USE_MAKER_ORDERS:
                        entry_price = round((clob_bid + clob_ask) / 2, 2)
                    else:
                        entry_price = clob_ask

                    if entry_price < config.MIN_ENTRY_PRICE:
                        logger.debug(f"Entry price {entry_price:.4f} < MIN_ENTRY_PRICE {config.MIN_ENTRY_PRICE} — skipping")
                        continue

                    if entry_price > config.MAX_ENTRY_PRICE:
                        logger.debug(f"Entry price {entry_price:.4f} > MAX_ENTRY_PRICE {config.MAX_ENTRY_PRICE} — skipping")
                        continue

                    # Risk-based sizing: target risk_pct of balance as max loss per trade.
                    # position_size (shares) = risk_dollars / entry_price so cost = risk_dollars.
                    score_risk = {2: 0.03, 3: 0.05, 4: 0.08}
                    balance = database.get_account_balance(config.STARTING_BALANCE)
                    risk_dollars = balance * score_risk.get(score, 0.03)
                    position_size = round(max(2.0, risk_dollars / entry_price), 2)

                    order = polymarket.place_order(mid, token_id, side, position_size, entry_price)
                    if order:
                        if config.USE_MAKER_ORDERS:
                            # Maker: track pending — only record in DB after fill confirmed
                            order_id = _extract_order_id(order) or f"mock_{int(time.time())}"
                            _pending_orders[mid] = {
                                "order_id": order_id,
                                "signal": signal,
                                "side": side,
                                "token_id": token_id,
                                "size": position_size,
                                "entry_price": entry_price,
                                "btc_price": btc_price,
                                "market_name": market.get("question"),
                                "window_start_ts": market.get("window_start_ts"),
                            }
                            logger.info(
                                f"Maker order posted: market={mid} side={side} price={entry_price:.4f} "
                                f"size={position_size:.2f}sh (${risk_dollars:.2f} at risk) "
                                f"score={score} order={order_id[:16]}"
                            )
                        else:
                            # Taker: record immediately (legacy behaviour)
                            database.insert_position(mid, side, entry_price, position_size,
                                                     btc_price, market.get("question"),
                                                     window_start_ts=market.get("window_start_ts"))
                            daily_trades += 1
                            logger.info(
                                f"Entered (taker): market={mid} side={side} price={entry_price:.4f} "
                                f"size={position_size:.2f}sh (${risk_dollars:.2f} at risk) "
                                f"score={score} balance=${balance:.2f}"
                            )

            open_positions = database.get_open_positions()
            daily_pnl = database.get_daily_pnl()
            next_poll = datetime.fromtimestamp(
                loop_start + config.POLL_INTERVAL_SECONDS, tz=timezone.utc
            )
            print_status_table(
                btc_price, open_positions, daily_pnl, next_poll,
                last_signal, daily_trades, hourly_trend, len(_pending_orders)
            )
            logger.debug("Heartbeat.")

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0, config.POLL_INTERVAL_SECONDS - elapsed))


def _write_daily_summary() -> None:
    closed = database.get_closed_positions_today()
    total = len(closed)
    winners = sum(1 for p in closed if (p["pnl"] or 0) > 0)
    losers = total - winners
    gross_pnl = sum(p["pnl"] or 0 for p in closed)
    database.upsert_daily_summary(total, winners, losers, gross_pnl, fees_paid=0.0)
    logger.info(f"Daily summary: trades={total} wins={winners} pnl=${gross_pnl:.2f}")


# ---------------------------------------------------------------------------
# Backtest (signal replay on historical 5m candles)
# ---------------------------------------------------------------------------

def run_backtest() -> None:
    """
    Simulate the 5-minute signal strategy on 7 days of Binance 5m candles.

    Key improvements over the old version:
    - OHLC-based intra-candle paths (no straight-line lookahead bias)
    - Signal generated from PREVIOUS candle's buffer, not the current one
    - Hourly trend filter applied (was missing before)
    - Taker fee (1.56%) and 2c slippage simulated
    - Win rate broken out by score — use this to calibrate P_WIN_SCORE_* in config
    """
    import requests
    import math as _math

    KLINES_URL = "https://api.binance.us/api/v3/klines"
    FEE_RATE = 0.0156   # Polymarket taker fee
    SLIPPAGE = 0.02     # simulated bid-ask half-spread on entry
    SHARES = 20.0       # shares per trade (~$10 at 0.50 mid)

    logger.info("Starting backtest (7 days, 5-min BTC candles)...")

    try:
        resp = requests.get(KLINES_URL, params={"symbol": "BTCUSD", "interval": "5m", "limit": 2016}, timeout=15)
        resp.raise_for_status()
        klines = resp.json()
    except Exception as e:
        logger.error(f"Backtest: failed to fetch 5m candles: {e}")
        return

    try:
        resp = requests.get(KLINES_URL, params={"symbol": "BTCUSD", "interval": "1h", "limit": 200}, timeout=15)
        resp.raise_for_status()
        hourly_map = {int(k[0]) // 1000: float(k[4]) for k in resp.json()}
    except Exception as e:
        logger.warning(f"Backtest: hourly candles unavailable ({e}); trend filter disabled")
        hourly_map = {}

    def _hourly_trend(ts: int) -> str | None:
        hour = (ts // 3600) * 3600
        closes = [hourly_map.get(hour - i * 3600) for i in range(3, -1, -1)]
        closes = [c for c in closes if c is not None]
        if len(closes) < 4:
            return None
        pct = (closes[-1] - closes[0]) / closes[0]
        if pct > 0.001: return "UP"
        if pct < -0.001: return "DOWN"
        return None

    def _build_ohlc_path(o: float, h: float, l: float, c: float) -> list[float]:
        # Bullish candle: dip before rally (O→L→H→C); bearish: spike before drop (O→H→L→C)
        waypoints = [o, l, h, c] if c >= o else [o, h, l, c]
        path = []
        for i in range(12):
            t = i / 11.0 * 3          # maps 0..11 → 0..3 across 4 waypoints
            idx = min(int(t), 2)
            frac = t - idx
            path.append(waypoints[idx] + frac * (waypoints[idx + 1] - waypoints[idx]))
        return path

    trades: list[dict] = []
    consecutive_losses = 0
    daily_trades = 0
    daily_pnl = 0.0
    last_day: str | None = None
    price_history: list[float] = []
    prev_candle_path: list[float] | None = None

    for candle in klines:
        ts      = int(candle[0]) // 1000
        open_p  = float(candle[1])
        high_p  = float(candle[2])
        low_p   = float(candle[3])
        close_p = float(candle[4])

        day_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if day_str != last_day:
            daily_trades = 0
            daily_pnl = 0.0
            consecutive_losses = 0
            last_day = day_str

        current_path = _build_ohlc_path(open_p, high_p, low_p, close_p)

        # Signal uses the PREVIOUS candle's price buffer to predict THIS candle.
        # This mirrors live trading (signal fires in the first 90s of a new window
        # using data from the prior window), eliminating lookahead.
        if prev_candle_path is not None:
            price_history.extend(prev_candle_path)
            if len(price_history) > 30:
                price_history = price_history[-30:]

            if (daily_trades < config.MAX_DAILY_TRADES
                    and consecutive_losses < config.MAX_CONSECUTIVE_LOSSES
                    and len(price_history) >= 12):

                balance = config.STARTING_BALANCE + sum(t["pnl"] for t in trades)
                if not (daily_pnl < 0 and abs(daily_pnl) >= balance * config.DAILY_LOSS_LIMIT_PCT):

                    # Approximate realized vol from rolling price history
                    rets = [_math.log(price_history[i] / price_history[i - 1])
                            for i in range(1, len(price_history))]
                    mean_r = sum(rets) / len(rets)
                    var = sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)
                    realized_vol = max(0.005, min(0.10, _math.sqrt(var) * _math.sqrt(8640)))

                    hourly_trend = _hourly_trend(ts)

                    # opening_price = start of this window (price to beat)
                    signal, score = strategy.generate_signal(
                        price_history, opening_price=open_p,
                        hourly_trend=hourly_trend, realized_vol=realized_vol
                    )

                    if signal != "SKIP":
                        entry_price = 0.50 + SLIPPAGE
                        resolution = 1.0 if close_p >= open_p else 0.0
                        side = "YES" if signal == "UP" else "NO"
                        pos = {"side": side, "entry_price": entry_price, "size": SHARES}
                        raw_pnl = strategy.compute_pnl(pos, resolution)
                        fee = SHARES * entry_price * FEE_RATE
                        pnl = raw_pnl - fee
                        won = pnl > 0

                        consecutive_losses = 0 if won else consecutive_losses + 1
                        daily_trades += 1
                        daily_pnl += pnl
                        trades.append({
                            "pnl": pnl, "raw_pnl": raw_pnl, "fee": fee,
                            "signal": signal, "score": score, "won": won, "day": day_str,
                        })

        prev_candle_path = current_path

    if not trades:
        print("\nBacktest: no trades generated.")
        return

    total = len(trades)
    winners = [t for t in trades if t["won"]]
    gross_pnl = sum(t["pnl"] for t in trades)
    total_fees = sum(t["fee"] for t in trades)
    win_rate = len(winners) / total * 100
    breakeven_wr = (0.50 + SLIPPAGE) * 100  # entry_price = breakeven win rate for binary bets

    print("\n" + "=" * 64)
    print("  BACKTEST RESULTS — BTC 5-min  (7 days, fees + slippage)")
    print("=" * 64)
    print(f"  Total trades    : {total}")
    print(f"  Win rate        : {win_rate:.1f}%  (breakeven ≈ {breakeven_wr:.1f}%)")
    print(f"  Net P&L         : ${gross_pnl:.2f}")
    print(f"  Total fees paid : ${total_fees:.2f}")
    print()
    print("  Score breakdown — use to calibrate P_WIN_SCORE_* in config:")
    for s in (2, 3, 4):
        st = [t for t in trades if t["score"] == s]
        if st:
            sw = sum(1 for t in st if t["won"])
            print(f"    Score {s}: {len(st):3d} trades   win={sw/len(st)*100:.1f}%   pnl=${sum(t['pnl'] for t in st):.2f}")
    print("=" * 64 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Minute Trading Bot")
    parser.add_argument("--backtest", action="store_true", help="Replay 7 days of 5m candles.")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    else:
        run_bot()


if __name__ == "__main__":
    main()

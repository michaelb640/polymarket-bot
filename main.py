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

import config
import database
import price_feed
import polymarket
import strategy
import risk
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

        if True:
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
    price_feed.start_price_sampler()

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

            btc_price, _ = price_feed.get_btc_data()
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
            last_signal, _ = strategy.generate_signal(prices, hourly_trend=hourly_trend)

            # Entry logic
            if last_signal != "SKIP":
                active_markets = polymarket.get_active_btc_markets()
                now = time.time()

                for market in active_markets:
                    mid = market["condition_id"]

                    # Record opening price on first sight of this window
                    is_new = _is_new_market(market, now, btc_price)
                    age = _seconds_since_market_opened(market, now)
                    if age > config.ENTRY_WINDOW_SECONDS:
                        if is_new:
                            logger.debug(f"Market {mid} already {age:.0f}s old — skipping entry window")
                        continue

                    if not risk.can_open_position(mid):
                        continue

                    # Re-score with opening price + hourly trend veto
                    opening_price = _get_opening_price(market)
                    signal, score = strategy.generate_signal(prices, opening_price, hourly_trend)
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

                    # Position size: 5%/10%/15% of account balance for score 2/3/4
                    score_pct = {2: 0.05, 3: 0.10, 4: 0.15}
                    balance = database.get_account_balance(config.STARTING_BALANCE)
                    position_size = round(max(2.0, balance * score_pct.get(score, 0.05)), 2)

                    # Prefer live CLOB best-ask over stale Gamma API price
                    clob_ask = polymarket.get_token_best_ask(token_id)
                    entry_price = clob_ask if clob_ask is not None else float(token.get("price", 0.5))
                    logger.debug(f"Token price: clob_ask={clob_ask} gamma={token.get('price')} using={entry_price:.4f}")

                    if entry_price > config.MAX_ENTRY_PRICE:
                        logger.debug(
                            f"Token price {entry_price:.4f} > MAX_ENTRY_PRICE {config.MAX_ENTRY_PRICE} — skipping"
                        )
                        continue

                    if config.CONVICTION_SKIP_LOW <= entry_price <= config.CONVICTION_SKIP_HIGH:
                        logger.debug(
                            f"Token price {entry_price:.4f} in no-conviction zone "
                            f"({config.CONVICTION_SKIP_LOW}-{config.CONVICTION_SKIP_HIGH}) — skipping"
                        )
                        continue
                    order = polymarket.place_order(mid, token_id, side, position_size, entry_price)
                    if order:
                        database.insert_position(mid, side, entry_price, position_size,
                                                 btc_price, market.get("question"),
                                                 window_start_ts=market.get("window_start_ts"))
                        daily_trades += 1
                        logger.info(
                            f"Entered: market={mid} side={side} price={entry_price:.4f} "
                            f"size=${position_size} score={score} balance=${balance:.2f} opening_price={opening_price}"
                        )

            open_positions = database.get_open_positions()
            daily_pnl = database.get_daily_pnl()
            next_poll = datetime.fromtimestamp(
                loop_start + config.POLL_INTERVAL_SECONDS, tz=timezone.utc
            )
            print_status_table(btc_price, open_positions, daily_pnl, next_poll, last_signal, daily_trades, hourly_trend)
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
    Each candle is treated as one 5-minute market cycle.
    Within each cycle we build a 12-sample price sub-series from the OHLC data
    to approximate the intra-candle price path, then run generate_signal().
    """
    logger.info("Starting backtest (7 days, 5-min BTC candles)...")

    import requests
    import numpy as np

    KLINES_URL = "https://api.binance.us/api/v3/klines"
    try:
        params = {"symbol": "BTCUSD", "interval": "5m", "limit": 2016}
        resp = requests.get(KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        klines = resp.json()
    except Exception as e:
        logger.error(f"Backtest: failed to fetch candles: {e}")
        return

    trades = []
    consecutive_losses = 0
    daily_trades = 0
    last_day = None
    price_history: list[float] = []

    for candle in klines:
        open_p  = float(candle[1])
        high_p  = float(candle[2])
        low_p   = float(candle[3])
        close_p = float(candle[4])
        ts_ms   = int(candle[0])

        day_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day_str != last_day:
            daily_trades = 0
            consecutive_losses = 0
            last_day = day_str

        # Build a 12-point intra-candle path: open → high/low midpoint → close
        # (crude but deterministic approximation of 10s samples)
        mid_path = [
            open_p + (close_p - open_p) * i / 11
            for i in range(12)
        ]
        price_history.extend(mid_path)
        if len(price_history) > 30:
            price_history = price_history[-30:]

        if daily_trades >= config.MAX_DAILY_TRADES:
            continue
        if consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            continue

        signal, _ = strategy.generate_signal(price_history)
        if signal == "SKIP":
            continue

        entry_price = 0.50
        if not (config.MIN_MARKET_PRICE <= entry_price <= config.MAX_MARKET_PRICE):
            continue

        # Resolution: did price go UP or DOWN?
        resolution = 1.0 if close_p >= open_p else 0.0
        side = "YES" if signal == "UP" else "NO"
        pos = {"side": side, "entry_price": entry_price, "size": config.POSITION_SIZE}
        pnl = strategy.compute_pnl(pos, resolution)
        won = pnl > 0

        if won:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        daily_trades += 1
        trades.append({"side": side, "pnl": pnl, "signal": signal, "day": day_str})

    if not trades:
        print("\nBacktest: no trades generated.")
        return

    total = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    gross_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(winners) / total * 100
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

    by_signal: dict[str, int] = {}
    for t in trades:
        by_signal[t["signal"]] = by_signal.get(t["signal"], 0) + 1

    print("\n" + "=" * 58)
    print("  BACKTEST RESULTS — BTC 5-min markets (7 days)")
    print("=" * 58)
    print(f"  Total trades      : {total}")
    print(f"  Winners           : {len(winners)} ({win_rate:.1f}%)")
    print(f"  Losers            : {len(losers)}")
    print(f"  Gross P&L         : ${gross_pnl:.2f}")
    print(f"  Avg win           : ${avg_win:.2f}")
    print(f"  Avg loss          : ${avg_loss:.2f}")
    for sig, count in by_signal.items():
        print(f"  Signal [{sig}]       : {count} trades")
    print("=" * 58 + "\n")


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

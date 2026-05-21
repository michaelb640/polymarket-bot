"""
YES/NO arbitrage scanner — runs as a daemon background thread.

In every BTC 5-min binary market, YES + NO must resolve to $1.
If YES_ask + NO_ask < 1 - fees, buying both legs locks in a
risk-free profit regardless of outcome.

Execution uses parallel IOC-style taker orders: both legs are sent
simultaneously. If either leg fails to place, the other is cancelled
immediately to prevent a naked directional exposure.

P&L and trade counts are tracked independently of the signal bot.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logger import logger
import config
import database
import polymarket

_started = False
_lock = threading.Lock()
_stats: dict = {
    "scans": 0,
    "detected": 0,       # opportunities above ARB_LOG_THRESHOLD
    "executed": 0,       # trades placed (live mode only)
    "dry_run_logged": 0, # would-have-executed in DRY_RUN
    "net_pnl": 0.0,      # estimated realised P&L (live mode)
}

_TAKER_FEE = 0.0156  # Polymarket taker fee rate


def get_stats() -> dict:
    """Return a snapshot of arb monitor statistics."""
    with _lock:
        return dict(_stats)


def _extract_order_id(resp: dict) -> str | None:
    if not resp:
        return None
    return (resp.get("orderID") or resp.get("order_id")
            or resp.get("id") or resp.get("orderId"))


def _check_market(market: dict) -> None:
    """Evaluate one market for a YES+NO arb opportunity and act if found."""
    yes_token = polymarket.get_token_for_signal(market, "UP")
    no_token = polymarket.get_token_for_signal(market, "DOWN")
    if yes_token is None or no_token is None:
        return

    yes_id = yes_token.get("token_id")
    no_id = no_token.get("token_id")
    if not yes_id or not no_id:
        return

    _, yes_ask = polymarket.get_token_spread(yes_id)
    _, no_ask = polymarket.get_token_spread(no_id)
    if yes_ask is None or no_ask is None:
        return

    total = yes_ask + no_ask
    if total >= config.ARB_LOG_THRESHOLD:
        return

    gross_pct = (1.0 - total) / total * 100
    fee_pct = _TAKER_FEE * 100
    net_pct = gross_pct - fee_pct
    mid = market["condition_id"]

    with _lock:
        _stats["detected"] += 1

    if total >= config.ARB_EXECUTE_THRESHOLD:
        # Visible but not worth executing (fees eat the margin)
        logger.info(
            f"ARB DETECTED (spread too thin to execute): "
            f"market={mid[:20]} YES@{yes_ask:.3f} NO@{no_ask:.3f} "
            f"total={total:.4f} gross={gross_pct:.2f}% net≈{net_pct:.2f}%"
        )
        database.insert_arb_event("detected", mid, yes_ask, no_ask, total, gross_pct, net_pct)
        return

    # Profitable arb — shares sized so total cost = ARB_NOTIONAL
    shares = round(config.ARB_NOTIONAL / total, 2)
    est_net = shares * (1.0 - total) - shares * total * _TAKER_FEE

    logger.info(
        f"ARB OPPORTUNITY: market={mid[:20]} YES@{yes_ask:.3f} NO@{no_ask:.3f} "
        f"total={total:.4f} gross={gross_pct:.2f}% net≈{net_pct:.2f}% "
        f"shares={shares:.2f} est_profit=${est_net:.3f}"
    )

    if config.DRY_RUN:
        with _lock:
            _stats["dry_run_logged"] += 1
        database.insert_arb_event("dry_run", mid, yes_ask, no_ask, total, gross_pct, net_pct, shares, est_net)
        logger.info(
            f"[DRY_RUN] ARB would execute: {shares:.2f}sh "
            f"(YES@{yes_ask:.3f} + NO@{no_ask:.3f}) est_profit=${est_net:.3f}"
        )
        return

    _execute_arb(mid, yes_id, no_id, yes_ask, no_ask, shares, est_net)


def _execute_arb(
    market_id: str,
    yes_id: str,
    no_id: str,
    yes_ask: float,
    no_ask: float,
    shares: float,
    est_net: float,
) -> None:
    """
    Send YES and NO taker orders in parallel. Cancel the surviving leg
    if either order fails to place (prevents naked exposure).
    """
    yes_order = no_order = None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_yes = ex.submit(polymarket.place_order, market_id, yes_id, "YES", shares, yes_ask)
            f_no = ex.submit(polymarket.place_order, market_id, no_id, "NO", shares, no_ask)
            yes_order = f_yes.result(timeout=8)
            no_order = f_no.result(timeout=8)
    except Exception as e:
        logger.error(f"ARB execution error for {market_id[:20]}: {e}")

    if yes_order and no_order:
        with _lock:
            _stats["executed"] += 1
            _stats["net_pnl"] += est_net
        database.insert_arb_event(
            "executed", market_id, yes_ask, no_ask,
            yes_ask + no_ask,
            (1.0 - (yes_ask + no_ask)) / (yes_ask + no_ask) * 100,
            (1.0 - (yes_ask + no_ask)) / (yes_ask + no_ask) * 100 - _TAKER_FEE * 100,
            shares, est_net,
        )
        logger.info(
            f"ARB FILLED: market={market_id[:20]} shares={shares:.2f} "
            f"YES@{yes_ask:.3f} NO@{no_ask:.3f} est_net=${est_net:.3f} "
            f"(total arb pnl=${_stats['net_pnl']:.2f})"
        )
    else:
        # Cancel whichever leg placed successfully to avoid naked exposure
        for resp in (yes_order, no_order):
            if resp:
                oid = _extract_order_id(resp)
                if oid:
                    polymarket.cancel_order(oid)
        database.insert_arb_event(
            "aborted", market_id, yes_ask, no_ask, yes_ask + no_ask,
            (1.0 - (yes_ask + no_ask)) / (yes_ask + no_ask) * 100, 0.0,
        )
        logger.warning(
            f"ARB aborted — one leg failed: "
            f"yes_placed={bool(yes_order)} no_placed={bool(no_order)}"
        )


def _arb_loop() -> None:
    scan_count = 0
    while True:
        try:
            markets = polymarket.get_active_btc_markets()
            scan_count += 1
            with _lock:
                _stats["scans"] = scan_count

            for market in markets:
                _check_market(market)

            if scan_count % 60 == 0:  # log summary every 5 minutes
                s = get_stats()
                logger.info(
                    f"Arb monitor stats — scans={s['scans']} detected={s['detected']} "
                    f"executed={s['executed']} dry_run_logged={s['dry_run_logged']} "
                    f"net_pnl=${s['net_pnl']:.2f}"
                )
        except Exception as e:
            logger.error(f"Arb monitor loop error: {e}")

        time.sleep(config.ARB_POLL_SECONDS)


def start_arb_monitor() -> None:
    """Start the background arb scanner thread (idempotent)."""
    global _started
    if _started:
        return
    t = threading.Thread(target=_arb_loop, daemon=True, name="arb-monitor")
    t.start()
    _started = True
    logger.info(
        f"Arb monitor started — poll={config.ARB_POLL_SECONDS}s "
        f"execute_threshold={config.ARB_EXECUTE_THRESHOLD} "
        f"log_threshold={config.ARB_LOG_THRESHOLD} "
        f"notional=${config.ARB_NOTIONAL}"
    )

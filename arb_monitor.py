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

# In-flight capital tracking — each entry: {"amount": float, "release_ts": float}
# An arb ties up capital until the market resolves (~5 min after window close).
_deployed: list[dict] = []
_deployed_lock = threading.Lock()


def _current_deployed() -> float:
    """Sum of currently in-flight arb capital (auto-prunes expired entries)."""
    now = time.time()
    with _deployed_lock:
        _deployed[:] = [d for d in _deployed if d["release_ts"] > now]
        return sum(d["amount"] for d in _deployed)


def _reserve_capital(amount: float, release_ts: float) -> None:
    with _deployed_lock:
        _deployed.append({"amount": amount, "release_ts": release_ts})


def _calculate_sizing(
    market_id: str,
    yes_id: str,
    no_id: str,
    yes_ask: float,
    no_ask: float,
) -> tuple[float, float, float, float, str | None]:
    """
    Determine per-arb notional, share count, and REALISTIC fill prices
    after walking the book.

    Returns (shares, notional, realistic_yes_price, realistic_no_price, reject_reason).
    realistic_*_price reflects book-walked slippage (top of book is rarely
    deep enough to absorb our full size, so true fill price is usually
    worse than the displayed best ask).
    """
    total = yes_ask + no_ask
    balance = database.get_account_balance(config.STARTING_BALANCE)

    # 1) Target notional from balance × pct (with legacy fallback)
    if config.ARB_NOTIONAL_PCT > 0:
        target = balance * config.ARB_NOTIONAL_PCT
    else:
        target = config.ARB_NOTIONAL

    # 2) Clamp to absolute floor/ceiling
    notional = max(config.ARB_MIN_NOTIONAL, min(config.ARB_MAX_NOTIONAL, target))

    # 3) Concurrent capital cap — don't blow past MAX_DEPLOYED_PCT of balance
    cap = balance * config.ARB_MAX_DEPLOYED_PCT
    deployed = _current_deployed()
    headroom = cap - deployed
    if headroom < config.ARB_MIN_NOTIONAL:
        return 0, 0, 0, 0, f"capital_cap_hit (deployed=${deployed:.2f}/${cap:.2f})"
    if notional > headroom:
        notional = headroom

    planned_shares = notional / total

    # 4) Fetch real ask ladders for both legs (one call each, ~200ms total)
    yes_ladder = polymarket.get_ask_ladder(yes_id)
    no_ladder = polymarket.get_ask_ladder(no_id)
    if not yes_ladder or not no_ladder:
        return 0, 0, 0, 0, "ladder_fetch_failed"

    # 5) Liquidity safety: cap at min(yes_depth, no_depth) * safety_factor
    yes_total_depth = sum(s for _, s in yes_ladder)
    no_total_depth = sum(s for _, s in no_ladder)
    safe_depth = min(yes_total_depth, no_total_depth) * config.ARB_LIQUIDITY_SAFETY
    if safe_depth < 1.0:
        return 0, 0, 0, 0, f"insufficient_depth (yes={yes_total_depth:.1f}sh, no={no_total_depth:.1f}sh)"

    shares = min(planned_shares, safe_depth)

    # 6) SLIPPAGE: walk both ladders to get realistic weighted-avg fill prices
    real_yes_price, yes_filled = polymarket.walk_ladder(yes_ladder, shares)
    real_no_price, no_filled = polymarket.walk_ladder(no_ladder, shares)
    if yes_filled < 1.0 or no_filled < 1.0:
        return 0, 0, 0, 0, "could_not_walk_book"

    # Use the smaller fill amount as the actual share count
    shares = min(yes_filled, no_filled)
    real_total = real_yes_price + real_no_price
    final_notional = shares * real_total
    if final_notional < config.ARB_MIN_NOTIONAL:
        return 0, 0, 0, 0, f"sized_below_min (${final_notional:.2f} < ${config.ARB_MIN_NOTIONAL})"

    return (
        round(shares, 2),
        round(final_notional, 2),
        round(real_yes_price, 4),
        round(real_no_price, 4),
        None,
    )

_started = False
_lock = threading.Lock()
_stats: dict = {
    "scans": 0,
    "detected": 0,       # opportunities above ARB_LOG_THRESHOLD
    "executed": 0,       # trades placed (live mode only)
    "dry_run_logged": 0, # would-have-executed in DRY_RUN
    "net_pnl": 0.0,      # estimated realised P&L (live mode)
    # Phase 1 latency tracking (rolling avg over last N scans)
    "last_scan_ms": 0.0,
    "avg_scan_ms": 0.0,
    "last_execute_ms": 0.0,  # detection → orders sent
}

_TAKER_FEE = 0.0156  # Polymarket taker fee rate
_SCAN_TIMES_BUFFER: list[float] = []  # rolling buffer of last 100 scan durations


def get_stats() -> dict:
    """Return a snapshot of arb monitor statistics."""
    with _lock:
        return dict(_stats)


def _extract_order_id(resp: dict) -> str | None:
    if not resp:
        return None
    return (resp.get("orderID") or resp.get("order_id")
            or resp.get("id") or resp.get("orderId"))


def check_and_execute_arb(
    market_id: str,
    yes_id: str,
    no_id: str,
    yes_ask: float,
    no_ask: float,
    source: str = "rest",
    book_fetch_ms: float = 0.0,
) -> None:
    """
    Public entry point: given known YES/NO best-ask prices for a market,
    evaluate the arb threshold and execute if profitable.
    `source` is just a tag for logs ("rest" or "ws") so we can see
    which path caught the opportunity.
    """
    total = yes_ask + no_ask
    if total >= config.ARB_LOG_THRESHOLD:
        return

    gross_pct = (1.0 - total) / total * 100
    fee_pct = _TAKER_FEE * 100
    net_pct = gross_pct - fee_pct

    with _lock:
        _stats["detected"] += 1

    if total >= config.ARB_EXECUTE_THRESHOLD:
        logger.info(
            f"[{source}] ARB DETECTED (thin): market={market_id[:20]} "
            f"YES@{yes_ask:.3f} NO@{no_ask:.3f} total={total:.4f} "
            f"gross={gross_pct:.2f}% net≈{net_pct:.2f}%"
        )
        database.insert_arb_event("detected", market_id, yes_ask, no_ask, total, gross_pct, net_pct)
        return

    # New: dynamic sizing with balance / cap / liquidity / slippage checks
    shares, notional, real_yes, real_no, reject = _calculate_sizing(
        market_id, yes_id, no_id, yes_ask, no_ask
    )
    if reject is not None:
        logger.info(
            f"[{source}] ARB skipped — {reject} | market={market_id[:20]} "
            f"YES@{yes_ask:.3f} NO@{no_ask:.3f} total={total:.4f}"
        )
        return

    # Two P&L numbers:
    #   est_net_optimistic — uses displayed best-ask (what dry-run used to show)
    #   est_net_realistic  — uses book-walked weighted-avg fill (true slippage)
    est_net_optimistic = shares * (1.0 - total) - shares * total * _TAKER_FEE
    real_total = real_yes + real_no
    est_net_realistic = shares * (1.0 - real_total) - shares * real_total * _TAKER_FEE
    slippage_cost = est_net_optimistic - est_net_realistic

    # If slippage flips the trade to unprofitable, log as skipped
    if est_net_realistic <= 0:
        logger.info(
            f"[{source}] ARB skipped — unprofitable_after_slippage | "
            f"opt=${est_net_optimistic:.3f} real=${est_net_realistic:.3f} "
            f"slippage=${slippage_cost:.3f} | market={market_id[:20]} "
            f"display={total:.4f} fill={real_total:.4f}"
        )
        return

    logger.info(
        f"[{source}] ARB OPPORTUNITY: market={market_id[:20]} "
        f"display(YES@{yes_ask:.3f}+NO@{no_ask:.3f}={total:.4f}) "
        f"fill(YES@{real_yes:.3f}+NO@{real_no:.3f}={real_total:.4f}) "
        f"shares={shares:.2f} notional=${notional:.2f} "
        f"est_profit: opt=${est_net_optimistic:.3f} real=${est_net_realistic:.3f} "
        f"(slippage=${slippage_cost:.3f})"
    )

    if config.DRY_RUN:
        with _lock:
            _stats["dry_run_logged"] += 1
        # Store the REALISTIC est_pnl so dashboard reflects post-slippage reality
        database.insert_arb_event(
            "dry_run", market_id, real_yes, real_no, real_total,
            (1.0 - real_total) / real_total * 100,
            (1.0 - real_total) / real_total * 100 - _TAKER_FEE * 100,
            shares, est_net_realistic,
        )
        _reserve_capital(notional, time.time() + 360)
        logger.info(
            f"[{source}][DRY_RUN] ARB would execute: {shares:.2f}sh "
            f"notional=${notional:.2f} est_realistic=${est_net_realistic:.3f} "
            f"book_fetch={book_fetch_ms:.0f}ms"
        )
        return

    # Live: reserve capital, then execute at realistic prices
    _reserve_capital(notional, time.time() + 360)
    exec_start = time.perf_counter()
    success = _execute_arb(market_id, yes_id, no_id, real_yes, real_no, shares, est_net_realistic)
    exec_ms = (time.perf_counter() - exec_start) * 1000
    if not success:
        with _deployed_lock:
            if _deployed:
                _deployed.pop()
    with _lock:
        _stats["last_execute_ms"] = round(exec_ms, 1)
    logger.info(f"[{source}] ARB latency: book_fetch={book_fetch_ms:.0f}ms execute={exec_ms:.0f}ms")


def _check_market(market: dict) -> None:
    """REST-driven check: fetch YES/NO order books, then evaluate."""
    yes_token = polymarket.get_token_for_signal(market, "UP")
    no_token = polymarket.get_token_for_signal(market, "DOWN")
    if yes_token is None or no_token is None:
        return

    yes_id = yes_token.get("token_id")
    no_id = no_token.get("token_id")
    if not yes_id or not no_id:
        return

    book_fetch_start = time.perf_counter()
    _, yes_ask = polymarket.get_token_spread(yes_id)
    _, no_ask = polymarket.get_token_spread(no_id)
    book_fetch_ms = (time.perf_counter() - book_fetch_start) * 1000
    if yes_ask is None or no_ask is None:
        return

    check_and_execute_arb(
        market["condition_id"], yes_id, no_id, yes_ask, no_ask,
        source="rest", book_fetch_ms=book_fetch_ms,
    )


def _execute_arb(
    market_id: str,
    yes_id: str,
    no_id: str,
    yes_ask: float,
    no_ask: float,
    shares: float,
    est_net: float,
) -> bool:
    """
    Send YES and NO taker orders in parallel. Cancel the surviving leg
    if either order fails to place (prevents naked exposure).
    Returns True if both legs filled, False otherwise.
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
        return True
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
        return False


def _arb_loop() -> None:
    scan_count = 0
    # Log summary every N scans — Phase 1: 5min / 1s poll = 300 scans
    summary_every = max(1, int(300 / max(config.ARB_POLL_SECONDS, 0.1)))
    while True:
        scan_start = time.perf_counter()
        try:
            markets = polymarket.get_active_btc_markets()
            scan_count += 1

            for market in markets:
                _check_market(market)

            scan_ms = (time.perf_counter() - scan_start) * 1000
            _SCAN_TIMES_BUFFER.append(scan_ms)
            if len(_SCAN_TIMES_BUFFER) > 100:
                _SCAN_TIMES_BUFFER.pop(0)
            avg = sum(_SCAN_TIMES_BUFFER) / len(_SCAN_TIMES_BUFFER)

            with _lock:
                _stats["scans"] = scan_count
                _stats["last_scan_ms"] = round(scan_ms, 1)
                _stats["avg_scan_ms"] = round(avg, 1)

            if scan_count % summary_every == 0:  # periodic summary
                s = get_stats()
                ws_summary = ""
                try:
                    import arb_websocket as _ws
                    ws_stats = _ws.get_stats()
                    ws_summary = (
                        f" | WS: connects={ws_stats['ws_connects']} "
                        f"disconnects={ws_stats['ws_disconnects']} "
                        f"msgs={ws_stats['ws_messages']} "
                        f"triggers={ws_stats['ws_arb_triggers']} "
                        f"last_trigger={ws_stats['last_ws_arb_ms']:.0f}ms"
                    )
                except Exception:
                    pass
                logger.info(
                    f"Arb monitor stats — scans={s['scans']} detected={s['detected']} "
                    f"executed={s['executed']} dry_run_logged={s['dry_run_logged']} "
                    f"net_pnl=${s['net_pnl']:.2f} "
                    f"avg_scan={s['avg_scan_ms']:.0f}ms last_execute={s['last_execute_ms']:.0f}ms"
                    f"{ws_summary}"
                )
        except Exception as e:
            logger.error(f"Arb monitor loop error: {e}")

        # Adaptive sleep: subtract scan time from poll interval (target true cadence)
        elapsed = time.perf_counter() - scan_start
        time.sleep(max(0.05, config.ARB_POLL_SECONDS - elapsed))


def start_arb_monitor() -> None:
    """Start the background arb scanner thread (idempotent)."""
    global _started
    if _started:
        return
    t = threading.Thread(target=_arb_loop, daemon=True, name="arb-monitor")
    t.start()
    _started = True
    if config.ARB_NOTIONAL_PCT > 0:
        sizing = (f"notional={config.ARB_NOTIONAL_PCT*100:.1f}% of balance "
                  f"(min=${config.ARB_MIN_NOTIONAL}, max=${config.ARB_MAX_NOTIONAL})")
    else:
        sizing = f"notional=${config.ARB_NOTIONAL} (fixed legacy)"
    logger.info(
        f"Arb monitor started — poll={config.ARB_POLL_SECONDS}s "
        f"execute_threshold={config.ARB_EXECUTE_THRESHOLD} "
        f"log_threshold={config.ARB_LOG_THRESHOLD} "
        f"{sizing} max_deployed={config.ARB_MAX_DEPLOYED_PCT*100:.0f}% "
        f"liquidity_safety={config.ARB_LIQUIDITY_SAFETY*100:.0f}%"
    )

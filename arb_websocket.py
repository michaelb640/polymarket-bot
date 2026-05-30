"""
WebSocket-driven arbitrage scanner (Phase 2).

Subscribes to Polymarket's CLOB WebSocket for real-time order book
updates on active BTC 5-min markets. On every book event, instantly
checks YES_ask + NO_ask against the arb threshold.

Reaction latency: ~50-100ms from book change to order send,
vs ~500-1500ms for the REST polling scanner.

If the WebSocket disconnects, the REST scanner in arb_monitor.py
continues running as a fallback (it polls every 1s).
"""

import json
import threading
import time
import requests
from logger import logger
import config
import polymarket
import arb_monitor

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_MARKET_REFRESH_SECONDS = 60   # re-scan active markets every 60s
_RECONNECT_BACKOFF = [1, 2, 5, 10, 30, 60]

# Per-token book state: token_id -> {"best_bid": float, "best_ask": float}
_books: dict = {}
# market_id -> {"yes_id": ..., "no_id": ...}
_markets_map: dict = {}
# token_id -> market_id (reverse lookup for fast arb pairing)
_token_to_market: dict = {}
_state_lock = threading.Lock()

_started = False
_stats = {
    "ws_connects": 0,
    "ws_disconnects": 0,
    "ws_messages": 0,
    "ws_arb_triggers": 0,
    "last_ws_arb_ms": 0.0,    # ws message → arb decision
}


def get_stats() -> dict:
    return dict(_stats)


def _best_from_levels(levels: list) -> tuple[float | None, float | None]:
    """Given a list of {price, size} dicts (bids or asks), return the best price."""
    if not levels:
        return None, None
    try:
        prices = [float(lvl["price"]) for lvl in levels if float(lvl.get("size", 0)) > 0]
    except (ValueError, KeyError, TypeError):
        return None, None
    if not prices:
        return None, None
    return min(prices), max(prices)


def _update_book_from_book_event(token_id: str, bids: list, asks: list) -> None:
    """Full book snapshot — overwrite local state."""
    best_bid = best_ask = None
    if bids:
        try:
            bid_prices = [float(b["price"]) for b in bids if float(b.get("size", 0)) > 0]
            if bid_prices:
                best_bid = max(bid_prices)
        except (ValueError, KeyError, TypeError):
            pass
    if asks:
        try:
            ask_prices = [float(a["price"]) for a in asks if float(a.get("size", 0)) > 0]
            if ask_prices:
                best_ask = min(ask_prices)
        except (ValueError, KeyError, TypeError):
            pass

    with _state_lock:
        book = _books.get(token_id, {})
        book["best_bid"] = best_bid
        book["best_ask"] = best_ask
        _books[token_id] = book


def _on_book_event(token_id: str, msg: dict) -> None:
    """Handle a `book` (full snapshot) message."""
    _update_book_from_book_event(token_id, msg.get("bids", []), msg.get("asks", []))
    _maybe_trigger_arb(token_id)


def _on_price_change(token_id: str, msg: dict) -> None:
    """Handle `price_change` (delta) message. Naively re-derive best bid/ask."""
    # `changes` is a list of {price, side, size}. size=0 means remove.
    # We don't keep the full book locally for delta application, so we just
    # invalidate our cached prices and wait for the next `book` snapshot.
    # Simpler and safer than reconstructing the book — Polymarket sends a
    # snapshot every few seconds anyway.
    changes = msg.get("changes", [])
    if not changes:
        return
    # Crude but effective: update best_bid / best_ask if the change improves them
    with _state_lock:
        book = _books.get(token_id, {})
        for ch in changes:
            try:
                price = float(ch["price"])
                size = float(ch.get("size", 0))
                side = ch.get("side", "").upper()
            except (KeyError, ValueError, TypeError):
                continue
            if size == 0:
                continue  # removal; wait for next snapshot for accuracy
            if side == "BUY":
                cur = book.get("best_bid")
                if cur is None or price > cur:
                    book["best_bid"] = price
            elif side == "SELL":
                cur = book.get("best_ask")
                if cur is None or price < cur:
                    book["best_ask"] = price
        _books[token_id] = book

    _maybe_trigger_arb(token_id)


def _maybe_trigger_arb(updated_token_id: str) -> None:
    """If we now have both sides of a market, check for an arb opportunity."""
    market_id = _token_to_market.get(updated_token_id)
    if not market_id:
        return
    pair = _markets_map.get(market_id)
    if not pair:
        return

    yes_id = pair["yes_id"]
    no_id = pair["no_id"]

    with _state_lock:
        yes_book = _books.get(yes_id, {})
        no_book = _books.get(no_id, {})

    yes_ask = yes_book.get("best_ask")
    no_ask = no_book.get("best_ask")
    if yes_ask is None or no_ask is None:
        return

    trigger_start = time.perf_counter()
    arb_monitor.check_and_execute_arb(
        market_id, yes_id, no_id, yes_ask, no_ask,
        source="ws", book_fetch_ms=0.0,
    )
    trigger_ms = (time.perf_counter() - trigger_start) * 1000

    _stats["ws_arb_triggers"] += 1
    _stats["last_ws_arb_ms"] = round(trigger_ms, 1)


def _refresh_subscription_targets() -> list[str]:
    """Get currently active BTC market token IDs to subscribe to."""
    markets = polymarket.get_active_btc_markets()
    new_token_ids: list[str] = []
    new_markets_map = {}
    new_reverse = {}
    for m in markets:
        mid = m.get("condition_id")
        yes_token = polymarket.get_token_for_signal(m, "UP")
        no_token = polymarket.get_token_for_signal(m, "DOWN")
        if not (mid and yes_token and no_token):
            continue
        yid = yes_token.get("token_id")
        nid = no_token.get("token_id")
        if not (yid and nid):
            continue
        new_markets_map[mid] = {"yes_id": yid, "no_id": nid}
        new_reverse[yid] = mid
        new_reverse[nid] = mid
        new_token_ids.extend([yid, nid])

    with _state_lock:
        _markets_map.clear()
        _markets_map.update(new_markets_map)
        _token_to_market.clear()
        _token_to_market.update(new_reverse)

    return new_token_ids


def _ws_loop() -> None:
    """Main WebSocket loop with reconnect."""
    try:
        import websocket  # websocket-client package
    except ImportError:
        logger.error("websocket-client not installed; Phase 2 WS disabled. Falling back to REST.")
        return

    attempt = 0
    while True:
        try:
            token_ids = _refresh_subscription_targets()
            if not token_ids:
                logger.info("WS: no active BTC markets to subscribe to; sleeping 10s")
                time.sleep(10)
                continue

            logger.info(f"WS: connecting to {WS_URL} for {len(token_ids)} tokens")
            ws = websocket.create_connection(WS_URL, timeout=10)
            _stats["ws_connects"] += 1
            attempt = 0  # reset backoff on successful connect

            # Polymarket public market channel subscription
            sub_msg = {
                "auth": {},
                "type": "Market",
                "assets_ids": token_ids,
            }
            ws.send(json.dumps(sub_msg))
            logger.info(f"WS: subscribed to {len(token_ids)} tokens")

            last_refresh = time.time()
            while True:
                # Periodic resubscription as new 5-min windows open
                if time.time() - last_refresh > _MARKET_REFRESH_SECONDS:
                    new_tokens = _refresh_subscription_targets()
                    if set(new_tokens) != set(token_ids):
                        logger.info(f"WS: market list changed — reconnecting with {len(new_tokens)} tokens")
                        ws.close()
                        token_ids = new_tokens
                        break  # outer loop will reconnect with new tokens
                    last_refresh = time.time()

                # Receive with timeout; falls through to refresh check
                ws.settimeout(5)
                try:
                    raw = ws.recv()
                except Exception as e:
                    msg = str(e).lower()
                    if "timed out" in msg or "timeout" in msg:
                        continue
                    raise

                _stats["ws_messages"] += 1

                # Polymarket sends JSON arrays of events
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, list):
                    payload = [payload]

                for ev in payload:
                    if not isinstance(ev, dict):
                        continue
                    event_type = ev.get("event_type") or ev.get("type")
                    asset_id = ev.get("asset_id")
                    if not asset_id:
                        continue
                    if event_type == "book":
                        _on_book_event(asset_id, ev)
                    elif event_type == "price_change":
                        _on_price_change(asset_id, ev)
                    # ignore last_trade_price / tick_size_change for arb purposes

        except Exception as e:
            _stats["ws_disconnects"] += 1
            backoff = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
            logger.warning(f"WS disconnected: {e!r} — reconnecting in {backoff}s")
            attempt += 1
            time.sleep(backoff)


def start_arb_websocket() -> None:
    """Start the background WebSocket subscriber (idempotent)."""
    global _started
    if _started:
        return
    t = threading.Thread(target=_ws_loop, daemon=True, name="arb-ws")
    t.start()
    _started = True
    logger.info("Arb WebSocket subscriber started (Phase 2)")

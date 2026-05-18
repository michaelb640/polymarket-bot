import json
import time
import requests
from logger import logger
import config

GAMMA_API = "https://gamma-api.polymarket.com"

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.constants import POLYGON
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("py-clob-client not installed or import failed; running in mock mode.")

_client = None
_read_client = None


def _get_read_client():
    """L1-only CLOB client for resolution checks. Works in DRY_RUN."""
    global _read_client
    if _read_client is not None:
        return _read_client
    if not _SDK_AVAILABLE:
        return None
    try:
        _read_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=config.POLYMARKET_PRIVATE_KEY,
        )
        logger.info("Read-only ClobClient initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize read ClobClient: {e}")
        _read_client = None
    return _read_client


def _get_client():
    """Full L2 CLOB client for placing orders. Returns None in DRY_RUN."""
    global _client
    if config.DRY_RUN:
        return None
    if _client is not None:
        return _client
    if not _SDK_AVAILABLE:
        return None
    try:
        l1 = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=config.POLYMARKET_PRIVATE_KEY,
        )
        creds = l1.create_or_derive_api_creds()
        logger.info(f"API credentials derived (key={creds.api_key[:8]}...)")
        _client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=config.POLYMARKET_PRIVATE_KEY,
            creds=creds,
        )
        logger.info("ClobClient initialized with derived credentials.")
    except Exception as e:
        logger.error(f"Failed to initialize ClobClient: {e}")
        _client = None
    return _client


def _gamma_fetch_window(window_start_ts: int) -> dict | None:
    """
    Fetch a BTC 5-min market from the Gamma API by window start timestamp.
    The slug is always btc-updown-5m-{timestamp}.
    Returns a normalized market dict with tokens, or None if unavailable.
    """
    slug = f"btc-updown-5m-{window_start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        events = r.json()
        if not events:
            return None
        markets = events[0].get("markets", [])
        if not markets:
            return None
        m = markets[0]

        if not m.get("active") or m.get("closed") or not m.get("acceptingOrders"):
            return None

        try:
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
            prices    = json.loads(m.get("outcomePrices") or "[]")
            outcomes  = json.loads(m.get("outcomes") or "[]")
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Could not parse token data for window {window_start_ts}")
            return None

        tokens = [
            {"token_id": tid, "outcome": out, "price": float(price)}
            for tid, out, price in zip(token_ids, outcomes, prices)
        ]

        return {
            "condition_id": m.get("conditionId"),
            "question": m.get("question"),
            "tokens": tokens,
            "best_ask": m.get("bestAsk", 0.5),
            "best_bid": m.get("bestBid", 0.5),
            "window_start_ts": window_start_ts,
            "window_end_ts": window_start_ts + 300,
            "active": True,
        }
    except Exception as e:
        logger.error(f"Gamma API error for window {window_start_ts}: {e}")
        return None


def get_token_for_signal(market: dict, signal: str) -> dict | None:
    """Return the token for 'UP' or 'DOWN', matching 'Up'/'Down' or 'Yes'/'No' outcomes."""
    tokens = market.get("tokens", [])
    targets = ("up", "yes") if signal == "UP" else ("down", "no")
    for token in tokens:
        if token.get("outcome", "").lower() in targets:
            return token
    return None


def get_token_spread(token_id: str) -> tuple[float | None, float | None]:
    """
    Return (best_bid, best_ask) from the CLOB orderbook in a single call.
    Returns (None, None) on error so callers fall back gracefully.
    """
    client = _get_read_client()
    if client is None:
        return None, None
    try:
        book = client.get_order_book(token_id)
        best_bid, best_ask = None, None
        for b in (book.bids or []):
            try:
                p = float(b["price"] if isinstance(b, dict) else b.price)
                best_bid = p if best_bid is None else max(best_bid, p)
            except (KeyError, AttributeError, TypeError, ValueError):
                continue
        for a in (book.asks or []):
            try:
                p = float(a["price"] if isinstance(a, dict) else a.price)
                best_ask = p if best_ask is None else min(best_ask, p)
            except (KeyError, AttributeError, TypeError, ValueError):
                continue
        return best_bid, best_ask
    except Exception as e:
        logger.error(f"CLOB spread fetch failed for {token_id[:16]}...: {e}")
        return None, None


def get_token_best_ask(token_id: str) -> float | None:
    """
    Fetch the real best-ask price from the CLOB orderbook for a specific token.
    This is the actual price you would pay to buy the token in a live trade.
    Returns None on error so callers can fall back to the Gamma API price.
    """
    client = _get_read_client()
    if client is None:
        return None
    try:
        book = client.get_order_book(token_id)
        asks = book.asks
        if not asks:
            return None
        prices = []
        for a in asks:
            try:
                prices.append(float(a["price"] if isinstance(a, dict) else a.price))
            except (KeyError, AttributeError, TypeError, ValueError):
                continue
        return min(prices) if prices else None
    except Exception as e:
        logger.error(f"CLOB orderbook fetch failed for {token_id[:16]}...: {e}")
        return None


def get_active_btc_markets() -> list[dict]:
    """
    Return active BTC 5-min markets for the current and next window.
    Uses the Gamma API — returns empty list if unreachable (no mock fallback).
    """
    now_ts = int(time.time())
    current_window = (now_ts // 300) * 300

    results = []
    for window_start in (current_window, current_window + 300):
        market = _gamma_fetch_window(window_start)
        if market:
            results.append(market)

    if results:
        logger.info(f"Found {len(results)} active BTC 5-min market(s) via Gamma API.")
    else:
        logger.info("No active BTC 5-min markets found via Gamma API.")

    return results


def place_order(market_id: str, token_id: str, side: str, size: float, price: float) -> dict | None:
    client = _get_client()

    if config.DRY_RUN or client is None:
        logger.info(
            f"[DRY_RUN] place_order: market={market_id} side={side} token={token_id[:12]}... "
            f"size={size} price={price}"
        )
        return {
            "order_id": f"mock_order_{int(time.time())}",
            "market_id": market_id,
            "side": side,
            "size": size,
            "price": price,
            "status": "mock_placed",
        }

    try:
        order_args = OrderArgs(price=price, size=size, side="BUY", token_id=token_id)
        response = client.create_and_post_order(order_args)
        logger.info(f"Order placed: {response}")
        return response
    except Exception as e:
        logger.error(f"Failed to place order market={market_id}: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    client = _get_client()
    if config.DRY_RUN or client is None:
        logger.info(f"[DRY_RUN] cancel_order: order_id={order_id}")
        return True
    try:
        client.cancel(order_id)
        logger.info(f"Order cancelled: {order_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to cancel order {order_id}: {e}")
        return False


def get_open_positions() -> list[dict]:
    client = _get_client()
    if config.DRY_RUN or client is None:
        logger.debug("[DRY_RUN] get_open_positions: returning empty list.")
        return []
    try:
        positions = client.get_positions()
        return positions if isinstance(positions, list) else positions.get("data", [])
    except Exception as e:
        logger.error(f"Failed to fetch positions from Polymarket: {e}")
        return []


def _gamma_fetch_resolved(window_start_ts: int) -> dict | None:
    """
    Fetch a BTC 5-min market (including closed/resolved ones) from Gamma API by slug.
    Returns raw outcome info, or None on error.
    """
    slug = f"btc-updown-5m-{window_start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        events = r.json()
        if not events:
            return None
        markets = events[0].get("markets", [])
        if not markets:
            return None
        m = markets[0]
        try:
            prices   = json.loads(m.get("outcomePrices") or "[]")
            outcomes = json.loads(m.get("outcomes") or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "outcomes": outcomes,
            "prices": prices,
            "resolved": m.get("resolved", False),
            "closed": m.get("closed", False),
        }
    except Exception as e:
        logger.error(f"Gamma resolution fetch error for window {window_start_ts}: {e}")
        return None


def get_market_resolution(market_id: str, side: str, window_start_ts: int | None = None) -> float | None:
    """
    Returns 1.0 if side won, 0.0 if lost, None if not yet resolved.
    Prefers Gamma API slug-based lookup (reliable); falls back to CLOB API.
    Works in DRY_RUN so paper trades settle on real outcomes.
    """
    if market_id.startswith("mock_"):
        return None

    # Normalize YES/NO → UP/DOWN so resolution logic is consistent
    bet_up = side.upper() in ("UP", "YES")

    # --- Gamma API path (preferred) ---
    if window_start_ts is not None:
        data = _gamma_fetch_resolved(window_start_ts)
        if data is None:
            return None
        prices   = data.get("prices", [])
        outcomes = data.get("outcomes", [])
        is_settled = data.get("resolved") or data.get("closed")
        # 0.99 for officially closed markets; 0.95 catches pre-settlement state
        threshold = 0.99 if is_settled else 0.95
        for outcome, price in zip(outcomes, prices):
            try:
                if float(price) >= threshold:
                    outcome_lower = outcome.lower()
                    up_won = outcome_lower in ("up", "yes")
                    return (1.0 if up_won else 0.0) if bet_up else (0.0 if up_won else 1.0)
            except (ValueError, TypeError):
                continue
        return None  # market still live or prices not decisive yet

    # --- CLOB fallback ---
    client = _get_read_client()
    if client is None:
        return None
    try:
        market = client.get_market(market_id)
        if not market.get("resolved"):
            return None
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("winner"):
                outcome = token.get("outcome", "").lower()
                up_won = outcome in ("up", "yes")
                return (1.0 if up_won else 0.0) if bet_up else (0.0 if up_won else 1.0)
        return None
    except Exception as e:
        logger.error(f"Failed to get resolution for {market_id}: {e}")
        return None

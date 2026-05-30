import math
import time
import threading
from collections import deque
import requests
from logger import logger

BINANCE_PRICE_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD"
BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"
_KLINES_SYMBOL = "BTCUSD"

# Rolling 30-sample buffer (10s interval → 5 minutes of data)
_price_buffer: deque = deque(maxlen=30)
_buffer_lock = threading.Lock()
_sampler_started = False

_vol_cache: dict = {"volatility": 0.02, "last_updated": 0.0}
_VOL_TTL = 300  # refresh volatility every 5 minutes

_trend_cache: dict = {"trend": None, "last_updated": 0.0}
_TREND_TTL = 300  # refresh hourly trend every 5 minutes


def _fetch_spot_price() -> float | None:
    try:
        resp = requests.get(BINANCE_PRICE_URL, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        logger.error(f"Spot price fetch failed: {e}")
        return None


def _sampler_loop() -> None:
    """Background thread: sample price every 10 seconds."""
    while True:
        price = _fetch_spot_price()
        if price is not None:
            with _buffer_lock:
                _price_buffer.append(price)
            logger.debug(f"Price sample: ${price:,.2f} (buffer={len(_price_buffer)})")
        time.sleep(10)


def start_price_sampler() -> None:
    """Start the background sampling thread (idempotent)."""
    global _sampler_started
    if _sampler_started:
        return
    t = threading.Thread(target=_sampler_loop, daemon=True, name="price-sampler")
    t.start()
    _sampler_started = True
    logger.info("Price sampler started (10s interval, 30-sample buffer).")


def get_price_buffer() -> list[float]:
    """Return a snapshot of the rolling price buffer (oldest first)."""
    with _buffer_lock:
        return list(_price_buffer)


def _fetch_realized_volatility() -> float:
    try:
        params = {"symbol": _KLINES_SYMBOL, "interval": "1h", "limit": 25}
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < 2:
            return 0.02
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        return round(math.sqrt(variance) * math.sqrt(24), 6)
    except Exception as e:
        logger.error(f"Volatility fetch failed: {e}")
        return 0.02


def get_hourly_trend() -> str | None:
    """
    Returns 'UP', 'DOWN', or None based on BTC's 1-hour trend.
    Fetches the last 4 hourly Binance candles and compares the close
    3 hours ago to the most recent close. Cached for 5 minutes.
    Returns None if the move is too small to call (flat/ambiguous).
    """
    now = time.time()
    if now - _trend_cache["last_updated"] < _TREND_TTL:
        return _trend_cache["trend"]

    try:
        params = {"symbol": _KLINES_SYMBOL, "interval": "1h", "limit": 4}
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=8)
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < 4:
            return _trend_cache["trend"]

        oldest, newest = closes[0], closes[-1]
        pct_change = (newest - oldest) / oldest

        if pct_change > 0.001:       # BTC up >0.1% over last 3 hours → uptrend
            trend = "UP"
        elif pct_change < -0.001:    # BTC down >0.1% over last 3 hours → downtrend
            trend = "DOWN"
        else:
            trend = None             # flat, no strong bias

        _trend_cache["trend"] = trend
        _trend_cache["last_updated"] = now
        logger.debug(f"Hourly trend: {trend} (3h change={pct_change:.4%})")
        return trend
    except Exception as e:
        logger.error(f"Hourly trend fetch failed: {e}")
        return _trend_cache["trend"]


def get_btc_data() -> tuple[float | None, float]:
    """Return (current_price, daily_volatility). Price comes from the buffer."""
    now = time.time()
    if now - _vol_cache["last_updated"] > _VOL_TTL:
        _vol_cache["volatility"] = _fetch_realized_volatility()
        _vol_cache["last_updated"] = now

    with _buffer_lock:
        price = _price_buffer[-1] if _price_buffer else None

    return price, _vol_cache["volatility"]


def get_realized_vol_per_sec(window_seconds: int = 60) -> float:
    """
    Realized volatility per second from the recent price buffer.
    Each buffer sample is 10s apart; uses the last window_seconds of data.
    Falls back to daily vol / sqrt(86400) when buffer is too short.
    """
    with _buffer_lock:
        buf = list(_price_buffer)

    n_samples = max(2, window_seconds // 10)
    buf = buf[-n_samples:]

    if len(buf) < 2:
        daily_vol = _vol_cache.get("volatility", 0.02)
        return daily_vol / math.sqrt(86400)

    log_returns = [math.log(buf[i] / buf[i - 1]) for i in range(1, len(buf))]
    mean_r = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / max(len(log_returns) - 1, 1)
    vol_per_sample = math.sqrt(max(variance, 0.0))
    vol_per_sec = vol_per_sample / math.sqrt(10)
    return max(1e-7, min(0.01, vol_per_sec))

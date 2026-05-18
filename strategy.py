import math
import re
import numpy as np
from logger import logger
import config


# ---------------------------------------------------------------------------
# Signal generation for 5-minute BTC markets
# ---------------------------------------------------------------------------

def generate_signal(prices: list[float], opening_price: float | None = None,
                    hourly_trend: str | None = None,
                    realized_vol: float = 0.02) -> tuple[str, int]:
    """
    prices: BTC prices sampled every 10 seconds, most recent last (up to 30 samples).
    opening_price: BTC price at the start of the current 5-min market window (the "price to beat").
    hourly_trend: 'UP', 'DOWN', or None — signals opposing the 1-hour trend are vetoed.
    realized_vol: daily realized vol (from price_feed); scales thresholds to the current regime.
    Returns (signal, score) where signal is 'UP', 'DOWN', or 'SKIP' and score is 0-4.
    """
    if len(prices) < 12:
        return "SKIP", 0

    current = prices[-1]

    # Scale thresholds by current vol relative to 2% daily baseline (clamped 0.5x–2x).
    # In quiet regimes thresholds shrink (catch smaller moves); in volatile ones they grow (filter noise).
    vol_scale = max(0.5, min(2.0, realized_vol / 0.02))

    # Signal 1: short momentum — last 60s vs previous 60s
    recent = sum(prices[-6:]) / 6
    prior = sum(prices[-12:-6]) / 6
    momentum = (recent - prior) / prior

    # Signal 2: micro trend — linear regression slope over last 2 minutes
    x = list(range(len(prices[-12:])))
    y = prices[-12:]
    slope = float(np.polyfit(x, y, 1)[0])
    trend = slope / current

    # Signal 3: mean reversion — is price extended from the 5m average?
    mean_5m = sum(prices) / len(prices)
    deviation = (current - mean_5m) / mean_5m

    # Signal 4: position vs window opening price ("price to beat")
    opening_dev = ((current - opening_price) / opening_price) if opening_price else 0.0

    up_score = 0
    down_score = 0

    if momentum > 0.00015 * vol_scale:
        up_score += 1
    elif momentum < -0.00015 * vol_scale:
        down_score += 1

    if trend > 0.00005 * vol_scale:
        up_score += 1
    elif trend < -0.00005 * vol_scale:
        down_score += 1

    if deviation > 0.001 * vol_scale:
        down_score += 1
    elif deviation < -0.001 * vol_scale:
        up_score += 1

    if opening_price is not None:
        if opening_dev < -0.0005 * vol_scale:
            up_score += 1
        elif opening_dev > 0.0005 * vol_scale:
            down_score += 1

    logger.debug(
        f"Signal scores: up={up_score} down={down_score} vol_scale={vol_scale:.2f} | "
        f"momentum={momentum:.6f} trend={trend:.6f} deviation={deviation:.6f} "
        f"opening_dev={opening_dev:.6f} hourly_trend={hourly_trend}"
    )

    if up_score >= 2 and down_score == 0:
        raw, score = "UP", up_score
    elif down_score >= 2 and up_score == 0:
        raw, score = "DOWN", down_score
    else:
        return "SKIP", 0

    # UP can fire in any trend (including flat/None); DOWN requires explicit hourly downtrend
    if raw == "DOWN" and hourly_trend != "DOWN":
        logger.debug(f"DOWN signal skipped — hourly trend is {hourly_trend!r}, need DOWN")
        return "SKIP", 0

    # Veto UP signals that oppose an explicit downtrend
    if raw == "UP" and hourly_trend == "DOWN":
        logger.debug(f"UP signal vetoed by hourly downtrend")
        return "SKIP", 0

    return raw, score




def get_entry_side(signal: str, market: dict) -> str | None:
    """
    Map signal to the Polymarket outcome name ('UP' or 'DOWN').
    Price-based filtering is done in the caller once we have the real token price.
    """
    if signal == "SKIP":
        return None
    return "UP" if signal == "UP" else "DOWN"


# ---------------------------------------------------------------------------
# Legacy fair-value helpers (kept for backtest compatibility)
# ---------------------------------------------------------------------------

def calculate_fair_value(btc_price, strike_price, minutes_remaining, daily_volatility=0.02):
    distance = (btc_price - strike_price) / strike_price
    time_decay = 1 - (minutes_remaining / 1440)
    volatility_scalar = daily_volatility * math.sqrt(minutes_remaining / 1440)
    edge = distance / (volatility_scalar + 1e-9)
    fair_value = 1 / (1 + math.exp(-edge * time_decay * 5))
    return round(fair_value, 4)


def parse_strike_price(market_title: str) -> float | None:
    match = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", market_title)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def compute_pnl(position: dict, exit_price: float) -> float:
    # exit_price is 1.0 if the token we bought won, 0.0 if it lost.
    # PnL is always (token value at resolution - what we paid) * size.
    return (exit_price - position["entry_price"]) * position["size"]

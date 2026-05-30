import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("1", "true", "yes")


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


POLYMARKET_PRIVATE_KEY: str = _require("POLYMARKET_PRIVATE_KEY")
# API key/secret/passphrase are auto-derived from the private key — not required
POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")

DRY_RUN: bool = _bool("DRY_RUN", True)
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 30)
STARTING_BALANCE: float = _float("STARTING_BALANCE", 100.0)

# 5-minute market risk settings
POSITION_SIZE: float = _float("POSITION_SIZE", 10.0)
MAX_DAILY_TRADES: int = _int("MAX_DAILY_TRADES", 20)
MAX_CONSECUTIVE_LOSSES: int = _int("MAX_CONSECUTIVE_LOSSES", 8)
ENTRY_WINDOW_SECONDS: int = _int("ENTRY_WINDOW_SECONDS", 240)
MIN_ENTRY_PRICE: float = _float("MIN_ENTRY_PRICE", 0.35)  # skip if token already <35¢ (market has strong consensus against us)
MAX_ENTRY_PRICE: float = _float("MAX_ENTRY_PRICE", 0.80)  # skip if token already >80¢ (market has strong consensus)

# Legacy daily-market settings kept for backtest compatibility
MAX_POSITION_SIZE: float = POSITION_SIZE
MAX_OPEN_POSITIONS: int = 1
DAILY_LOSS_LIMIT: float = _float("DAILY_LOSS_LIMIT", 100.0)
DAILY_LOSS_LIMIT_PCT: float = _float("DAILY_LOSS_LIMIT_PCT", 0.075)
ENTRY_THRESHOLD: float = _float("ENTRY_THRESHOLD", 0.07)
FORCE_CLOSE_MINUTES_BEFORE_RESOLUTION: int = 0

# Market quality: skip if bid-ask spread is too wide
MAX_SPREAD: float = _float("MAX_SPREAD", 0.04)

# Order execution mode: True = post limit order at mid (maker, 0% fee); False = hit ask (taker, 1.56% fee)
USE_MAKER_ORDERS: bool = _bool("USE_MAKER_ORDERS", True)

# YES/NO arbitrage scanner — sizing
# Per-arb notional = clamp(balance × ARB_NOTIONAL_PCT, MIN, MAX)
# Set ARB_NOTIONAL_PCT=0 to fall back to fixed ARB_NOTIONAL (legacy mode)
ARB_NOTIONAL_PCT: float = _float("ARB_NOTIONAL_PCT", 0.05)       # 5% of balance per arb
ARB_MIN_NOTIONAL: float = _float("ARB_MIN_NOTIONAL", 5.0)        # never go below $5
ARB_MAX_NOTIONAL: float = _float("ARB_MAX_NOTIONAL", 200.0)      # never go above $200
ARB_NOTIONAL: float = _float("ARB_NOTIONAL", 20.0)               # legacy: fixed $ if PCT=0
ARB_MAX_DEPLOYED_PCT: float = _float("ARB_MAX_DEPLOYED_PCT", 0.50)  # cap on total in-flight capital
ARB_LIQUIDITY_SAFETY: float = _float("ARB_LIQUIDITY_SAFETY", 0.80)  # only consume 80% of available depth

# YES/NO arbitrage scanner — thresholds and pacing
ARB_EXECUTE_THRESHOLD: float = _float("ARB_EXECUTE_THRESHOLD", 0.97)  # execute only if YES_ask + NO_ask < this
ARB_LOG_THRESHOLD: float = _float("ARB_LOG_THRESHOLD", 0.985)         # log (but don't execute) below this
ARB_POLL_SECONDS: float = _float("ARB_POLL_SECONDS", 1.0)             # scan frequency in seconds (Phase 1: 5→1)

# Kill switch: disable signal-based trading entirely (Phase 1 prep for arb-only).
# When True, main.py loop still runs resolution checks but stops generating signals,
# placing maker orders, or managing pending orders. Arb scanner is unaffected.
DISABLE_SIGNAL_BOT: bool = _bool("DISABLE_SIGNAL_BOT", False)

# CEX latency arbitrage (Phase A) — fair-value signal thresholds
# LATENCY_ARB_THRESHOLD: min |fair_value - market_price| to trigger entry (e.g. 0.06 = 6 cents)
# LATENCY_ARB_MIN_EDGE: min |ln(current_btc/open_btc)| to filter noise (0.0003 ≈ 0.03% BTC move)
# LATENCY_ARB_VOL_WINDOW: seconds of price history for realized-vol calculation
LATENCY_ARB_THRESHOLD: float = _float("LATENCY_ARB_THRESHOLD", 0.06)
LATENCY_ARB_MIN_EDGE: float = _float("LATENCY_ARB_MIN_EDGE", 0.0003)
LATENCY_ARB_VOL_WINDOW: int = _int("LATENCY_ARB_VOL_WINDOW", 60)

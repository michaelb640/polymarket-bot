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

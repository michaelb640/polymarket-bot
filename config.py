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
ENTRY_WINDOW_SECONDS: int = _int("ENTRY_WINDOW_SECONDS", 90)
MAX_ENTRY_PRICE: float = _float("MAX_ENTRY_PRICE", 0.80)  # skip if token already >80¢ (market has strong consensus)
CONVICTION_SKIP_LOW: float = _float("CONVICTION_SKIP_LOW", 0.46)   # skip trades priced between these two values
CONVICTION_SKIP_HIGH: float = _float("CONVICTION_SKIP_HIGH", 0.54) # — market has no conviction near 50¢

# Legacy daily-market settings kept for backtest compatibility
MAX_POSITION_SIZE: float = POSITION_SIZE
MAX_OPEN_POSITIONS: int = 1
DAILY_LOSS_LIMIT: float = _float("DAILY_LOSS_LIMIT", 100.0)
DAILY_LOSS_LIMIT_PCT: float = _float("DAILY_LOSS_LIMIT_PCT", 0.075)
ENTRY_THRESHOLD: float = _float("ENTRY_THRESHOLD", 0.07)
FORCE_CLOSE_MINUTES_BEFORE_RESOLUTION: int = 0

# EV gate: only enter when expected value exceeds this threshold
# P_WIN_SCORE_* are initial estimates — update them using backtest score breakdown
MIN_EDGE: float = _float("MIN_EDGE", 0.03)
P_WIN_SCORE_2: float = _float("P_WIN_SCORE_2", 0.53)
P_WIN_SCORE_3: float = _float("P_WIN_SCORE_3", 0.56)
P_WIN_SCORE_4: float = _float("P_WIN_SCORE_4", 0.60)

# Market quality: skip if bid-ask spread is too wide
MAX_SPREAD: float = _float("MAX_SPREAD", 0.04)

import time
import config
import database
from logger import logger

# In-memory counters (reset on daily rollover via main.py)
_consecutive_losses: int = 0
_daily_trade_count: int = 0
_cooldown_until: float = 0.0  # epoch timestamp; 0 means no cooldown

_COOLDOWN_LOSSES = 3    # trigger cooldown after this many consecutive losses
_COOLDOWN_SECONDS = 600 # sit out for 2 windows (10 minutes)


def record_trade_result(won: bool) -> None:
    global _consecutive_losses, _daily_trade_count, _cooldown_until
    _daily_trade_count += 1
    if won:
        _consecutive_losses = 0
    else:
        _consecutive_losses += 1
        if _consecutive_losses >= _COOLDOWN_LOSSES:
            _cooldown_until = time.time() + _COOLDOWN_SECONDS
            logger.warning(
                f"Risk: {_consecutive_losses} consecutive losses — cooling down for {_COOLDOWN_SECONDS//60} minutes"
            )
    logger.debug(
        f"Trade result: {'WIN' if won else 'LOSS'} | "
        f"streak={_consecutive_losses} | daily_trades={_daily_trade_count}"
    )


def reset_daily_counters() -> None:
    global _consecutive_losses, _daily_trade_count, _cooldown_until
    _consecutive_losses = 0
    _daily_trade_count = 0
    _cooldown_until = 0.0
    logger.info("Daily risk counters reset.")


_DAILY_LOSS_LIMIT_PCT = config.DAILY_LOSS_LIMIT_PCT


def can_open_position(market_id: str) -> bool:
    open_positions = database.get_open_positions()

    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        logger.debug(f"Risk check: already have {len(open_positions)} open position(s) — max is {config.MAX_OPEN_POSITIONS}")
        return False

    if _daily_trade_count >= config.MAX_DAILY_TRADES:
        logger.warning(f"Risk check: daily trade limit reached ({_daily_trade_count}/{config.MAX_DAILY_TRADES})")
        return False

    if _consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
        logger.warning(f"Risk check: {_consecutive_losses} consecutive losses — stopping for the day")
        return False

    now = time.time()
    if now < _cooldown_until:
        remaining = int(_cooldown_until - now)
        logger.debug(f"Risk check: in cooldown after losing streak — {remaining}s remaining")
        return False

    # Daily loss limit — disabled when DAILY_LOSS_LIMIT_PCT=0
    if _DAILY_LOSS_LIMIT_PCT > 0:
        daily_pnl = database.get_daily_pnl()
        if daily_pnl < 0:
            balance = database.get_account_balance(config.STARTING_BALANCE)
            limit = balance * _DAILY_LOSS_LIMIT_PCT
            if abs(daily_pnl) >= limit:
                logger.warning(
                    f"Risk check: daily loss ${abs(daily_pnl):.2f} hit {_DAILY_LOSS_LIMIT_PCT*100:.0f}% limit "
                    f"(${limit:.2f} of ${balance:.2f} balance) — stopping for the day"
                )
                return False

    if database.market_has_open_position(market_id):
        logger.debug(f"Risk check: already have an open position in market {market_id}")
        return False

    return True



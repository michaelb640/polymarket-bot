import sqlite3
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from logger import logger

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_day_range() -> tuple[str, str]:
    """Return (start_utc, end_utc) naive ISO strings bracketing today in Pacific time."""
    now = datetime.now(_PACIFIC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (
        day_start.astimezone(timezone.utc).strftime(fmt),
        day_end.astimezone(timezone.utc).strftime(fmt),
    )


def pacific_today() -> str:
    """Return today's date string in Pacific time (for summaries and display)."""
    return datetime.now(_PACIFIC).date().isoformat()

DB_PATH = "bot.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                btc_entry_price REAL,
                size REAL NOT NULL,
                entry_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_time TEXT,
                pnl REAL
            )
        """)
        # Migrate existing tables that predate new columns
        for col in ("btc_entry_price REAL", "market_name TEXT", "window_start_ts INTEGER"):
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
            except Exception:
                pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                total_trades INTEGER NOT NULL DEFAULT 0,
                winners INTEGER NOT NULL DEFAULT 0,
                losers INTEGER NOT NULL DEFAULT 0,
                gross_pnl REAL NOT NULL DEFAULT 0.0,
                fees_paid REAL NOT NULL DEFAULT 0.0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS arb_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                market_id TEXT,
                yes_ask REAL,
                no_ask REAL,
                total REAL,
                gross_pct REAL,
                net_pct REAL,
                shares REAL,
                est_pnl REAL
            )
        """)
        conn.commit()
    logger.debug("Database initialized.")


def insert_position(market_id: str, side: str, entry_price: float, size: float,
                    btc_entry_price: float | None = None, market_name: str | None = None,
                    window_start_ts: int | None = None) -> int:
    entry_time = datetime.utcnow().isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO positions
               (market_id, side, entry_price, btc_entry_price, market_name, size, entry_time, status, window_start_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (market_id, side, entry_price, btc_entry_price, market_name, size, entry_time, window_start_ts),
        )
        conn.commit()
        row_id = cursor.lastrowid
    logger.debug(f"Inserted position id={row_id} market={market_name or market_id} side={side} btc=${btc_entry_price}")
    return row_id


def update_position_closed(position_id: int, exit_price: float, pnl: float) -> None:
    exit_time = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE positions
               SET status='closed', exit_price=?, exit_time=?, pnl=?
               WHERE id=?""",
            (exit_price, exit_time, pnl, position_id),
        )
        conn.commit()
    logger.debug(f"Closed position id={position_id} exit={exit_price} pnl={pnl:.4f}")


def get_open_positions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open'"
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl() -> float:
    start, end = _pacific_day_range()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM positions WHERE status='closed' AND exit_time >= ? AND exit_time < ?",
            (start, end),
        ).fetchone()
    return row["total"] if row else 0.0


def upsert_daily_summary(
    total_trades: int,
    winners: int,
    losers: int,
    gross_pnl: float,
    fees_paid: float,
    for_date: str | None = None,
) -> None:
    target = for_date or pacific_today()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO daily_summary (date, total_trades, winners, losers, gross_pnl, fees_paid)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 total_trades=excluded.total_trades,
                 winners=excluded.winners,
                 losers=excluded.losers,
                 gross_pnl=excluded.gross_pnl,
                 fees_paid=excluded.fees_paid""",
            (target, total_trades, winners, losers, gross_pnl, fees_paid),
        )
        conn.commit()
    logger.debug(f"Daily summary upserted for {target}: pnl={gross_pnl:.2f}")


def get_closed_positions_today() -> list[dict]:
    start, end = _pacific_day_range()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND exit_time >= ? AND exit_time < ? ORDER BY exit_time DESC",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def get_push_positions() -> list[dict]:
    """Return all closed positions still sitting at the 0.5 PUSH fallback."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND exit_price=0.5"
        ).fetchall()
    return [dict(r) for r in rows]


def get_account_balance(starting_balance: float = 100.0) -> float:
    """Starting balance plus all realised P&L to date."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM positions WHERE status='closed'"
        ).fetchone()
    return starting_balance + (row["total"] if row else 0.0)


def market_has_open_position(market_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE market_id=? AND status='open'",
            (market_id,),
        ).fetchone()
    return row["cnt"] > 0


def insert_arb_event(
    event_type: str,
    market_id: str,
    yes_ask: float,
    no_ask: float,
    total: float,
    gross_pct: float,
    net_pct: float,
    shares: float | None = None,
    est_pnl: float | None = None,
) -> None:
    """Record an arb scanner event (detected / dry_run / executed / aborted)."""
    event_time = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO arb_events
               (event_time, event_type, market_id, yes_ask, no_ask, total, gross_pct, net_pct, shares, est_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_time, event_type, market_id, yes_ask, no_ask, total,
             gross_pct, net_pct, shares, est_pnl),
        )
        conn.commit()


def get_arb_stats() -> dict:
    """Summary counts and estimated P&L for the arb monitor."""
    with _connect() as conn:
        detected = conn.execute(
            "SELECT COUNT(*) as cnt FROM arb_events WHERE event_type IN ('detected','dry_run','executed')"
        ).fetchone()
        executed = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(est_pnl),0) as pnl "
            "FROM arb_events WHERE event_type IN ('executed','dry_run')"
        ).fetchone()
    return {
        "detected": detected["cnt"] if detected else 0,
        "executed": executed["cnt"] if executed else 0,
        "est_pnl": round(float(executed["pnl"]), 4) if executed else 0.0,
    }


def get_arb_events(limit: int = 30) -> list[dict]:
    """Return the most recent arb events for the dashboard."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM arb_events ORDER BY event_time DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

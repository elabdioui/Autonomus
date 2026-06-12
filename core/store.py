"""SQLite persistence layer — SPEC 0 §5."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.models import Signal, TradeRecord

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "scalper.db"

_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    killzone TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_price REAL, entry_zone_low REAL, entry_zone_high REAL,
    sl REAL, tp1 REAL, tp2 REAL, sl_pips REAL,
    score INTEGER,
    confluences TEXT,
    context TEXT,
    status TEXT NOT NULL,
    skip_reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    mt5_ticket INTEGER,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    lot REAL NOT NULL,
    entry_price_fill REAL,
    entry_ts_utc TEXT,
    sl_initial REAL, sl_current REAL, tp1 REAL, tp2 REAL,
    status TEXT NOT NULL,
    exit_reason TEXT,
    exit_ts_utc TEXT,
    pnl_pips REAL, pnl_usd REAL,
    mae_pips REAL DEFAULT 0, mfe_pips REAL DEFAULT 0,
    news_flag INTEGER DEFAULT 0,
    vol_regime TEXT,
    spread_at_entry_pips REAL
);

CREATE TABLE IF NOT EXISTS trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT REFERENCES trades(trade_id),
    ts_utc TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts_utc TEXT NOT NULL,
    open_positions INTEGER,
    last_scan_killzone TEXT
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    with _conn() as con:
        con.executescript(_DDL)
        # Idempotent migrations — SQLite has no ADD COLUMN IF NOT EXISTS
        for stmt in (
            "ALTER TABLE trades ADD COLUMN be_target REAL",
            "ALTER TABLE trades ADD COLUMN be_retries INTEGER DEFAULT 0",
        ):
            try:
                con.execute(stmt)
            except Exception:
                pass  # column already exists


def insert_signal(sig: Signal, status: str = "DETECTED", skip_reason: str | None = None) -> None:
    row = sig.to_row()
    row["status"] = status
    row["skip_reason"] = skip_reason
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO signals
               (signal_id, ts_utc, strategy, direction, killzone, entry_type,
                entry_price, entry_zone_low, entry_zone_high,
                sl, tp1, tp2, sl_pips, score, confluences, context, status, skip_reason)
               VALUES
               (:signal_id,:ts_utc,:strategy,:direction,:killzone,:entry_type,
                :entry_price,:entry_zone_low,:entry_zone_high,
                :sl,:tp1,:tp2,:sl_pips,:score,:confluences,:context,:status,:skip_reason)""",
            row,
        )


def update_signal_status(signal_id: str, status: str, skip_reason: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status=?, skip_reason=? WHERE signal_id=?",
            (status, skip_reason, signal_id),
        )


def insert_trade(trade: TradeRecord) -> None:
    row = trade.to_row()
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO trades
               (trade_id, signal_id, mt5_ticket, strategy, direction, lot,
                entry_price_fill, entry_ts_utc, sl_initial, sl_current, tp1, tp2,
                status, exit_reason, exit_ts_utc, pnl_pips, pnl_usd,
                mae_pips, mfe_pips, news_flag, vol_regime, spread_at_entry_pips,
                be_target, be_retries)
               VALUES
               (:trade_id,:signal_id,:mt5_ticket,:strategy,:direction,:lot,
                :entry_price_fill,:entry_ts_utc,:sl_initial,:sl_current,:tp1,:tp2,
                :status,:exit_reason,:exit_ts_utc,:pnl_pips,:pnl_usd,
                :mae_pips,:mfe_pips,:news_flag,:vol_regime,:spread_at_entry_pips,
                :be_target,:be_retries)""",
            row,
        )


def update_trade(trade_id: str, **kwargs) -> None:
    if not kwargs:
        return
    # Serialize datetime values
    for k, v in kwargs.items():
        if isinstance(v, datetime):
            kwargs[k] = v.isoformat()
        elif isinstance(v, bool):
            kwargs[k] = int(v)
    cols = ", ".join(f"{k}=:{k}" for k in kwargs)
    kwargs["trade_id"] = trade_id
    with _conn() as con:
        con.execute(f"UPDATE trades SET {cols} WHERE trade_id=:trade_id", kwargs)


def insert_event(trade_id: str, event: str, detail: dict | None = None) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO trade_events (trade_id, ts_utc, event, detail) VALUES (?,?,?,?)",
            (trade_id, datetime.now(timezone.utc).isoformat(), event, json.dumps(detail) if detail else None),
        )


def upsert_heartbeat(open_positions: int, last_scan_killzone: str | None) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO heartbeat (id, ts_utc, open_positions, last_scan_killzone)
               VALUES (1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 ts_utc=excluded.ts_utc,
                 open_positions=excluded.open_positions,
                 last_scan_killzone=excluded.last_scan_killzone""",
            (datetime.now(timezone.utc).isoformat(), open_positions, last_scan_killzone),
        )


def get_open_trades() -> list[TradeRecord]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE status IN ('PENDING','OPEN','PARTIAL')"
        ).fetchall()
    return [TradeRecord.from_row(dict(r)) for r in rows]


def get_trade_by_ticket(ticket: int) -> TradeRecord | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM trades WHERE mt5_ticket=?", (ticket,)
        ).fetchone()
    return TradeRecord.from_row(dict(row)) if row else None

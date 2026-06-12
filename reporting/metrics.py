"""Pure measurement functions over the SQLite journal.

All functions accept optional filter kwargs and return plain dataclasses.
No MT5 dependency — safe to call from tests or CLI without a terminal.
"""
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import core.store as _store

_STRATEGIES   = ["S1", "S2", "S3", "S4"]
_KILLZONES    = ["LONDON", "NY_AM", "NY_PM", "ASIA"]
_EXIT_REASONS = ["TP2", "BE", "SL", "TIMEOUT", "FRIDAY_FLAT", "MANUAL"]


# ── Output dataclasses ────────────────────────────────────────────────────────

@dataclass
class TradeStats:
    count: int        = 0
    wins: int         = 0
    losses: int       = 0
    winrate: float    = 0.0
    avg_win_pips: float  = 0.0
    avg_loss_pips: float = 0.0   # negative magnitude
    expectancy_pips: float = 0.0
    profit_factor: float   = 0.0
    total_pnl_pips: float  = 0.0
    total_pnl_usd: float   = 0.0
    exits:     dict = field(default_factory=dict)   # reason → count
    exits_pct: dict = field(default_factory=dict)   # reason → 0-1
    avg_mae_winners: float = 0.0
    avg_mae_losers: float  = 0.0
    avg_mfe_losers: float  = 0.0   # "how close losers got to TP1"
    avg_duration_min: float = 0.0


@dataclass
class SignalFunnel:
    strategy: str             = ""
    total_signals: int        = 0
    detected: int             = 0
    executed: int             = 0
    skipped_sl_too_wide: int  = 0
    skipped_position_open: int = 0
    skipped_cooldown: int     = 0
    skipped_spread: int       = 0
    skipped_order_rejected: int = 0


@dataclass
class DayRow:
    date: str
    trades: int
    pnl_pips: float
    pnl_usd: float
    cum_pnl_pips: float
    max_intraday_dd_pips: float


# ── Internal helpers ──────────────────────────────────────────────────────────

def _con() -> sqlite3.Connection:
    con = sqlite3.connect(_store.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _load_closed_trades(
    con: sqlite3.Connection,
    strategy: str | None    = None,
    direction: str | None   = None,
    killzone: str | None    = None,
    news_flag: int | None   = None,
    vol_regime: str | None  = None,
    date_from: str | None   = None,
    date_to: str | None     = None,
) -> list[dict]:
    where = ["t.status = 'CLOSED'", "t.pnl_pips IS NOT NULL"]
    params: list = []

    def _add(clause, val):
        where.append(clause)
        params.append(val)

    if strategy:   _add("t.strategy = ?",   strategy)
    if direction:  _add("t.direction = ?",  direction)
    if killzone:   _add("s.killzone = ?",   killzone)
    if news_flag is not None: _add("t.news_flag = ?", int(news_flag))
    if vol_regime: _add("t.vol_regime = ?", vol_regime)
    if date_from:  _add("t.exit_ts_utc >= ?", date_from)
    if date_to:    _add("t.exit_ts_utc <  ?", date_to)

    sql = f"""
        SELECT t.*, s.killzone, s.score, s.entry_type
        FROM   trades   t
        LEFT JOIN signals s ON t.signal_id = s.signal_id
        WHERE  {' AND '.join(where)}
        ORDER  BY t.exit_ts_utc DESC
    """
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _duration_min(row: dict) -> float:
    try:
        e = datetime.fromisoformat(row["entry_ts_utc"])
        x = datetime.fromisoformat(row["exit_ts_utc"])
        if e.tzinfo is None: e = e.replace(tzinfo=timezone.utc)
        if x.tzinfo is None: x = x.replace(tzinfo=timezone.utc)
        return (x - e).total_seconds() / 60.0
    except Exception:
        return 0.0


def _max_intraday_dd(pnls: list[float]) -> float:
    """Peak-to-trough drawdown of the intraday pnl sequence."""
    peak = cum = max_dd = 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


# ── Public API ────────────────────────────────────────────────────────────────

def compute_stats(**filters) -> TradeStats:
    """Compute TradeStats for the closed trades matching the given filters."""
    con = _con()
    rows = _load_closed_trades(con, **filters)
    con.close()

    if not rows:
        return TradeStats()

    count   = len(rows)
    wins    = [r for r in rows if (r["pnl_pips"] or 0) > 0]
    losses  = [r for r in rows if (r["pnl_pips"] or 0) <= 0]

    winrate = len(wins) / count

    avg_win  = sum(r["pnl_pips"] for r in wins)  / len(wins)  if wins   else 0.0
    avg_loss = sum(r["pnl_pips"] for r in losses) / len(losses) if losses else 0.0

    expectancy = winrate * avg_win + (1 - winrate) * avg_loss

    gross_profit = sum(r["pnl_pips"] for r in wins)
    gross_loss   = abs(sum(r["pnl_pips"] for r in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    total_pnl_pips = sum(r["pnl_pips"] for r in rows)
    total_pnl_usd  = sum((r["pnl_usd"] or 0) for r in rows)

    exits = {r: sum(1 for t in rows if t["exit_reason"] == r) for r in _EXIT_REASONS}
    exits_pct = {r: exits[r] / count for r in _EXIT_REASONS}

    def _avg(lst, key):
        vals = [(r[key] or 0.0) for r in lst]
        return sum(vals) / len(vals) if vals else 0.0

    avg_mae_winners = _avg(wins,   "mae_pips")
    avg_mae_losers  = _avg(losses, "mae_pips")
    avg_mfe_losers  = _avg(losses, "mfe_pips")

    durations = [_duration_min(r) for r in rows]
    avg_duration_min = sum(durations) / count

    return TradeStats(
        count=count, wins=len(wins), losses=len(losses),
        winrate=round(winrate, 4),
        avg_win_pips=round(avg_win, 2),
        avg_loss_pips=round(avg_loss, 2),
        expectancy_pips=round(expectancy, 2),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else profit_factor,
        total_pnl_pips=round(total_pnl_pips, 1),
        total_pnl_usd=round(total_pnl_usd, 2),
        exits=exits, exits_pct=exits_pct,
        avg_mae_winners=round(avg_mae_winners, 2),
        avg_mae_losers=round(avg_mae_losers, 2),
        avg_mfe_losers=round(avg_mfe_losers, 2),
        avg_duration_min=round(avg_duration_min, 1),
    )


def compute_funnel(strategy: str | None = None) -> list[SignalFunnel]:
    """Return signal funnel counts per strategy."""
    con = _con()
    strategies = [strategy] if strategy else _STRATEGIES
    result = []

    for s in strategies:
        rows = con.execute(
            "SELECT status, COUNT(*) n FROM signals WHERE strategy=? GROUP BY status", (s,)
        ).fetchall()
        c = {r["status"]: r["n"] for r in rows}
        result.append(SignalFunnel(
            strategy=s,
            total_signals=sum(c.values()),
            detected=c.get("DETECTED", 0),
            executed=c.get("EXECUTED", 0),
            skipped_sl_too_wide=c.get("SKIPPED_SL_TOO_WIDE", 0),
            skipped_position_open=c.get("SKIPPED_POSITION_OPEN", 0),
            skipped_cooldown=c.get("SKIPPED_COOLDOWN", 0),
            skipped_spread=c.get("SKIPPED_SPREAD", 0),
            skipped_order_rejected=c.get("SKIPPED_ORDER_REJECTED", 0),
        ))

    con.close()
    return result


def compute_by_killzone() -> dict[tuple[str, str], TradeStats]:
    """(strategy, killzone) → TradeStats matrix."""
    return {
        (s, kz): compute_stats(strategy=s, killzone=kz)
        for s in _STRATEGIES for kz in _KILLZONES
    }


def compute_daily(tz_name: str = "Africa/Casablanca") -> list[DayRow]:
    """One DayRow per calendar day (local timezone), sorted ascending."""
    import pytz
    tz = pytz.timezone(tz_name)
    con = _con()
    rows = con.execute(
        "SELECT pnl_pips, pnl_usd, exit_ts_utc FROM trades "
        "WHERE status='CLOSED' AND pnl_pips IS NOT NULL ORDER BY exit_ts_utc"
    ).fetchall()
    con.close()

    if not rows:
        return []

    daily_pips: dict[str, list[float]] = defaultdict(list)
    daily_usd:  dict[str, float]       = defaultdict(float)

    for r in rows:
        try:
            dt = datetime.fromisoformat(r["exit_ts_utc"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_str = dt.astimezone(tz).strftime("%Y-%m-%d")
            daily_pips[date_str].append(r["pnl_pips"])
            daily_usd[date_str] += (r["pnl_usd"] or 0.0)
        except Exception:
            continue

    result: list[DayRow] = []
    cum = 0.0
    for date_str in sorted(daily_pips):
        pnls    = daily_pips[date_str]
        day_pip = sum(pnls)
        cum    += day_pip
        result.append(DayRow(
            date=date_str,
            trades=len(pnls),
            pnl_pips=round(day_pip, 1),
            pnl_usd=round(daily_usd[date_str], 2),
            cum_pnl_pips=round(cum, 1),
            max_intraday_dd_pips=round(_max_intraday_dd(pnls), 1),
        ))
    return result

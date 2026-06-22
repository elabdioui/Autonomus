"""Local read-only FastAPI dashboard.

Run:  uvicorn reporting.dashboard:app --host 127.0.0.1 --port 8080
Bind to 127.0.0.1 only — no auth, no external exposure.
Auto-refresh every 30 s via meta tag.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import core.store as _store
from reporting.metrics import (
    compute_stats, compute_funnel, compute_by_killzone,
    _STRATEGIES, _KILLZONES, _EXIT_REASONS,
)

app = FastAPI(title="xauusd-scalper", docs_url=None, redoc_url=None)

_CSS = """
body{font-family:monospace;margin:20px;background:#1a1a1a;color:#d4d4d4}
h2{color:#e8b86d;margin-top:28px}
table{border-collapse:collapse;width:100%;margin-bottom:16px}
th{background:#2d2d2d;color:#e8b86d;padding:6px 10px;text-align:left;font-size:13px}
td{padding:5px 10px;font-size:12px;border-bottom:1px solid #2d2d2d}
tr:hover td{background:#252525}
.green{color:#4ec94e}.red{color:#e05252}.gray{color:#888}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.badge-ok{background:#1e3a1e;color:#4ec94e}
.badge-warn{background:#3a1e1e;color:#e05252}
"""


def _con():
    con = sqlite3.connect(_store.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _heartbeat_html() -> str:
    con = _con()
    row = con.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    con.close()
    if not row:
        return '<span class="badge badge-warn">No heartbeat</span>'
    try:
        ts = datetime.fromisoformat(row["ts_utc"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
        label = f"OK — {int(age_s)}s ago | kz={row['last_scan_killzone'] or 'NONE'} | pos={row['open_positions']}"
        cls = "badge-ok" if age_s < 120 else "badge-warn"
    except Exception as exc:
        label, cls = f"Error: {exc}", "badge-warn"
    return f'<span class="badge {cls}">{label}</span>'


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{v}</td>" for v in row)
        body += f"<tr>{cells}</tr>"
    return f"<table><tr>{th}</tr>{body}</table>"


def _open_positions_html() -> str:
    con = _con()
    rows = con.execute(
        "SELECT * FROM trades WHERE status IN ('OPEN','PARTIAL') ORDER BY entry_ts_utc DESC"
    ).fetchall()
    con.close()
    if not rows:
        return '<p class="gray">No open positions.</p>'
    cols = ["Ticket", "Strategy", "Dir", "Entry", "SL", "TP1", "TP2",
            "Structural SL", "Spread", "Tags", "Status", "EntryTs"]
    data = []
    for r in rows:
        structural = r["sl_structural_pips"] or 0
        sl_note = (f"{structural:.1f}p -> 20p" if structural != 20 else "20p")
        tags = " ".join(name for name, value in (
            ("position", r["would_block_position"]),
            ("cooldown", r["would_block_cooldown"]),
            ("news", r["would_block_news"]),
            ("spread", r["would_block_spread"]),
        ) if value) or "-"
        data.append([r["mt5_ticket"], r["strategy"], r["direction"],
                     f"{r['entry_price_fill']:.3f}", f"{r['sl_current']:.3f}",
                     f"{r['tp1']:.3f}", f"{r['tp2']:.3f}", sl_note,
                     f"{(r['spread_at_entry_pips'] or 0):.1f}p", tags,
                     r["status"], r["entry_ts_utc"][:16]])
    return _table(cols, data)


def _todays_trades_html() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = _con()
    rows = con.execute(
        """SELECT t.*, s.killzone FROM trades t
           LEFT JOIN signals s ON s.signal_id=t.signal_id
           WHERE t.status='CLOSED' AND t.exit_ts_utc LIKE ?
           ORDER BY t.exit_ts_utc DESC""",
        (f"{today}%",)
    ).fetchall()
    con.close()
    if not rows:
        return f'<p class="gray">No closed trades today ({today}).</p>'
    cols = ["Ticket", "Strategy", "KZ", "Dir", "SL structural/executed",
            "Spread", "Gross$", "Commission$", "Net$", "Tags", "ExitReason", "ExitTs"]
    data = []
    for r in rows:
        pip = r["pnl_pips"] or 0
        cls = "green" if pip > 0 else "red"
        structural = r["sl_structural_pips"] or 0
        if structural > 20:
            sl_note = f'<span class="badge badge-warn">structural {structural:.1f}p - capped at 20p</span>'
        elif structural < 20:
            sl_note = f'<span class="badge badge-ok">structural {structural:.1f}p - executed at 20p</span>'
        else:
            sl_note = "20p"
        net = r["pnl_net_usd"] if r["pnl_net_usd"] is not None else (r["pnl_usd"] or 0)
        tags = " ".join(name for name, value in (
            ("position", r["would_block_position"]),
            ("cooldown", r["would_block_cooldown"]),
            ("news", r["would_block_news"]),
            ("spread", r["would_block_spread"]),
        ) if value) or "-"
        data.append([r["mt5_ticket"], r["strategy"], r["killzone"] or "-",
                     r["direction"], sl_note, f'{(r["spread_at_entry_pips"] or 0):.1f}p',
                     f'{(r["pnl_gross_usd"] or 0):+.2f}',
                     f'{(r["commission_usd"] or 0):.2f}',
                     f'<span class="{cls}">{net:+.2f}</span>', tags,
                     r["exit_reason"], r["exit_ts_utc"][:16]])
    return _table(cols, data)


def _summary_html() -> str:
    cols = ["Strategy", "Count", "Winrate%", "ExpPips", "ProfitFactor", "TotalPips", "TotalUsd"]
    data = []
    for s in _STRATEGIES:
        st = compute_stats(strategy=s)
        cls = "green" if st.expectancy_pips > 0 else "red"
        data.append([
            s, st.count, f"{st.winrate*100:.0f}%",
            f'<span class="{cls}">{st.expectancy_pips:+.2f}</span>',
            "∞" if st.profit_factor == float("inf") else f"{st.profit_factor:.2f}",
            f"{st.total_pnl_pips:+.1f}", f"{st.total_pnl_usd:+.2f}",
        ])
    return _table(cols, data)


def _killzone_summary_html() -> str:
    cols = ["Strategy", "Killzone", "Count", "Winrate%", "Expectancy", "Net$"]
    matrix = compute_by_killzone()
    data = []
    for strategy in _STRATEGIES:
        for killzone in _KILLZONES:
            st = matrix[(strategy, killzone)]
            data.append([strategy, killzone, st.count, f"{st.winrate*100:.0f}%",
                         f"{st.expectancy_pips:+.2f}", f"{st.total_pnl_usd:+.2f}"])
    return _table(cols, data)


def _recent_signals_html(limit: int = 20) -> str:
    con = _con()
    rows = con.execute(
        "SELECT signal_id, ts_utc, strategy, direction, killzone, status, skip_reason "
        "FROM signals ORDER BY ts_utc DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    if not rows:
        return '<p class="gray">No signals yet.</p>'
    cols = ["Id", "Time", "Strategy", "Dir", "Killzone", "Status", "SkipReason"]
    data = [[r["signal_id"][:8], r["ts_utc"][:16], r["strategy"], r["direction"],
             r["killzone"], r["status"], r["skip_reason"] or ""]
            for r in rows]
    return _table(cols, data)


def _full_html() -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>xauusd-scalper</title>
<style>{_CSS}</style>
</head><body>
<h1>&#x1F4C8; xauusd-scalper dashboard</h1>
<p>Heartbeat: {_heartbeat_html()} &nbsp; <span class="gray">{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</span></p>

<h2>Open Positions</h2>
{_open_positions_html()}

<h2>Today's Closed Trades</h2>
{_todays_trades_html()}

<h2>Per-Strategy Summary (all time)</h2>
{_summary_html()}

<h2>Strategy x Killzone Segmentation</h2>
{_killzone_summary_html()}

<h2>Last 20 Signals</h2>
{_recent_signals_html()}

<p class="gray" style="margin-top:40px;font-size:11px">Auto-refresh 30 s &mdash; DEMO ONLY &mdash; no live capital</p>
</body></html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _full_html()


@app.get("/api/summary")
async def api_summary():
    result = {}
    for s in _STRATEGIES:
        st = compute_stats(strategy=s)
        result[s] = {
            "count": st.count, "winrate": st.winrate,
            "expectancy_pips": st.expectancy_pips,
            "profit_factor": st.profit_factor if st.profit_factor != float("inf") else None,
            "total_pnl_pips": st.total_pnl_pips,
            "total_pnl_usd": st.total_pnl_usd,
            "exits": st.exits,
        }
    return JSONResponse(result)


@app.get("/api/trades")
async def api_trades(limit: int = 100):
    con = _con()
    rows = con.execute(
        "SELECT * FROM trades ORDER BY entry_ts_utc DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return JSONResponse([dict(r) for r in rows])

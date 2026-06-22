"""Print stable key=value database status for ops/bot.ps1."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone


def main(db: str) -> None:
    con = sqlite3.connect(db, timeout=5)
    try:
        open_pos = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')"
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        con.close()
        print(f"db=UNINITIALIZED,{exc}")
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_cnt, today_pips, today_usd = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl_pips),0), COALESCE(SUM(pnl_usd),0) "
        "FROM trades WHERE status='CLOSED' AND exit_ts_utc LIKE ?", (today + "%",)
    ).fetchone()
    total_cnt, total_pips = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl_pips),0) FROM trades WHERE status='CLOSED'"
    ).fetchone()
    recent = con.execute(
        "SELECT strategy, direction, status, COALESCE(pnl_pips,0) "
        "FROM trades ORDER BY entry_ts_utc DESC LIMIT 3"
    ).fetchall()
    hb = con.execute(
        "SELECT ts_utc, open_positions, last_scan_killzone FROM heartbeat WHERE id=1"
    ).fetchone()
    con.close()

    print(f"open_pos={open_pos}")
    print(f"today={today_cnt},{today_pips:.1f},{today_usd:.2f}")
    print(f"total={total_cnt},{total_pips:.1f}")
    for strategy, direction, status, pnl in recent:
        print(f"recent={strategy},{direction},{status},{pnl:.1f}")
    if not hb:
        print("hb=NONE")
        return
    ts = datetime.fromisoformat(hb[0])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    status = "OK" if age_min < 2 else ("WARN" if age_min < 15 else "STALE")
    print(f"hb={status},{age_min:.1f},{hb[2] or 'NONE'},{hb[1]}")


if __name__ == "__main__":
    main(sys.argv[1])

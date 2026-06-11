# watch.ps1 — 10-second daily health check for xauusd-scalper.
# Run manually in RDP session or schedule as a daily reminder.

$Root    = "C:\Users\BotVm\Desktop\xauusd-scalper"
$LogFile = Join-Path $Root "logs\scalper-wrapper.log"
$DB      = Join-Path $Root "data\scalper.db"
$Python  = "C:\Users\BotVm\AppData\Local\Programs\Python\Python311\python.exe"

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  xauusd-scalper watch" -ForegroundColor Cyan
Get-Date -Format "yyyy-MM-dd HH:mm:ss UTC ($(([System.TimeZoneInfo]::Local).StandardName))"
Write-Host ""

# ── Wrapper log tail ──────────────────────────────────────────────────────────
Write-Host "-- Last 3 lines of scalper-wrapper.log:" -ForegroundColor Yellow
if (Test-Path $LogFile) {
    Get-Content $LogFile -Tail 3
} else {
    Write-Host "  (log not found — bot not started yet)" -ForegroundColor DarkGray
}
Write-Host ""

# ── DB stats + heartbeat ──────────────────────────────────────────────────────
if (-not (Test-Path $DB)) {
    Write-Host "  DB not found: $DB" -ForegroundColor Red
    exit 0
}

$pyScript = @'
import sqlite3, sys
from datetime import datetime, timezone

db  = sys.argv[1]
con = sqlite3.connect(db, timeout=5)

open_pos = con.execute(
    "SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')"
).fetchone()[0]

today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
row   = con.execute(
    "SELECT COUNT(*), COALESCE(SUM(pnl_pips),0), COALESCE(SUM(pnl_usd),0) "
    "FROM trades WHERE status='CLOSED' AND exit_ts_utc LIKE ?",
    (today + '%',)
).fetchone()
today_cnt, today_pips, today_usd = row

total = con.execute(
    "SELECT COUNT(*), COALESCE(SUM(pnl_pips),0) FROM trades WHERE status='CLOSED'"
).fetchone()
total_cnt, total_pips = total

hb = con.execute("SELECT ts_utc, open_positions, last_scan_killzone FROM heartbeat WHERE id=1").fetchone()
con.close()

print(f"Open positions : {open_pos}")
print(f"Today trades   : {today_cnt}  pnl={today_pips:+.1f} pips  ({today_usd:+.2f} USD)")
print(f"All-time trades: {total_cnt}  pnl={total_pips:+.1f} pips")
print()

if hb:
    ts = datetime.fromisoformat(hb[0])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    kz      = hb[2] or "NONE"
    status  = "OK" if age_min < 2 else ("WARN" if age_min < 15 else "STALE")
    colour  = "" # plain text — callers can grep
    print(f"Heartbeat      : {status}  ({age_min:.1f} min ago)  kz={kz}  pos_mt5={hb[1]}")
    if age_min > 15:
        print()
        print("!! HEARTBEAT STALE > 15 min — bot may be hung or crashed.")
        print("   Restart options:")
        print("     schtasks /End  /TN xauusd-scalper")
        print("     schtasks /Run  /TN xauusd-scalper")
        print("   Or manually: start scripts\\start_scalper.bat")
else:
    print("Heartbeat      : NO ENTRY — bot has not written a heartbeat yet")
'@

& $Python -c $pyScript $DB

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

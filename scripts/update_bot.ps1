# update_bot.ps1 — Zero-ish-downtime git pull for xauusd-scalper.
#
# Safety guard: REFUSES to proceed if an open/partial position exists in the DB
# unless -Force is passed.  An execution bot mid-trade must not be interrupted.
#
# Flow:
#   1. Check for open positions in DB (fail unless -Force).
#   2. Find the running python PID (by CWD / command-line match).
#   3. git pull.
#   4. Stop old PID.
#   5. Start scripts\start_scalper.bat.
#   6. Tail wrapper log for 10 s to confirm startup.
#
# Usage:
#   .\scripts\update_bot.ps1          # safe (refuses with open position)
#   .\scripts\update_bot.ps1 -Force   # override (use only if position is known safe)

param(
    [switch]$Force
)

$Root    = "C:\Users\BotVm\Desktop\xauusd-scalper"
$DB      = Join-Path $Root "data\scalper.db"
$BatPath = Join-Path $Root "scripts\start_scalper.bat"
$Python  = "C:\Users\BotVm\AppData\Local\Programs\Python\Python311\python.exe"

# ── 1. Open-position guard ────────────────────────────────────────────────────
if (Test-Path $DB) {
    $openCount = & $Python -c @"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=5)
n = con.execute("SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')").fetchone()[0]
print(n)
"@ $DB

    $openCount = [int]($openCount.Trim())
    if ($openCount -gt 0) {
        if (-not $Force) {
            Write-Error "REFUSED: $openCount open/partial position(s) in DB.`nWait for the position to close, or pass -Force to override."
            exit 1
        }
        Write-Warning "-Force passed with $openCount open position(s). Proceeding anyway."
    } else {
        Write-Host "No open positions — safe to update." -ForegroundColor Green
    }
} else {
    Write-Host "DB not found ($DB) — assuming first deploy, continuing." -ForegroundColor Yellow
}

# ── 2. Find running scalper PID ───────────────────────────────────────────────
$oldPid = $null
$wmi = Get-WmiObject Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like "*main.py*" -and $_.CommandLine -notlike "*test*"
} | Where-Object {
    # extra check: working dir is the scalper root
    try { (Split-Path $_.ExecutablePath -Parent) -ne $null } catch { $false }
}

if ($wmi) {
    $oldPid = $wmi.ProcessId
    Write-Host "Found scalper python PID: $oldPid"
} else {
    Write-Host "No running scalper python found — will just start fresh." -ForegroundColor Yellow
}

# ── 3. git pull ───────────────────────────────────────────────────────────────
Push-Location $Root
Write-Host ""
Write-Host "-- git pull" -ForegroundColor Cyan
git pull
if ($LASTEXITCODE -ne 0) {
    Write-Error "git pull failed (exit $LASTEXITCODE). Aborting — old process untouched."
    Pop-Location
    exit 1
}
Pop-Location

# ── 4. Stop old PID ──────────────────────────────────────────────────────────
if ($oldPid) {
    Write-Host ""
    Write-Host "-- Stopping PID $oldPid..." -ForegroundColor Cyan
    Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        Write-Warning "PID $oldPid still alive after 2 s — continuing anyway."
    } else {
        Write-Host "PID $oldPid stopped."
    }
}

# ── 5. Start new instance ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "-- Starting $BatPath..." -ForegroundColor Cyan
Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$BatPath`"" -WindowStyle Minimized

# ── 6. Tail log for 10 s ──────────────────────────────────────────────────────
$LogFile = Join-Path $Root "logs\scalper-wrapper.log"
Write-Host "-- Tailing wrapper log for 10 s (Ctrl-C to stop early):" -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-Path $LogFile) {
        Get-Content $LogFile -Tail 5
        Write-Host "---"
    }
}

Write-Host ""
Write-Host "Update complete. Run .\scripts\watch.ps1 to confirm heartbeat." -ForegroundColor Green

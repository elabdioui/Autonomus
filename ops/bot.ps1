# ops/bot.ps1 — Single lifecycle entry point for xauusd-scalper.
#
# ISOLATION GUARD: This script MUST NEVER reference, stop, start, or restart
# xauusd-detector or xauusd-backend. Those belong to the signals bot (xauusd repo).
# Every process kill/start here filters on "*xauusd-scalper*main.py*" in CommandLine.
# MT5 checks filter on C:\MT5_scalper\ path only — never touch the signals terminal.
#
# Usage:
#   .\ops\bot.ps1 [status|start|stop|restart|update|logs|disconnect] [-Force] [-All]

param(
    [Parameter(Position=0)]
    [ValidateSet('status','start','stop','restart','update','logs','disconnect','')]
    [string]$Command = 'status',

    [switch]$Force,
    [switch]$All,
    [switch]$Wait,

    [Parameter(Position=1)]
    [string]$SubCommand = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root        = Split-Path $PSScriptRoot -Parent
$DB          = Join-Path $Root "data\scalper.db"
$LogWrapper  = Join-Path $Root "logs\scalper-wrapper.log"
$LogScalper  = Join-Path $Root "logs\scalper.log"
$BatPath     = Join-Path $Root "scripts\start_scalper.bat"
$Python      = Join-Path $Root ".venv\Scripts\python.exe"
$TaskName    = "xauusd-scalper"
$SvcName     = "xauusd-scalper-dashboard"
$MT5Path     = "C:\MT5_scalper\terminal64.exe"   # configurable — MUST stay under C:\MT5_scalper\

# ── Colour helpers ─────────────────────────────────────────────────────────────
function Write-OK   { param($msg) Write-Host "  [OK]   $msg" -ForegroundColor Green  }
function Write-SKIP { param($msg) Write-Host "  [SKIP] $msg" -ForegroundColor DarkGray }
function Write-FAIL { param($msg) Write-Host "  [FAIL] $msg" -ForegroundColor Red   }
function Write-INFO { param($msg) Write-Host "  [INFO] $msg" -ForegroundColor Cyan  }
function Write-WARN { param($msg) Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

function Write-Banner {
    param([string]$title)
    Write-Host ""
    Write-Host ("━" * 60) -ForegroundColor Cyan
    Write-Host "  xauusd-scalper — $title" -ForegroundColor Cyan
    Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') UTC" -ForegroundColor DarkGray
    Write-Host ("━" * 60) -ForegroundColor Cyan
}

# ── Isolation-scoped process finder ───────────────────────────────────────────
function Get-ScalperProcess {
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*xauusd-scalper*" -and $_.CommandLine -like "*main.py*" }
}

function Get-ScalperMT5 {
    Get-Process -Name "terminal64" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "C:\MT5_scalper\*" }
}

function Stop-ScalperProcess {
    $procs = Get-ScalperProcess
    if ($procs) {
        foreach ($p in $procs) {
            Write-INFO "Stopping scalper python PID $($p.ProcessId)..."
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
        $remaining = Get-ScalperProcess
        if ($remaining) { Write-WARN "PID(s) still alive after 2 s." }
        else             { Write-OK  "Scalper python stopped." }
    } else {
        Write-SKIP "No scalper python process found."
    }
}

# ── Dashboard service helpers ──────────────────────────────────────────────────
function Get-DashboardState {
    try {
        $svc = Get-Service -Name $SvcName -ErrorAction Stop
        return $svc.Status
    } catch {
        return $null   # not installed
    }
}

function Stop-Dashboard {
    $state = Get-DashboardState
    if ($null -eq $state)            { Write-SKIP "Dashboard service not installed." ; return }
    if ($state -eq 'Stopped')        { Write-SKIP "Dashboard already stopped."       ; return }
    Stop-Service -Name $SvcName -Force -ErrorAction SilentlyContinue
    Write-OK "Dashboard service stopped."
}

function Start-Dashboard {
    $state = Get-DashboardState
    if ($null -eq $state)     { Write-SKIP "Dashboard service not installed — skipping." ; return }
    if ($state -eq 'Running') { Write-SKIP "Dashboard already running."                  ; return }
    Start-Service -Name $SvcName -ErrorAction SilentlyContinue
    Write-OK "Dashboard service started."
}

# ── DB stats inline python ────────────────────────────────────────────────────
$PyDbScript = @'
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

hb = con.execute(
    "SELECT ts_utc, open_positions, last_scan_killzone FROM heartbeat WHERE id=1"
).fetchone()
con.close()

print(f"open_pos={open_pos}")
print(f"today={today_cnt},{today_pips:.1f},{today_usd:.2f}")
print(f"total={total_cnt},{total_pips:.1f}")
if hb:
    ts = datetime.fromisoformat(hb[0])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    kz      = hb[2] or "NONE"
    status  = "OK" if age_min < 2 else ("WARN" if age_min < 15 else "STALE")
    print(f"hb={status},{age_min:.1f},{kz},{hb[1]}")
else:
    print("hb=NONE")
'@

$PyOpenCount = @'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=5)
n = con.execute("SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')").fetchone()[0]
print(n)
'@

# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

function Invoke-Status {
    Write-Banner "status"
    $ok = $true

    # ── Scheduled task ────────────────────────────────────────────────────────
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-OK   "Task '$TaskName': $($task.State)" }
    else        { Write-FAIL "Task '$TaskName': not registered" ; $ok = $false }

    # ── Scalper python process ────────────────────────────────────────────────
    $proc = Get-ScalperProcess
    if ($proc) { Write-OK   "Scalper python: running (PID $($proc.ProcessId))" }
    else        { Write-FAIL "Scalper python: not running"                       ; $ok = $false }

    # ── Wrapper log ───────────────────────────────────────────────────────────
    if (Test-Path $LogWrapper) {
        $last    = Get-Content $LogWrapper -Tail 1
        $modAge  = ((Get-Date) - (Get-Item $LogWrapper).LastWriteTime).TotalMinutes
        $ageStr  = "$([math]::Round($modAge,1)) min ago"
        if ($modAge -lt 15) { Write-OK   "Wrapper log: ALIVE ($ageStr) | $last" }
        else                 { Write-WARN "Wrapper log: OLD   ($ageStr) | $last" }
    } else {
        Write-SKIP "Wrapper log not found (bot not started yet)."
    }

    # ── MT5 scalper terminal ──────────────────────────────────────────────────
    $mt5 = Get-ScalperMT5
    if ($mt5) { Write-OK   "MT5 scalper: running (PID $($mt5.Id))" }
    else       { Write-WARN "MT5 scalper: not detected at C:\MT5_scalper\" }

    # ── Dashboard service ─────────────────────────────────────────────────────
    $dashState = Get-DashboardState
    if ($null -eq $dashState)      { Write-SKIP "Dashboard service: not installed (optional)" }
    elseif ($dashState -eq 'Running') { Write-OK   "Dashboard service: Running" }
    else                           { Write-WARN "Dashboard service: $dashState" }

    # ── DB section ────────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host "  -- Database" -ForegroundColor DarkGray
    if (-not (Test-Path $DB)) {
        Write-SKIP "DB not found: $DB"
    } elseif (-not (Test-Path $Python)) {
        Write-WARN "Venv python not found ($Python) — skipping DB query."
    } else {
        $raw = & $Python -c $PyDbScript $DB 2>&1
        $hbFail = $false
        foreach ($line in $raw) {
            if ($line -match "^open_pos=(.+)") {
                Write-INFO "Open/partial positions : $($Matches[1])"
            } elseif ($line -match "^today=(\d+),([\d.\-]+),([\d.\-]+)") {
                Write-INFO "Today trades           : $($Matches[1])  pnl=$($Matches[2]) pips  ($($Matches[3]) USD)"
            } elseif ($line -match "^total=(\d+),([\d.\-]+)") {
                Write-INFO "All-time trades        : $($Matches[1])  pnl=$($Matches[2]) pips"
            } elseif ($line -match "^hb=NONE") {
                Write-WARN "Heartbeat              : NO ENTRY — bot has not written a heartbeat yet"
                $hbFail = $true ; $ok = $false
            } elseif ($line -match "^hb=(\w+),([\d.]+),(\S+),(\S+)") {
                $hbStatus = $Matches[1] ; $hbAge = $Matches[2] ; $hbKz = $Matches[3] ; $hbPos = $Matches[4]
                $hbMsg = "Heartbeat              : $hbStatus  ($hbAge min ago)  kz=$hbKz  pos_mt5=$hbPos"
                if ($hbStatus -eq "OK")   { Write-OK   $hbMsg }
                elseif ($hbStatus -eq "WARN") { Write-WARN $hbMsg }
                else { Write-FAIL $hbMsg ; $hbFail = $true ; $ok = $false }
            }
        }
    }

    # ── Git HEAD ──────────────────────────────────────────────────────────────
    Write-Host ""
    $gitHead = git -C $Root log -1 --oneline 2>&1
    Write-INFO "Git HEAD: $gitHead"

    # ── Summary ───────────────────────────────────────────────────────────────
    Write-Host ""
    if ($ok) { Write-Host "  FINAL: OK"   -ForegroundColor Green }
    else      { Write-Host "  FINAL: FAIL" -ForegroundColor Red  }
    Write-Host ""

    if (-not $ok) { exit 1 }
}

function Invoke-Start {
    Write-Banner "start"

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Write-INFO "Starting scheduled task '$TaskName'..."
        Start-ScheduledTask -TaskName $TaskName
        Write-OK "Task start requested."
    } else {
        Write-WARN "Task '$TaskName' not registered — falling back to bat file."
        if (Test-Path $BatPath) {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$BatPath`"" -WindowStyle Minimized
            Write-OK "Launched $BatPath."
        } else {
            Write-FAIL "Bat not found: $BatPath"
            exit 1
        }
    }

    Start-Dashboard

    Write-INFO "Waiting 15 s for process to appear..."
    Start-Sleep -Seconds 15
    Invoke-Status
}

function Invoke-Stop {
    Write-Banner "stop"
    Stop-ScalperProcess
    Stop-Dashboard
}

function Invoke-Restart {
    Write-Banner "restart"
    Stop-ScalperProcess
    if ($All) { Stop-Dashboard }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $TaskName
        Write-OK "Task '$TaskName' start requested."
    } else {
        if (Test-Path $BatPath) {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$BatPath`"" -WindowStyle Minimized
            Write-OK "Launched $BatPath."
        } else {
            Write-FAIL "Bat not found and task not registered."
            exit 1
        }
    }

    if ($All) { Start-Dashboard }

    Write-INFO "Waiting 15 s..."
    Start-Sleep -Seconds 15
    Invoke-Status
}

function Invoke-Update {
    Write-Banner "update"

    # 1. Open-position guard
    if (Test-Path $DB) {
        if (-not (Test-Path $Python)) { Write-FAIL "Venv python not found — run install.ps1 first." ; exit 1 }
        $openCount = (& $Python -c $PyOpenCount $DB 2>&1) | Select-Object -Last 1
        $openCount = [int]($openCount.Trim())
        if ($openCount -gt 0) {
            if (-not $Force) {
                Write-FAIL "REFUSED: $openCount open/partial position(s) in DB."
                Write-Host "         Wait for the position to close, or pass -Force to override." -ForegroundColor Red
                exit 1
            }
            Write-WARN "-Force passed with $openCount open position(s). Proceeding."
        } else {
            Write-OK "No open positions — safe to update."
        }
    } else {
        Write-WARN "DB not found — assuming first deploy, continuing."
    }

    # 2. git pull
    Write-INFO "Running git pull..."
    $prevHead = git -C $Root rev-parse HEAD 2>&1
    git -C $Root pull
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "git pull failed (exit $LASTEXITCODE). Aborting — old process untouched."
        exit 1
    }
    Write-OK "git pull succeeded."

    # 3. pip install if requirements changed
    $reqChanged = git -C $Root diff "$prevHead" HEAD --name-only 2>&1 | Where-Object { $_ -eq "requirements.txt" }
    if ($reqChanged) {
        Write-INFO "requirements.txt changed — running pip install..."
        & $Python -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { Write-WARN "pip install returned non-zero — check output." }
        else                      { Write-OK   "pip install complete." }
    } else {
        Write-SKIP "requirements.txt unchanged."
    }

    # 4. Kill old process, restart task, tail log
    Stop-ScalperProcess

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $TaskName
        Write-OK "Task '$TaskName' start requested."
    } else {
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$BatPath`"" -WindowStyle Minimized
        Write-OK "Launched $BatPath."
    }

    Write-INFO "Tailing wrapper log for 10 s..."
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-Path $LogWrapper) {
            Get-Content $LogWrapper -Tail 5
            Write-Host "---"
        }
    }

    Invoke-Status
}

function Invoke-Logs {
    Write-Banner "logs $SubCommand"

    if ($SubCommand -eq 'stats') {
        if (-not (Test-Path $LogScalper)) {
            Write-SKIP "scalper.log not found."
            return
        }
        Write-INFO "Grep: DETECTED|FILLED|PLACED|CLOSED|TP1_PARTIAL|BE_|FRIDAY_FLAT|TIMEOUT_CLOSE|SKIPPED_"
        Get-Content $LogScalper |
            Where-Object { $_ -match 'DETECTED|FILLED|PLACED|CLOSED|TP1_PARTIAL|BE_|FRIDAY_FLAT|TIMEOUT_CLOSE|SKIPPED_' }
    } else {
        if (-not (Test-Path $LogWrapper)) {
            Write-SKIP "Wrapper log not found."
            return
        }
        if ($Wait) {
            Get-Content $LogWrapper -Tail 30 -Wait
        } else {
            Get-Content $LogWrapper -Tail 30
        }
    }
}

function Invoke-Disconnect {
    Write-Banner "disconnect"
    Write-INFO "Querying active RDP session..."

    $sessionLine = query session 2>&1 | Where-Object { $_ -match '^\s*>\s*rdp' -or $_ -match 'Active' } | Select-Object -First 1
    if (-not $sessionLine) {
        Write-WARN "No active RDP session found. Are you already on the console?"
        return
    }

    $sessionId = ($sessionLine -split '\s+' | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
    if (-not $sessionId) {
        Write-WARN "Could not parse session ID from: $sessionLine"
        return
    }

    Write-INFO "Transferring session $sessionId to console (keeping interactive tasks alive)..."
    Write-INFO "Note: one disconnect covers both bots — they share the same Windows session."
    tscon $sessionId /dest:console
    Write-OK "tscon issued."
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
switch ($Command) {
    'status'     { Invoke-Status }
    'start'      { Invoke-Start }
    'stop'       { Invoke-Stop }
    'restart'    { Invoke-Restart }
    'update'     { Invoke-Update }
    'logs'       { Invoke-Logs }
    'disconnect' { Invoke-Disconnect }
    default      { Invoke-Status }
}

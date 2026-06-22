# ops/install.ps1 — Idempotent bootstrap for xauusd-scalper on botvm.
#
# MUST run as Administrator.
# Safe to re-run: every step prints [OK] or [SKIP], no destructive action on repeat.
#
# Usage:
#   .\ops\install.ps1 [-User BotVm]

param(
    [string]$User = "BotVm"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root     = Split-Path $PSScriptRoot -Parent
$BatPath  = Join-Path $Root "scripts\start_scalper.bat"
$VenvDir  = Join-Path $Root ".venv"
$Python   = Join-Path $VenvDir "Scripts\python.exe"
$Req      = Join-Path $Root "requirements.txt"
$EnvFile  = Join-Path $Root ".env"
$TaskName = "xauusd-scalper"
$SvcName  = "xauusd-scalper-dashboard"
$MT5Expected = "C:\MT5_scalper\"

$NssmCandidates = @(
    "C:\tools\nssm-2.24-101-g897c7ad\win64\nssm.exe",
    "C:\tools\nssm\win64\nssm.exe",
    "C:\nssm\nssm.exe",
    (Get-Command nssm -ErrorAction SilentlyContinue)?.Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

function Write-OK   { param($msg) Write-Host "  [OK]   $msg" -ForegroundColor Green  }
function Write-SKIP { param($msg) Write-Host "  [SKIP] $msg" -ForegroundColor DarkGray }
function Write-FAIL { param($msg) Write-Host "  [FAIL] $msg" -ForegroundColor Red   }
function Write-INFO { param($msg) Write-Host "  [INFO] $msg" -ForegroundColor Cyan  }
function Write-WARN { param($msg) Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

function Write-Step { param($n,$title) Write-Host "`n  ── Step $n : $title" -ForegroundColor White }

Write-Host ""
Write-Host ("━" * 60) -ForegroundColor Cyan
Write-Host "  xauusd-scalper — install.ps1" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host ("━" * 60) -ForegroundColor Cyan

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — PREFLIGHT
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 1 "Preflight checks"

$failures = @()

# Python ≥ 3.11 on PATH
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    $pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1
    if ([version]$pyVer -ge [version]"3.11") { Write-OK "Python $pyVer on PATH." }
    else { $failures += "Python ≥ 3.11 required (found $pyVer)." }
} else {
    $failures += "Python not found on PATH."
}

# git
if (Get-Command git -ErrorAction SilentlyContinue) { Write-OK "git on PATH." }
else { $failures += "git not found on PATH." }

# .env
if (Test-Path $EnvFile) { Write-OK ".env present." }
else { $failures += ".env not found — copy .env.example and fill in credentials." }

# Repo root sanity
if (Test-Path (Join-Path $Root "main.py")) { Write-OK "Repo root detected: $Root" }
else { $failures += "main.py not found — $Root does not look like the scalper repo root." }

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-FAIL "Preflight FAILED. Fix these before re-running:"
    $failures | ForEach-Object { Write-Host "    • $_" -ForegroundColor Red }
    exit 1
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — VENV + DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 2 "Python virtual environment"

if (Test-Path $Python) {
    Write-SKIP "Venv already exists at $VenvDir."
} else {
    Write-INFO "Creating venv at $VenvDir..."
    & python -m venv $VenvDir
    Write-OK "Venv created."
}

Write-INFO "Running pip install -r requirements.txt..."
& $Python -m pip install --quiet -r $Req
if ($LASTEXITCODE -ne 0) { Write-FAIL "pip install failed." ; exit 1 }
Write-OK "pip install complete."

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — MT5 PATH CHECK
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 3 "MT5 terminal path"

# Read MT5_TERMINAL_PATH from .env
$mt5PathEnv = $null
Get-Content $EnvFile | Where-Object { $_ -match '^MT5_TERMINAL_PATH\s*=\s*(.+)$' } | ForEach-Object {
    $mt5PathEnv = $Matches[1].Trim().Trim('"').Trim("'")
}

if (-not $mt5PathEnv) {
    Write-WARN "MT5_TERMINAL_PATH not set in .env — skipping path verification."
} else {
    if ($mt5PathEnv -notlike "$MT5Expected*") {
        Write-Host ""
        Write-WARN "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        Write-WARN "MT5_TERMINAL_PATH does NOT start with $MT5Expected"
        Write-WARN "Value in .env: $mt5PathEnv"
        Write-WARN "This may point to the SIGNALS terminal — verify before continuing!"
        Write-WARN "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        Write-Host ""
    } else {
        Write-OK "MT5_TERMINAL_PATH is under $MT5Expected"
    }

    if (Test-Path $mt5PathEnv) { Write-OK "terminal64.exe exists on disk." }
    else { Write-WARN "terminal64.exe not found at: $mt5PathEnv — install MT5 portable first." }

    $mt5Running = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$MT5Expected*" }
    if ($mt5Running) { Write-OK "MT5 scalper terminal is running (PID $($mt5Running.Id))." }
    else             { Write-WARN "MT5 scalper terminal is NOT running — launch and log in manually." }
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — ACCOUNT IDENTITY CHECK
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 4 "MT5 account identity"

$acctScript = @'
import sys
sys.path.insert(0, sys.argv[1])
try:
    import mt5_client
    import MetaTrader5 as mt5
    if not mt5_client.is_connected():
        print("NOT_CONNECTED")
        sys.exit(0)
    info = mt5.account_info()
    if info is None:
        print("NO_ACCOUNT_INFO")
    else:
        print(f"LOGIN={info.login} SERVER={info.server}")
    mt5.shutdown()
except Exception as e:
    print(f"ERROR: {e}")
'@

$acctResult = & $Python -c $acctScript $Root 2>&1
if ($acctResult -match "NOT_CONNECTED|NO_ACCOUNT_INFO|ERROR") {
    Write-WARN "Could not retrieve MT5 account info: $acctResult"
    Write-WARN "Ensure MT5 scalper terminal is running and logged in, then re-run install.ps1."
} else {
    Write-INFO "MT5 account: $acctResult"
    Write-Host ""
    Write-Host "  Is this the SCALPER demo account (not the signals bot account)? [Y/n] " -ForegroundColor Yellow -NoNewline
    $confirm = Read-Host
    if ($confirm -and $confirm.Trim().ToLower() -eq 'n') {
        Write-FAIL "Aborted — fix MT5_TERMINAL_PATH in .env to point to C:\MT5_scalper\terminal64.exe."
        exit 1
    }
    Write-OK "Account identity confirmed."
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — TASK SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 5 "Task Scheduler — $TaskName"

if (-not (Test-Path $BatPath)) {
    Write-FAIL "Bat not found: $BatPath — cannot register task."
    exit 1
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-SKIP "Task '$TaskName' already registered (state: $($existingTask.State)). Replacing."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`""
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -RestartOnFailure `
    -RestartInterval  (New-TimeSpan -Minutes 1) `
    -RestartCount     3 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Principal $principal `
    -Settings  $settings `
    -Force | Out-Null

Write-OK "Task '$TaskName' registered (AtLogOn/$User, RunLevel Highest, restart 1 min ×3)."

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — DASHBOARD SERVICE (OPTIONAL)
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 6 "Dashboard NSSM service (optional)"

if (-not $NssmCandidates) {
    Write-SKIP "NSSM not found — dashboard service skipped. Install NSSM to enable."
} else {
    $Nssm = $NssmCandidates
    Write-INFO "NSSM found: $Nssm"

    $LogDir = Join-Path $Root "logs"
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

    $existingSvc = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
    if ($existingSvc) {
        Write-SKIP "Service '$SvcName' already installed (status: $($existingSvc.Status))."
    } else {
        & $Nssm install $SvcName $Python "-m" "uvicorn" "reporting.dashboard:app" "--host" "127.0.0.1" "--port" "8080" "--no-access-log"
        & $Nssm set $SvcName AppDirectory $Root
        & $Nssm set $SvcName AppStdout (Join-Path $LogDir "dashboard.log")
        & $Nssm set $SvcName AppStderr (Join-Path $LogDir "dashboard-error.log")
        & $Nssm set $SvcName AppRotateFiles 1
        & $Nssm set $SvcName AppRotateOnline 1
        & $Nssm set $SvcName AppRotateBytes 10485760
        Write-OK "Service '$SvcName' installed (port 8080)."
    }
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — FINAL STATUS
# ══════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host ("━" * 60) -ForegroundColor Cyan
Write-Host "  Install complete — running status check" -ForegroundColor Cyan
Write-Host ("━" * 60) -ForegroundColor Cyan

& (Join-Path $PSScriptRoot "bot.ps1") status

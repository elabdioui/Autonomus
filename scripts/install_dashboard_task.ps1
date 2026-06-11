# install_dashboard_task.ps1 — Install xauusd-scalper-dashboard as an NSSM service.
#
# The dashboard is FastAPI/uvicorn with no MT5 dependency, so it CAN run in
# session 0 (unlike the scalper itself).  NSSM gives auto-restart + log rotation.
#
# Prerequisite: NSSM already present on the VPS (used by xauusd-backend).
# Usage (Admin PS):  .\scripts\install_dashboard_task.ps1 [-NssmPath <path>]

param(
    [string]$NssmPath  = "C:\tools\nssm-2.24-101-g897c7ad\win64\nssm.exe",
    [string]$PythonPath = "C:\Users\BotVm\AppData\Local\Programs\Python\Python311\python.exe",
    [string]$ProjectRoot = "C:\Users\BotVm\Desktop\xauusd-scalper"
)

$SvcName = "xauusd-scalper-dashboard"
$LogDir  = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Path $NssmPath)) {
    Write-Error "NSSM not found: $NssmPath"
    exit 1
}

# Remove existing service if present
$existing = & sc.exe query $SvcName 2>&1
if ($existing -notmatch "does not exist") {
    Write-Host "Stopping and removing existing '$SvcName'..."
    & $NssmPath stop   $SvcName confirm 2>&1 | Out-Null
    & $NssmPath remove $SvcName confirm 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

& $NssmPath install $SvcName $PythonPath `
    "-m uvicorn reporting.dashboard:app --host 127.0.0.1 --port 8080 --no-access-log"
& $NssmPath set $SvcName AppDirectory        $ProjectRoot
& $NssmPath set $SvcName AppStdout          (Join-Path $LogDir "dashboard.log")
& $NssmPath set $SvcName AppStderr          (Join-Path $LogDir "dashboard-error.log")
& $NssmPath set $SvcName AppRotateFiles     1
& $NssmPath set $SvcName AppRotateBytes     5242880    # 5 MB
& $NssmPath set $SvcName Start              SERVICE_AUTO_START
& $NssmPath set $SvcName ObjectName         LocalSystem

Write-Host "Service '$SvcName' installed. Starting..."
& $NssmPath start $SvcName
Start-Sleep -Seconds 3

$status = & sc.exe query $SvcName | Select-String "STATE"
Write-Host $status
Write-Host ""
Write-Host "Dashboard: http://127.0.0.1:8080  (RDP browser or SSH tunnel)"
Write-Host "Logs     : $LogDir\dashboard.log"

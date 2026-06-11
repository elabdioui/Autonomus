# install_task.ps1 — Register xauusd-scalper in Task Scheduler.
#
# MUST run as Administrator on botvm.
# Mirrors the existing signals-bot Task Scheduler pattern:
#   - Logon trigger for BotVm (interactive session — required for MT5 IPC).
#   - NEVER session-0 / LocalSystem: MT5 named-pipe IPC only works in the
#     session where the terminal is running.
#
# Usage:
#   .\scripts\install_task.ps1 [-User BotVm] [-Force]

param(
    [string]$User  = "BotVm",
    [switch]$Force
)

$TaskName = "xauusd-scalper"
$BatPath  = "C:\Users\BotVm\Desktop\xauusd-scalper\scripts\start_scalper.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "Bat not found: $BatPath  — verify repo path."
    exit 1
}

# Remove existing task silently before re-registering
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if (-not $Force) {
        Write-Host "Task '$TaskName' already exists. Use -Force to overwrite."
        exit 0
    }
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

Write-Host "Task '$TaskName' registered."
Write-Host "  Trigger  : at logon of $User (interactive session)"
Write-Host "  Action   : $BatPath"
Write-Host "  On fail  : restart every 1 min, up to 3 attempts"
Write-Host ""
Write-Host "Verify: Get-ScheduledTask -TaskName '$TaskName' | Select-Object State,LastRunTime"

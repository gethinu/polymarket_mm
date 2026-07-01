[CmdletBinding()]
param(
  [string]$TaskName = "PolymarketGateMonitorDaily",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "08:10",
  [string]$DiscordWebhookEnv = "CLOBBOT_DISCORD_WEBHOOK_URL",
  [switch]$NoDiscord,
  [switch]$RunNow
)

# Lean daily gate monitor: refresh the register snapshot, then run the capital-gate
# alarm. The alarm only pushes to Discord on a state TRANSITION (capital gate or the
# no-longshot practical-judgment status changing), so daily cadence will not spam.
#
# Deliberately does NOT reuse install_morning_status_daily_task.ps1: that task
# hardcodes -NoLongshotPracticalDecisionDate 2026-03-02 (the pre-re-baseline rotted
# date) and would re-pin the expired judgment window. This monitor passes no fixed
# date, so the gate keeps its fresh today+35 anchor.
#   - S4U principal => runs whether the user is logged on or not.
#   - Read-only: render + alarm only; never places orders.

$ErrorActionPreference = "Stop"
$baseDir = (Resolve-Path $RepoRoot).Path
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$alarmCmd = "python scripts/check_strategy_gate_alarm.py"
if (-not $NoDiscord.IsPresent) {
  $alarmCmd += " --discord --discord-webhook-env $DiscordWebhookEnv"
}
$inner = "Set-Location '$baseDir'; python scripts/render_strategy_register_snapshot.py; $alarmCmd"
$actionArgString = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $inner + '"'

$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $actionArgString -WorkingDirectory $baseDir
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$desc = "Daily Polymarket capital-gate monitor: refresh register snapshot + gate alarm (Discord on transition). Read-only."

try {
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
}
catch {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
}

if ($RunNow.IsPresent) { Start-ScheduledTask -TaskName $TaskName }

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

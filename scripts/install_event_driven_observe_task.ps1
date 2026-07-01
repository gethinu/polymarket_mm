[CmdletBinding()]
param(
  [string]$TaskName = "PolymarketEventDrivenObserve",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "",
  [int]$IntervalMinutes = 5,
  [int]$DurationDays = 3650,
  [int]$MaxPages = 12,
  [int]$PollSec = 120,
  [double]$MinEdgeCents = 0.8,
  [int]$MaxDaysToEnd = 180,
  [int]$TopN = 20,
  [int]$SignalCooldownSec = 7200,
  [switch]$RunNow
)

# Registers a reboot-durable scheduled task that keeps the event-driven observe
# poller alive. The poller (polymarket_event_driven_observe.py) is hard observe-only
# ("no order placement") and loops forever on --poll-sec, so it needs no wrapper /
# CLOBBOT_EXECUTE guard; the task just keeps it running.
#   - S4U principal => runs whether the user is logged on or not, no stored secret.
#   - Once + RepetitionInterval + MultipleInstances=IgnoreNew => single instance;
#     re-fires restart it only if it has died.
# PythonExe is resolved to an absolute path at install time so S4U (which may have a
# reduced PATH) launches the right interpreter.

$ErrorActionPreference = "Stop"
$baseDir = (Resolve-Path $RepoRoot).Path
$scriptPath = Join-Path $baseDir "scripts\polymarket_event_driven_observe.py"
if (-not (Test-Path $scriptPath)) { throw "Script not found: $scriptPath" }

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($null -eq $cmd) { throw "python not found on PATH; pass -PythonExe <abs path>" }
  $PythonExe = $cmd.Source
}
if (-not (Test-Path $PythonExe)) { throw "PythonExe not found: $PythonExe" }

$actionArgs = @(
  ('"{0}"' -f $scriptPath),
  "--max-pages", ([string]$MaxPages),
  "--poll-sec", ([string]$PollSec),
  "--min-edge-cents", ([string]$MinEdgeCents),
  "--max-days-to-end", ([string]$MaxDaysToEnd),
  "--top-n", ([string]$TopN),
  "--signal-cooldown-sec", ([string]$SignalCooldownSec),
  "--signal-state-file", "logs/event-driven-observe-signal-state.json"
)
$actionArgString = ($actionArgs -join " ")

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgString -WorkingDirectory $baseDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$desc = "Keep the Polymarket event-driven observe poller alive (observe-only, no order placement)."

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

[CmdletBinding()]
param(
  [string]$TaskName = "PolymarketGammaEventPairObserve",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [int]$IntervalMinutes = 5,
  [int]$DurationDays = 3650,
  [int]$RescanDelaySec = 300,
  [switch]$RunNow
)

# Registers a reboot-durable scheduled task that keeps the gamma-active/event-pair
# observe wrapper alive. Mirrors install_weather_arb_observe_task.ps1:
#   - S4U principal => runs whether the user is logged on or not, no stored secret.
#   - Once + RepetitionInterval + MultipleInstances=IgnoreNew => the trigger re-fires
#     every $IntervalMinutes; if the wrapper is alive (its mutex is held) the new run
#     is ignored, and if it died the next fire restarts it. Observe-only: the wrapper
#     hard-sets CLOBBOT_EXECUTE=0.

$ErrorActionPreference = "Stop"
$baseDir = (Resolve-Path $RepoRoot).Path
$runnerPath = Join-Path $baseDir "scripts\run_gamma_eventpair_observe.ps1"
if (-not (Test-Path $runnerPath)) { throw "Runner not found: $runnerPath" }

$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$actionArgs = @(
  "-NoLogo", "-NoProfile", "-NonInteractive",
  "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-RescanDelaySec", ([string]$RescanDelaySec)
)
$actionArgString = ($actionArgs -join " ")

$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $actionArgString -WorkingDirectory $baseDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$desc = "Keep the Polymarket gamma event-pair observe monitor alive (observe-only)."

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

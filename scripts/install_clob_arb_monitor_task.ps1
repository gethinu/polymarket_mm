param(
  [string]$TaskName = "PolymarketClobArbMonitor",
  [int]$IntervalMinutes = 2,
  [int]$DurationDays = 3650
)

$ErrorActionPreference = "Stop"

$baseDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $baseDir "scripts\\run_clob_arb_monitor.ps1"
$hiddenLauncherPath = Join-Path $baseDir "scripts\\run_clob_arb_monitor_hidden.vbs"
if (-not (Test-Path $scriptPath)) {
  throw "Script not found: $scriptPath"
}
if (-not (Test-Path $hiddenLauncherPath)) {
  throw "Hidden launcher not found: $hiddenLauncherPath"
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"{0}"' -f $hiddenLauncherPath)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Run Polymarket CLOB arbitrage monitor periodically" -Force | Out-Null
}
catch {
  # Fallback for environments where S4U registration isn't available.
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Run Polymarket CLOB arbitrage monitor periodically" -Force | Out-Null
}

schtasks /Query /TN $TaskName /V /FO LIST

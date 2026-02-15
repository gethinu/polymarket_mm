param(
  [string]$TaskName = "SimmerPingPong",
  [string]$Pythonw = "C:\Users\stair\AppData\Local\Programs\Python\Python311\pythonw.exe",
  [string]$ScriptPath = "C:\Repos\polymarket_mm\scripts\simmer_pingpong_mm.py"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Pythonw)) {
  throw "pythonw.exe not found: $Pythonw"
}
if (-not (Test-Path $ScriptPath)) {
  throw "Script not found: $ScriptPath"
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}"' -f $ScriptPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Run Simmer ping-pong bot (venue=simmer demo) on logon" -Force | Out-Null
}
catch {
  Write-Host "Failed to register scheduled task: $($_.Exception.Message)"
  Write-Host "Try running PowerShell as Administrator and re-run this script."
  throw
}

Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult


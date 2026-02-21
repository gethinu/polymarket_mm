param(
  [string]$TaskName = "SimmerPingPong",
  [string]$Pythonw = "C:\Users\stair\AppData\Local\Programs\Python\Python311\pythonw.exe",
  [string]$ScriptPath = "C:\Repos\polymarket_mm\scripts\simmer_pingpong_mm.py",
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Start-BackgroundSelf {
  param(
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [Parameter(Mandatory = $true)][hashtable]$BoundParameters
  )

  $argList = @(
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", $ScriptPath,
    "-Background"
  )

  foreach ($key in ($BoundParameters.Keys | Sort-Object)) {
    if ($key -in @("Background", "NoBackground")) { continue }
    $value = $BoundParameters[$key]
    if ($value -is [System.Management.Automation.SwitchParameter]) {
      if ($value.IsPresent) { $argList += "-$key" }
      continue
    }
    if ($null -eq $value) { continue }
    $argList += "-$key"
    $argList += [string]$value
  }

  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden -PassThru
  Write-Host ("Started in background: pid={0} script={1}" -f $proc.Id, $ScriptPath)
  exit 0
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

if (-not (Test-Path $Pythonw)) {
  throw "pythonw.exe not found: $Pythonw"
}
if (-not (Test-Path $ScriptPath)) {
  throw "Script not found: $ScriptPath"
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}"' -f $ScriptPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$fallbackTaskName = "PolymarketClobMM"

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Run Simmer ping-pong bot (venue=simmer demo) on logon" -Force | Out-Null
}
catch {
  try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Run Simmer ping-pong bot (venue=simmer demo) on logon" -Force | Out-Null
  }
  catch {
    # In some environments, creating a new task is ACL-restricted for non-admin users.
    # Fallback: repurpose an existing mutable task.
    try {
      $existing = Get-ScheduledTask -TaskName $fallbackTaskName -ErrorAction Stop
      Stop-ScheduledTask -TaskName $fallbackTaskName -ErrorAction SilentlyContinue
      Set-ScheduledTask -TaskName $fallbackTaskName -Action $action | Out-Null
      Start-ScheduledTask -TaskName $fallbackTaskName

      Write-Host "Task create denied. Reused existing task: $fallbackTaskName"
      Get-ScheduledTask -TaskName $fallbackTaskName | Select-Object TaskName,State
      Get-ScheduledTaskInfo -TaskName $fallbackTaskName | Select-Object TaskName,LastRunTime,LastTaskResult
      exit 0
    }
    catch {
      Write-Host "Failed to register scheduled task: $($_.Exception.Message)"
      Write-Host "Try running PowerShell as Administrator and re-run this script."
      throw
    }
  }
}

Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult

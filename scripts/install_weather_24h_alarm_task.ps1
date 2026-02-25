[CmdletBinding()]
param(
  [string]$TaskName = "PolymarketWeather24hAlarm",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PowerShellExe = "powershell.exe",
  [string]$AlarmAt = "",
  [string]$Message = "Polymarket weather 24h observe completed. Check logs.",
  [string]$LogFile = "logs/alarm_weather24h.log",
  [string]$MarkerFile = "logs/alarm_weather24h.marker",
  [int]$MsgTimeoutSec = 3,
  [switch]$DisableMsg,
  [switch]$RunNow,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1 -NoBackground [-AlarmAt '2026-02-24 09:00:00'] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1 -NoBackground -?"
}

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
    "-File", ('"{0}"' -f $ScriptPath.Replace('"', '\"')),
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
    $text = [string]$value
    if ($text -match '[\s"]') {
      $text = ('"{0}"' -f $text.Replace('"', '\"'))
    }
    $argList += $text
  }

  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden -PassThru
  Write-Host ("Started in background: pid={0} script={1}" -f $proc.Id, $ScriptPath)
  exit 0
}

function Parse-AlarmTime {
  param([string]$Text)
  if ([string]::IsNullOrWhiteSpace($Text)) {
    return (Get-Date).AddHours(24)
  }
  try {
    return [datetime]::Parse($Text, [System.Globalization.CultureInfo]::InvariantCulture)
  } catch {
  }
  try {
    return [datetime]::Parse($Text)
  } catch {
    throw "Invalid AlarmAt format: $Text"
  }
}

function Quote-Arg {
  param([Parameter(Mandatory = $true)][string]$Text)
  return ('"{0}"' -f $Text.Replace('"', '\"'))
}

$invLine = [string]$MyInvocation.Line
if ($Help.IsPresent -or ($invLine -match '(^|\s)(-\?|/\?|--help|-h)(\s|$)')) {
  Show-Usage
  exit 0
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

if (-not (Test-Path $PowerShellExe)) {
  if ($PowerShellExe -notmatch "^[A-Za-z]:\\") {
    $resolved = Get-Command $PowerShellExe -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
      throw "PowerShell executable not found: $PowerShellExe"
    }
    $PowerShellExe = $resolved.Source
  }
}

$repoPath = (Resolve-Path $RepoRoot).Path
$actionScriptPath = Join-Path $repoPath "scripts\run_weather_24h_alarm_action.ps1"
if (-not (Test-Path $actionScriptPath)) {
  throw "Alarm action script not found: $actionScriptPath"
}

$at = Parse-AlarmTime -Text $AlarmAt
$now = Get-Date
if ($at -le $now.AddSeconds(10)) {
  $at = $now.AddSeconds(20)
}

$argList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", (Quote-Arg -Text $actionScriptPath),
  "-Message", (Quote-Arg -Text $Message),
  "-LogFile", (Quote-Arg -Text $LogFile),
  "-MarkerFile", (Quote-Arg -Text $MarkerFile),
  "-MsgTimeoutSec", "$MsgTimeoutSec"
)
if ($DisableMsg.IsPresent) {
  $argList += "-DisableMsg"
}
$actionArgs = ($argList -join " ")

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Once -At $at
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$desc = "One-shot alarm for weather observe completion"

try {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
} catch {
}
try {
  schtasks /End /TN $TaskName | Out-Null
} catch {
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($currentUser)) {
  if (-not [string]::IsNullOrWhiteSpace($env:USERDOMAIN) -and -not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
    $currentUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
  } else {
    $currentUser = $env:USERNAME
  }
}

$registered = $false
$principalMode = "default"
if (-not [string]::IsNullOrWhiteSpace($currentUser)) {
  foreach ($logonType in @("S4U", "Interactive")) {
    try {
      $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $logonType -RunLevel Limited
      Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
      $registered = $true
      $principalMode = "${logonType}:$currentUser"
      break
    } catch {
    }
  }
}
if (-not $registered) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
}

Write-Host ("Registered principal mode: {0}" -f $principalMode)

if ($RunNow.IsPresent) {
  Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

[CmdletBinding()]
param(
  [string]$AlarmAt = "",
  [string]$Message = "Polymarket weather 24h observe completed. Check logs.",
  [string]$LogFile = "logs/alarm_weather24h.log",
  [string]$MarkerFile = "logs/alarm_weather24h.marker",
  [string]$WaiterStateFile = "logs/weather24h_alarm_waiter_state.json",
  [int]$MsgTimeoutSec = 3,
  [switch]$DisableMsg,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1 [-AlarmAt '2026-02-24 09:00:00']"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1 -NoBackground -AlarmAt '2026-02-24 09:00:00'"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1 -NoBackground -?"
}

function Resolve-RepoPath {
  param([Parameter(Mandatory = $true)][string]$PathText)
  if ([System.IO.Path]::IsPathRooted($PathText)) {
    return $PathText
  }
  $repoRoot = Split-Path -Parent $PSScriptRoot
  return (Join-Path $repoRoot $PathText)
}

function Ensure-ParentDir {
  param([Parameter(Mandatory = $true)][string]$PathText)
  $parent = Split-Path -Parent $PathText
  if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
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
  Write-Host ("Alarm waiter started: pid={0}" -f $proc.Id)
  exit 0
}

function Write-StateFile {
  param(
    [Parameter(Mandatory = $true)][string]$PathText,
    [Parameter(Mandatory = $true)][hashtable]$Payload
  )
  Ensure-ParentDir -PathText $PathText
  $Payload | ConvertTo-Json -Depth 5 | Set-Content -Path $PathText -Encoding UTF8
}

$invLine = [string]$MyInvocation.Line
if ($Help.IsPresent -or ($invLine -match '(^|\s)(-\?|/\?|--help|-h)(\s|$)')) {
  Show-Usage
  exit 0
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$alarmAtDt = Parse-AlarmTime -Text $AlarmAt
$now = Get-Date
if ($alarmAtDt -le $now) {
  $alarmAtDt = $now.AddSeconds(5)
}

$statePath = Resolve-RepoPath -PathText $WaiterStateFile
$actionScriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) "scripts\run_weather_24h_alarm_action.ps1"
if (-not (Test-Path $actionScriptPath)) {
  throw "Alarm action script not found: $actionScriptPath"
}

if (Test-Path $statePath) {
  try {
    $existing = Get-Content -Path $statePath -Raw | ConvertFrom-Json
    if ($null -ne $existing.waiter_pid) {
      $existingPid = [int]$existing.waiter_pid
      if ($existingPid -gt 0) {
        $existingProc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($null -ne $existingProc -and $existingProc.Id -ne $PID) {
          Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
        }
      }
    }
  } catch {
  }
}

$state = [ordered]@{
  status = "waiting"
  waiter_pid = $PID
  set_ts_local = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
  alarm_at_local = ($alarmAtDt.ToString("yyyy-MM-dd HH:mm:ss"))
  message = $Message
  log_file = $LogFile
  marker_file = $MarkerFile
  waiter_state_file = $WaiterStateFile
}
Write-StateFile -PathText $statePath -Payload $state

while ($true) {
  $remaining = $alarmAtDt - (Get-Date)
  if ($remaining.TotalSeconds -le 0) {
    break
  }
  $sleepSec = [Math]::Min([Math]::Ceiling($remaining.TotalSeconds), 30)
  Start-Sleep -Seconds ([int][Math]::Max($sleepSec, 1))
}

try {
  $actionArgs = @{
    Message = $Message
    LogFile = $LogFile
    MarkerFile = $MarkerFile
    MsgTimeoutSec = $MsgTimeoutSec
  }
  if ($DisableMsg.IsPresent) {
    $actionArgs.DisableMsg = $true
  }
  & $actionScriptPath @actionArgs

  $state.status = "fired"
  $state.fired_ts_local = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
  Write-StateFile -PathText $statePath -Payload $state
} catch {
  $state.status = "error"
  $state.error = $_.Exception.Message
  $state.error_ts_local = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
  Write-StateFile -PathText $statePath -Payload $state
  throw
}

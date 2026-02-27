[CmdletBinding()]
param(
  [string]$Message = "Polymarket weather 24h observe completed. Check logs.",
  [string]$LogFile = "logs/alarm_weather24h.log",
  [string]$MarkerFile = "logs/alarm_weather24h.marker",
  [int]$MsgTimeoutSec = 3,
  [switch]$DisableMsg,
  [string]$PythonExe = "python",
  [switch]$SkipStrategySnapshot
)

$ErrorActionPreference = "Stop"

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

$logPath = Resolve-RepoPath -PathText $LogFile
$markerPath = Resolve-RepoPath -PathText $MarkerFile

Ensure-ParentDir -PathText $logPath
Ensure-ParentDir -PathText $markerPath

$tsLocal = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$line = "[${tsLocal}] ALERT: $Message"
Add-Content -Path $logPath -Value $line

$marker = [ordered]@{
  ts_local = $tsLocal
  ts_unix = [DateTimeOffset]::Now.ToUnixTimeSeconds()
  message = $Message
}
$markerJson = $marker | ConvertTo-Json -Depth 4
Set-Content -Path $markerPath -Value $markerJson -Encoding UTF8

try { [console]::beep(880, 500) } catch {}
try { [System.Media.SystemSounds]::Exclamation.Play() } catch {}

if (-not $DisableMsg.IsPresent) {
  try {
    $target = if ([string]::IsNullOrWhiteSpace($env:USERNAME)) { "*" } else { $env:USERNAME }
    $msgProc = Start-Process -FilePath "msg.exe" -ArgumentList @($target, $Message) -WindowStyle Hidden -PassThru -ErrorAction Stop
    if (-not $msgProc.WaitForExit([Math]::Max($MsgTimeoutSec, 1) * 1000)) {
      Stop-Process -Id $msgProc.Id -Force -ErrorAction SilentlyContinue
    }
  } catch {
  }
}

$postcheckScriptPath = Join-Path $PSScriptRoot "run_weather_24h_postcheck.ps1"
if (Test-Path $postcheckScriptPath) {
  try {
    & $postcheckScriptPath -NoBackground -AlarmLogFile $LogFile -AutoRecheckOnSamplesShortfall | Out-Null
  } catch {
  }
}

if (-not $SkipStrategySnapshot.IsPresent) {
  $snapshotScriptPath = Join-Path $PSScriptRoot "render_strategy_register_snapshot.py"
  if (Test-Path $snapshotScriptPath) {
    try {
      & $PythonExe $snapshotScriptPath --pretty | Out-Null
      $tsSnapshot = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
      Add-Content -Path $logPath -Value ("[{0}] SNAPSHOT refreshed: strategy_register_latest.json/html" -f $tsSnapshot)
    } catch {
      $tsSnapshot = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
      Add-Content -Path $logPath -Value ("[{0}] SNAPSHOT refresh error: {1}" -f $tsSnapshot, $_.Exception.Message)
    }
  }
}

Write-Host $line

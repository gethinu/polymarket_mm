[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StateFile = "logs/fade_observe_supervisor_state.json",
  [string]$LogFile = "logs/fade_observe_watchdog.log",
  [string]$StartupCmd = "",
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

function Resolve-RepoPath([string]$PathValue, [string]$DefaultLeaf) {
  $candidate = [string]$PathValue
  if ([string]::IsNullOrWhiteSpace($candidate)) {
    $candidate = $DefaultLeaf
  }
  if ([System.IO.Path]::IsPathRooted($candidate)) {
    return $candidate
  }
  return (Join-Path $RepoRoot $candidate)
}

$resolvedState = Resolve-RepoPath -PathValue $StateFile -DefaultLeaf "logs/fade_observe_supervisor_state.json"
$resolvedLog = Resolve-RepoPath -PathValue $LogFile -DefaultLeaf "logs/fade_observe_watchdog.log"
if ([string]::IsNullOrWhiteSpace($StartupCmd)) {
  $StartupCmd = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\polymarket_fade_observe_startup.cmd"
}

$logDir = Split-Path $resolvedLog -Parent
if (-not [string]::IsNullOrWhiteSpace($logDir) -and -not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $resolvedLog -Append -Encoding utf8
  Write-Host $line
}

function Get-SupervisorPidFromState([string]$StatePath) {
  if (-not (Test-Path $StatePath)) {
    return 0
  }
  try {
    $payload = Get-Content -Path $StatePath -Raw | ConvertFrom-Json
  } catch {
    return 0
  }
  try {
    return [int]$payload.supervisor_pid
  } catch {
    return 0
  }
}

function Test-PidAlive([int]$PidValue) {
  if ($PidValue -le 0) { return $false }
  $proc = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
  return ($null -ne $proc)
}

function Get-SupervisorPidsFromProcessScan([string]$StatePath) {
  $pathLower = [string]$StatePath
  if ([string]::IsNullOrWhiteSpace($pathLower)) {
    return @()
  }
  $pathLower = $pathLower.ToLowerInvariant()
  try {
    $rows = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $_.Name -like "python*" -and
      -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
      $_.CommandLine -like "*bot_supervisor.py*" -and
      $_.CommandLine.ToLowerInvariant().Contains($pathLower)
    }
    return @($rows | ForEach-Object { [int]$_.ProcessId })
  } catch {
    return @()
  }
}

function Get-ActiveSupervisorInfo([string]$StatePath) {
  $statePid = Get-SupervisorPidFromState -StatePath $StatePath
  if (Test-PidAlive -PidValue $statePid) {
    return @{
      pid = $statePid
      source = "state"
      state_pid = $statePid
      scan_pids = @()
    }
  }

  $scanPids = Get-SupervisorPidsFromProcessScan -StatePath $StatePath
  foreach ($scanPid in $scanPids) {
    if (Test-PidAlive -PidValue $scanPid) {
      return @{
        pid = $scanPid
        source = "scan"
        state_pid = $statePid
        scan_pids = $scanPids
      }
    }
  }

  return @{
    pid = 0
    source = "none"
    state_pid = $statePid
    scan_pids = $scanPids
  }
}

$supervisorInfo = Get-ActiveSupervisorInfo -StatePath $resolvedState
$supervisorPid = [int]$supervisorInfo.pid
$alive = ($supervisorPid -gt 0)

if ($alive) {
  $scanPids = @($supervisorInfo.scan_pids)
  if ($scanPids.Count -gt 1) {
    Log ("watchdog warning: multiple supervisor processes detected scan_pids={0}" -f (($scanPids | Sort-Object) -join ","))
  }
  Log ("watchdog ok: supervisor alive pid={0} source={1}" -f $supervisorPid, $supervisorInfo.source)
  exit 0
}

if (-not (Test-Path $StartupCmd)) {
  Log ("watchdog error: startup cmd not found: {0}" -f $StartupCmd)
  exit 1
}

Log ("watchdog recover: supervisor not alive (state_pid={0}) -> startup cmd" -f $supervisorInfo.state_pid)
try {
  & cmd.exe /c ('"{0}"' -f $StartupCmd)
  $startupExit = $LASTEXITCODE
  if ($startupExit -ne 0) {
    Log ("watchdog recover warning: startup cmd exit_code={0}" -f $startupExit)
  }
} catch {
  Log ("watchdog recover error: startup cmd invocation failed: {0}" -f $_.Exception.Message)
  exit 1
}

$maxWaitSec = 45
$pollSec = 3
$waited = 0
while ($waited -lt $maxWaitSec) {
  Start-Sleep -Seconds $pollSec
  $newInfo = Get-ActiveSupervisorInfo -StatePath $resolvedState
  $newPid = [int]$newInfo.pid
  $newAlive = ($newPid -gt 0)
  if ($newAlive) {
    $scanPids = @($newInfo.scan_pids)
    if ($scanPids.Count -gt 1) {
      Log ("watchdog warning: multiple supervisor processes detected scan_pids={0}" -f (($scanPids | Sort-Object) -join ","))
    }
    Log ("watchdog recover success: supervisor pid={0} source={1} waited_sec={2}" -f $newPid, $newInfo.source, $waited)
    exit 0
  }
  $waited += $pollSec
}

$newInfo = Get-ActiveSupervisorInfo -StatePath $resolvedState
Log ("watchdog recover pending: supervisor still not alive after startup cmd (state_pid={0}) waited_sec={1}" -f $newInfo.state_pid, $waited)
exit 1

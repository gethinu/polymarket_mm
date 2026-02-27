param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$ConfigFile = "configs/bot_supervisor.simmer_ab.observe.json",
  [string]$LogFile = "logs/simmer_ab_supervisor.log",
  [string]$StateFile = "logs/simmer_ab_supervisor_state.json",
  [string]$LockFile = "logs/simmer_ab_observe_supervisor.lock",
  [double]$PollSec = 1,
  [double]$WriteStateSec = 2,
  [int]$RunSeconds = 0,
  [switch]$IgnoreLock,
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

function Resolve-PathFromRepo([string]$repo, [string]$p) {
  $raw = [string]$p
  if ([System.IO.Path]::IsPathRooted($raw)) {
    return (Resolve-Path $raw).Path
  }
  return (Resolve-Path (Join-Path $repo $raw)).Path
}

function Test-PidAlive([int]$pidValue) {
  if ($pidValue -le 0) { return $false }
  try {
    $null = Get-Process -Id $pidValue -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Get-LockOwnerPid([string]$lockPath) {
  if (-not (Test-Path $lockPath)) { return 0 }
  try {
    $raw = (Get-Content -Path $lockPath -Raw -ErrorAction Stop).Trim()
  } catch {
    return 0
  }
  if ([string]::IsNullOrWhiteSpace($raw)) { return 0 }

  try {
    $obj = $raw | ConvertFrom-Json -ErrorAction Stop
    if ($null -ne $obj -and $obj.PSObject.Properties.Name -contains "pid") {
      return [int]$obj.pid
    }
  } catch {
  }

  $parsed = 0
  if ([int]::TryParse($raw, [ref]$parsed)) {
    return [int]$parsed
  }
  return 0
}

if (-not (Test-Path $PythonExe)) {
  if ($PythonExe -notmatch "^[A-Za-z]:\\") {
    $resolvedPy = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($null -eq $resolvedPy) {
      throw "Python executable not found: $PythonExe"
    }
    $PythonExe = $resolvedPy.Source
  } else {
    throw "Python executable not found: $PythonExe"
  }
}

$repoResolved = (Resolve-Path $RepoRoot).Path
$supervisorPy = Resolve-PathFromRepo -repo $repoResolved -p "scripts/bot_supervisor.py"
$configResolved = Resolve-PathFromRepo -repo $repoResolved -p $ConfigFile
$logResolved = if ([System.IO.Path]::IsPathRooted($LogFile)) { $LogFile } else { Join-Path $repoResolved $LogFile }
$stateResolved = if ([System.IO.Path]::IsPathRooted($StateFile)) { $StateFile } else { Join-Path $repoResolved $StateFile }
$lockResolved = if ([System.IO.Path]::IsPathRooted($LockFile)) { $LockFile } else { Join-Path $repoResolved $LockFile }

$logDir = Split-Path -Parent $logResolved
$stateDir = Split-Path -Parent $stateResolved
$lockDir = Split-Path -Parent $lockResolved
if (-not (Test-Path $logDir)) { New-Item -Path $logDir -ItemType Directory -Force | Out-Null }
if (-not (Test-Path $stateDir)) { New-Item -Path $stateDir -ItemType Directory -Force | Out-Null }
if (-not (Test-Path $lockDir)) { New-Item -Path $lockDir -ItemType Directory -Force | Out-Null }

$exitCode = 0
$lockAcquired = $false

try {
  if (Test-Path $lockResolved) {
    $ownerPid = Get-LockOwnerPid -lockPath $lockResolved
    if ($ownerPid -gt 0 -and (Test-PidAlive -pidValue $ownerPid) -and (-not $IgnoreLock.IsPresent)) {
      $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
      "[$stamp] skip start: lock busy path=$lockResolved owner_pid=$ownerPid" | Out-File -FilePath $logResolved -Append -Encoding utf8
      $exitCode = 0
    } else {
      try {
        Remove-Item -Path $lockResolved -Force -ErrorAction Stop
      } catch {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$stamp] warn: stale lock exists but could not remove path=$lockResolved err=$($_.Exception.Message)" | Out-File -FilePath $logResolved -Append -Encoding utf8
      }
    }
  }

  if ($exitCode -eq 0 -and (-not $lockAcquired)) {
    if ($IgnoreLock.IsPresent -or (-not (Test-Path $lockResolved))) {
      $payloadObj = @{
        pid = [int]$PID
        acquired_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
        repo_root = $repoResolved
      }
      try {
        $payload = $payloadObj | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($lockResolved, $payload, [System.Text.Encoding]::UTF8)
        $lockAcquired = $true
      } catch {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$stamp] error: lock create failed path=$lockResolved err=$($_.Exception.Message)" | Out-File -FilePath $logResolved -Append -Encoding utf8
        $exitCode = 2
      }
    }
  }

  if ($lockAcquired) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$stamp] start config=$configResolved run_seconds=$RunSeconds lock=$lockResolved" | Out-File -FilePath $logResolved -Append -Encoding utf8

    $args = @(
      $supervisorPy,
      "run",
      "--config", $configResolved,
      "--log-file", $logResolved,
      "--state-file", $stateResolved,
      "--poll-sec", ([string]$PollSec),
      "--write-state-sec", ([string]$WriteStateSec),
      "--halt-when-all-stopped"
    )
    if ($RunSeconds -gt 0) {
      $args += @("--run-seconds", ([string]$RunSeconds))
    }

    & $PythonExe @args
    $exitCode = [int]$LASTEXITCODE

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$stamp] end exit_code=$exitCode" | Out-File -FilePath $logResolved -Append -Encoding utf8
  }
} finally {
  if ($lockAcquired) {
    try {
      $ownerPid = Get-LockOwnerPid -lockPath $lockResolved
      if ($ownerPid -eq 0 -or $ownerPid -eq [int]$PID) {
        Remove-Item -Path $lockResolved -Force -ErrorAction SilentlyContinue
      }
    } catch {
    }
  }
}

exit $exitCode

param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,
  [int]$RunSeconds = 0,
  [double]$MinEdgeCents = 2.0,
  [double]$Shares = 5.0,
  [ValidateSet("buckets", "yes-no", "both")]
  [string]$Strategy = "buckets",
  [double]$SummaryEverySec = 30.0,
  [int]$MaxSubscribeTokens = 400,
  [string]$LogFile = "",
  [string]$StateFile = "",
  [string]$MutexName = "Global\PolymarketWeatherArbObserve",
  [switch]$RestartOnFailure,
  [int]$RestartDelaySec = 20,
  [int]$MaxRestarts = 30
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

function To-Arg([object]$Value) {
  return [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0}", $Value)
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$baseDir = (Resolve-Path $RepoRoot).Path
$botPy = Join-Path $baseDir "scripts\polymarket_clob_arb_realtime.py"
$logDir = Join-Path $baseDir "logs"

if ([string]::IsNullOrWhiteSpace($LogFile)) {
  $LogFile = Join-Path $logDir "clob-arb-weather-observe.log"
}
if ([string]::IsNullOrWhiteSpace($StateFile)) {
  # Reuse existing state filename conventions to avoid introducing new long-lived state docs burden.
  $StateFile = Join-Path $logDir "clob_arb_state.json"
}

if (-not (Test-Path $botPy)) {
  throw "bot script not found: $botPy"
}

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}

# Hard-stop live execution in this helper.
[Environment]::SetEnvironmentVariable("CLOBBOT_EXECUTE", "0", "Process")
[Environment]::SetEnvironmentVariable("CLOBBOT_CONFIRM_LIVE", "", "Process")

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasLock = $mutex.WaitOne(0)
if (-not $hasLock) {
  Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] skipped: previous weather-observe run still active"
  exit 0
}

try {
  $deadlineUtc = $null
  if ($RunSeconds -gt 0) {
    $deadlineUtc = [DateTime]::UtcNow.AddSeconds($RunSeconds)
  }

  $restartCount = 0
  $code = 0

  while ($true) {
    $nowUtc = [DateTime]::UtcNow
    $runSecondsThisAttempt = $RunSeconds
    if ($null -ne $deadlineUtc) {
      $remaining = [Math]::Max([int][Math]::Ceiling(($deadlineUtc - $nowUtc).TotalSeconds), 0)
      if ($remaining -le 0) {
        break
      }
      $runSecondsThisAttempt = $remaining
    }

    $attemptNo = $restartCount + 1
    $args = @(
      $botPy,
      "--universe", "weather",
      "--strategy", $Strategy,
      "--run-seconds", (To-Arg $runSecondsThisAttempt),
      "--min-edge-cents", (To-Arg $MinEdgeCents),
      "--shares", (To-Arg $Shares),
      "--summary-every-sec", (To-Arg $SummaryEverySec),
      "--max-subscribe-tokens", (To-Arg $MaxSubscribeTokens),
      "--log-file", $LogFile,
      "--state-file", $StateFile
    )

    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] weather-observe run start (attempt=$attemptNo run_seconds=$runSecondsThisAttempt)"
    try {
      & $PythonExe @args
      $code = $LASTEXITCODE
    }
    catch {
      $code = 1
      $msg = $_.Exception.Message
      Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] weather-observe exception (attempt=$attemptNo): $msg"
    }

    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] weather-observe run end (attempt=$attemptNo code=$code)`n"
    if ($code -eq 0) {
      break
    }
    if (-not $RestartOnFailure.IsPresent) {
      break
    }
    if ($MaxRestarts -ge 0 -and $restartCount -ge $MaxRestarts) {
      Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] weather-observe restart limit reached (max_restarts=$MaxRestarts)"
      break
    }
    if ($null -ne $deadlineUtc -and [DateTime]::UtcNow -ge $deadlineUtc) {
      break
    }

    $delay = [Math]::Max($RestartDelaySec, 1)
    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] weather-observe restart scheduled in ${delay}s"
    Start-Sleep -Seconds $delay
    $restartCount += 1
  }

  exit $code
}
finally {
  if ($hasLock) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}

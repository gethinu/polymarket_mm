param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,
  # Perpetual re-scan cadence. The gamma-active/event-pair scan EXITS immediately
  # ("No valid Gamma baskets found") when the current universe has nothing to
  # subscribe to, so a single --run-seconds 0 process cannot stay resident on an
  # empty universe. This wrapper re-runs the scan every $RescanDelaySec so it
  # picks baskets up as soon as they appear; when baskets DO exist the inner
  # process stays resident (subscribed) and the wrapper simply waits on it.
  [int]$RescanDelaySec = 300,
  [int]$MaxRescans = 0,            # 0 = run forever
  [double]$SummaryEverySec = 30.0,
  [int]$GammaLimit = 1500,
  [int]$GammaScanMaxMarkets = 40000,
  [int]$GammaMaxDaysToEnd = 60,
  [int]$MaxMarketsPerEvent = 5,
  [int]$MaxSubscribeTokens = 400,
  [double]$ObserveExecEdgeMinUsd = 0.01,
  [int]$ObserveExecEdgeStrikeLimit = 1,
  [int]$ObserveExecEdgeCooldownSec = 180,
  [double]$MinEdgeCents = 10.0,
  [string]$LogFile = "",
  [string]$StateFile = "",
  [string]$MutexName = "Global\PolymarketGammaEventPairObserve"
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
  $LogFile = Join-Path $logDir "clob-arb-gamma-observe.log"
}
if ([string]::IsNullOrWhiteSpace($StateFile)) {
  $StateFile = Join-Path $logDir "clob_arb_gamma_state.json"
}

if (-not (Test-Path $botPy)) {
  throw "bot script not found: $botPy"
}
if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}

# Hard-stop live execution in this helper (observe-only).
[Environment]::SetEnvironmentVariable("CLOBBOT_EXECUTE", "0", "Process")
[Environment]::SetEnvironmentVariable("CLOBBOT_CONFIRM_LIVE", "", "Process")

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasLock = $mutex.WaitOne(0)
if (-not $hasLock) {
  Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] skipped: previous gamma-observe run still active"
  exit 0
}

try {
  $scanCount = 0
  while ($true) {
    $scanCount += 1
    $args = @(
      $botPy,
      "--universe", "gamma-active",
      "--strategy", "event-pair",
      "--run-seconds", "0",
      "--summary-every-sec", (To-Arg $SummaryEverySec),
      "--gamma-limit", (To-Arg $GammaLimit),
      "--gamma-min-liquidity", "0",
      "--gamma-min-volume24hr", "0",
      "--gamma-scan-max-markets", (To-Arg $GammaScanMaxMarkets),
      "--gamma-max-days-to-end", (To-Arg $GammaMaxDaysToEnd),
      "--max-markets-per-event", (To-Arg $MaxMarketsPerEvent),
      "--max-subscribe-tokens", (To-Arg $MaxSubscribeTokens),
      "--metrics-log-all-candidates",
      "--observe-exec-edge-filter",
      "--observe-exec-edge-min-usd", (To-Arg $ObserveExecEdgeMinUsd),
      "--observe-exec-edge-strike-limit", (To-Arg $ObserveExecEdgeStrikeLimit),
      "--observe-exec-edge-cooldown-sec", (To-Arg $ObserveExecEdgeCooldownSec),
      "--observe-exec-edge-filter-strategies", "event-yes",
      "--min-edge-cents", (To-Arg $MinEdgeCents),
      "--log-file", $LogFile,
      "--state-file", $StateFile
    )

    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gamma-observe scan start (scan=$scanCount)"
    $code = 0
    try {
      & $PythonExe @args
      $code = $LASTEXITCODE
    }
    catch {
      $code = 1
      Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gamma-observe exception (scan=$scanCount): $($_.Exception.Message)"
    }
    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gamma-observe scan end (scan=$scanCount code=$code) -- empty universe exits fast; re-scanning`n"

    if ($MaxRescans -gt 0 -and $scanCount -ge $MaxRescans) {
      Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gamma-observe rescan limit reached (max_rescans=$MaxRescans)"
      break
    }

    $delay = [Math]::Max($RescanDelaySec, 1)
    Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gamma-observe next scan in ${delay}s"
    Start-Sleep -Seconds $delay
  }
}
finally {
  if ($hasLock) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}

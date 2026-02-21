param(
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

$baseDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$botPy = Join-Path $baseDir "scripts\polymarket_clob_arb_realtime.py"
$logDir = Join-Path $baseDir "logs"
$logFile = Join-Path $logDir "clob-arb-monitor.log"
$stateFile = Join-Path $logDir "clob_arb_state.json"
$mutexName = "Global\PolymarketClobArbMonitor"

function Get-EnvOrDefault {
  param([string]$Name, [string]$Default)
  # Prefer persisted values (User/Machine) over inherited Process env,
  # because scheduled tasks may inherit stale Process vars from Explorer.
  $scopes = @("User", "Machine", "Process")
  foreach ($scope in $scopes) {
    $v = [Environment]::GetEnvironmentVariable($Name, $scope)
    if (-not [string]::IsNullOrWhiteSpace($v)) {
      return $v
    }
  }
  return $Default
}

function Ensure-ProcessEnv {
  param([string]$Name)
  $current = [Environment]::GetEnvironmentVariable($Name, "Process")
  if (-not [string]::IsNullOrWhiteSpace($current)) { return }

  $fromUser = [Environment]::GetEnvironmentVariable($Name, "User")
  if (-not [string]::IsNullOrWhiteSpace($fromUser)) {
    [Environment]::SetEnvironmentVariable($Name, $fromUser, "Process")
    return
  }

  $fromMachine = [Environment]::GetEnvironmentVariable($Name, "Machine")
  if (-not [string]::IsNullOrWhiteSpace($fromMachine)) {
    [Environment]::SetEnvironmentVariable($Name, $fromMachine, "Process")
  }
}

function Read-DpapiSecretFile {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
  if (-not (Test-Path $Path)) { return "" }
  try {
    $enc = Get-Content -Path $Path -Raw
    if ([string]::IsNullOrWhiteSpace($enc)) { return "" }
    $secure = $enc | ConvertTo-SecureString
    $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { if ($ptr -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) } }
  }
  catch {
    return ""
  }
}

function Ensure-ProcessSecretFromDpapiFile {
  param(
    [string]$Name,
    [string]$FileEnvVar
  )

  $current = [Environment]::GetEnvironmentVariable($Name, "Process")
  if (-not [string]::IsNullOrWhiteSpace($current)) { return }

  $path = Get-EnvOrDefault $FileEnvVar ""
  $plain = (Read-DpapiSecretFile -Path $path).Trim()
  if ($Name -eq "PM_PRIVATE_KEY" -and -not [string]::IsNullOrWhiteSpace($plain)) {
    if ($plain -match '^[0-9a-fA-F]{64}$') { $plain = "0x$plain" }
  }
  if (-not [string]::IsNullOrWhiteSpace($plain)) {
    [Environment]::SetEnvironmentVariable($Name, $plain, "Process")
  }
}

function Rotate-LogIfNeeded {
  param([string]$Path, [int64]$MaxBytes = 20MB, [int]$Keep = 5)

  if (-not (Test-Path $Path)) { return }
  $file = Get-Item $Path
  if ($file.Length -lt $MaxBytes) { return }

  $ts = Get-Date -Format "yyyyMMdd-HHmmss"
  $archived = "$Path.$ts"
  Move-Item -Path $Path -Destination $archived -Force

  $pattern = ([IO.Path]::GetFileName($Path)) + ".*"
  $old = Get-ChildItem -Path ([IO.Path]::GetDirectoryName($Path)) -Filter $pattern |
    Sort-Object LastWriteTime -Descending
  if ($old.Count -gt $Keep) {
    $old | Select-Object -Skip $Keep | Remove-Item -Force
  }
}

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}
Rotate-LogIfNeeded -Path $logFile

$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasLock = $mutex.WaitOne(0)
if (-not $hasLock) {
  Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] skipped: previous monitor run still active"
  exit 0
}

try {
  if (-not (Test-Path $botPy)) {
    Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] error: bot script not found: $botPy"
    exit 1
  }

  # Ensure child python process sees credentials configured at User/Machine scope.
  $forwardEnv = @(
    "SIMMER_API_KEY",
    "PM_FUNDER",
    "PM_PROXY_ADDRESS",
    "PM_API_KEY"
  )
  foreach ($name in $forwardEnv) { Ensure-ProcessEnv -Name $name }

  # Prefer DPAPI-encrypted secret files if configured (more secure than env vars).
  Ensure-ProcessSecretFromDpapiFile -Name "PM_PRIVATE_KEY" -FileEnvVar "PM_PRIVATE_KEY_DPAPI_FILE"
  Ensure-ProcessSecretFromDpapiFile -Name "PM_API_SECRET" -FileEnvVar "PM_API_SECRET_DPAPI_FILE"
  Ensure-ProcessSecretFromDpapiFile -Name "PM_API_PASSPHRASE" -FileEnvVar "PM_API_PASSPHRASE_DPAPI_FILE"

  # Backwards-compatible fallback (only if still unset after DPAPI load).
  Ensure-ProcessEnv -Name "PM_PRIVATE_KEY"
  Ensure-ProcessEnv -Name "PM_API_SECRET"
  Ensure-ProcessEnv -Name "PM_API_PASSPHRASE"

  $runSeconds = Get-EnvOrDefault "CLOBBOT_RUN_SECONDS" "85"
  $shares = Get-EnvOrDefault "CLOBBOT_SHARES" "5"
  $minEdge = Get-EnvOrDefault "CLOBBOT_MIN_EDGE_CENTS" "2.5"
  $winnerFee = Get-EnvOrDefault "CLOBBOT_WINNER_FEE_RATE" "0.02"
  $fixedCost = Get-EnvOrDefault "CLOBBOT_FIXED_COST" "0.00"
  $summaryEvery = Get-EnvOrDefault "CLOBBOT_SUMMARY_EVERY_SEC" "0"

  $maxExec = Get-EnvOrDefault "CLOBBOT_MAX_EXEC_PER_DAY" "20"
  $maxNotional = Get-EnvOrDefault "CLOBBOT_MAX_NOTIONAL_PER_DAY" "200"
  $maxOpenOrders = Get-EnvOrDefault "CLOBBOT_MAX_OPEN_ORDERS" "0"
  $maxFailures = Get-EnvOrDefault "CLOBBOT_MAX_CONSEC_FAILURES" "3"
  $dailyLoss = Get-EnvOrDefault "CLOBBOT_DAILY_LOSS_LIMIT_USD" "0"

  $execSlippageBps = Get-EnvOrDefault "CLOBBOT_EXEC_SLIPPAGE_BPS" "50"
  $unwindSlippageBps = Get-EnvOrDefault "CLOBBOT_UNWIND_SLIPPAGE_BPS" "150"
  $execCooldownSec = Get-EnvOrDefault "CLOBBOT_EXEC_COOLDOWN_SEC" "30"
  $execAttempts = Get-EnvOrDefault "CLOBBOT_EXEC_MAX_ATTEMPTS" "2"
  $execBackend = Get-EnvOrDefault "CLOBBOT_EXEC_BACKEND" "auto"
  $simmerVenue = Get-EnvOrDefault "CLOBBOT_SIMMER_VENUE" "polymarket"
  $simmerSource = Get-EnvOrDefault "CLOBBOT_SIMMER_SOURCE" "sdk:clob-arb"
  $simmerMinAmount = Get-EnvOrDefault "CLOBBOT_SIMMER_MIN_AMOUNT" "1.0"
  $strategy = Get-EnvOrDefault "CLOBBOT_STRATEGY" "both"
  $maxLegs = Get-EnvOrDefault "CLOBBOT_MAX_LEGS" "9"

  $executeLive = (Get-EnvOrDefault "CLOBBOT_EXECUTE" "0") -eq "1"

  $args = @(
    $botPy,
    "--run-seconds", $runSeconds,
    "--summary-every-sec", $summaryEvery,
    "--shares", $shares,
    "--min-edge-cents", $minEdge,
    "--winner-fee-rate", $winnerFee,
    "--fixed-cost", $fixedCost,
    "--max-exec-per-day", $maxExec,
    "--max-notional-per-day", $maxNotional,
    "--max-open-orders", $maxOpenOrders,
    "--max-consecutive-failures", $maxFailures,
    "--daily-loss-limit-usd", $dailyLoss,
    "--exec-slippage-bps", $execSlippageBps,
    "--unwind-slippage-bps", $unwindSlippageBps,
    "--exec-cooldown-sec", $execCooldownSec,
    "--exec-max-attempts", $execAttempts,
    "--exec-backend", $execBackend,
    "--strategy", $strategy,
    "--max-legs", $maxLegs,
    "--simmer-venue", $simmerVenue,
    "--simmer-source", $simmerSource,
    "--simmer-min-amount", $simmerMinAmount,
    "--log-file", $logFile,
    "--state-file", $stateFile
  )

  if ($executeLive) {
    $args += @("--execute", "--confirm-live", "YES")
  }

  Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] run start (execute=$executeLive, backend=$execBackend)"
  & python @args
  Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] run end`n"
}
finally {
  if ($hasLock) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}

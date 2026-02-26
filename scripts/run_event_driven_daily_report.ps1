param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,
  [int]$MaxPages = 10,
  [int]$PageSize = 200,
  [double]$MinLiquidity = 5000,
  [double]$MinVolume24h = 250,
  [double]$MinDaysToEnd = 0.5,
  [double]$MaxDaysToEnd = 365,
  [double]$MinEdgeCents = 1.0,
  [double]$MinLegPrice = 0.05,
  [double]$MaxLegPrice = 0.95,
  [double]$MinConfidence = 0.25,
  [int]$TopN = 15,
  [double]$KellyFraction = 0.25,
  [double]$MaxKellyFraction = 0.20,
  [double]$BankrollUsd = 1000,
  [double]$MinBetUsd = 10,
  [double]$MaxBetUsd = 150,
  [string]$IncludeRegex = "",
  [string]$ExcludeRegex = "",
  [switch]$IncludeNonEvent,
  [double]$ReportHours = 24,
  [string]$ThresholdsCents = "1,2,3,5,8,10",
  [string]$ProfitThresholdsCents = "0.8,1,2,3,5",
  [string]$ProfitCaptureRatios = "0.25,0.35,0.50",
  [double]$ProfitTargetMonthlyReturnPct = 12,
  [double]$ProfitAssumedBankrollUsd = 100,
  [double]$ProfitMaxEvMultipleOfStake = 0.35,
  [switch]$SkipProfitWindow,
  [switch]$FailOnNoGo,
  [switch]$Discord
)

$ErrorActionPreference = "Stop"

# Earliest marker for Task Scheduler diagnostics.
try {
  $bootstrapDir = Join-Path $RepoRoot "logs"
  if (-not (Test-Path $bootstrapDir)) {
    New-Item -Path $bootstrapDir -ItemType Directory -Force | Out-Null
  }
  $bootstrapLine = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] bootstrap repo_root=$RepoRoot"
  $bootstrapLine | Out-File -FilePath (Join-Path $bootstrapDir "event_driven_daily_bootstrap.log") -Append -Encoding utf8
} catch {
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

function Parse-Bool([string]$v) {
  if ([string]::IsNullOrWhiteSpace($v)) { return $false }
  switch ($v.Trim().ToLowerInvariant()) {
    "1" { return $true }
    "true" { return $true }
    "yes" { return $true }
    "y" { return $true }
    "on" { return $true }
    default { return $false }
  }
}

function Get-EnvAny([string]$name) {
  $pv = [Environment]::GetEnvironmentVariable($name, "Process")
  if (-not [string]::IsNullOrWhiteSpace($pv)) { return $pv }
  $uv = [Environment]::GetEnvironmentVariable($name, "User")
  if (-not [string]::IsNullOrWhiteSpace($uv)) { return $uv }
  $mv = [Environment]::GetEnvironmentVariable($name, "Machine")
  if (-not [string]::IsNullOrWhiteSpace($mv)) { return $mv }
  return ""
}

function Get-DiscordWebhookUrl {
  $url = Get-EnvAny "CLOBBOT_DISCORD_WEBHOOK_URL"
  if (-not [string]::IsNullOrWhiteSpace($url)) { return $url }
  return (Get-EnvAny "DISCORD_WEBHOOK_URL")
}

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
}

function Run-Python([string[]]$CmdArgs) {
  & $PythonExe @CmdArgs
  if ($LASTEXITCODE -ne 0) {
    throw "python failed with code ${LASTEXITCODE}: $($CmdArgs -join ' ')"
  }
}

$observeScript = Join-Path $RepoRoot "scripts\polymarket_event_driven_observe.py"
$reportScript = Join-Path $RepoRoot "scripts\report_event_driven_observation.py"
$profitReportScript = Join-Path $RepoRoot "scripts\report_event_driven_profit_window.py"
$logDir = Join-Path $RepoRoot "logs"
$runLog = Join-Path $logDir "event_driven_daily_run.log"
$summaryTxt = Join-Path $logDir "event_driven_daily_summary.txt"
$observeLogFile = Join-Path $logDir "event-driven-observe.log"
$signalsFile = Join-Path $logDir "event-driven-observe-signals.jsonl"
$metricsFile = Join-Path $logDir "event-driven-observe-metrics.jsonl"
$profitJson = Join-Path $logDir "event_driven_profit_window_latest.json"
$profitTxt = Join-Path $logDir "event_driven_profit_window_latest.txt"

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}
if (-not (Test-Path $observeScript)) {
  throw "observe script not found: $observeScript"
}
if (-not (Test-Path $reportScript)) {
  throw "report script not found: $reportScript"
}
if (-not $SkipProfitWindow.IsPresent -and -not (Test-Path $profitReportScript)) {
  throw "profit report script not found: $profitReportScript"
}

$discordRequested = $Discord.IsPresent
if (-not $discordRequested) {
  $discordRequested = Parse-Bool (Get-EnvAny "EVENT_DRIVEN_DAILY_DISCORD")
}

Log "start max_pages=$MaxPages page_size=$PageSize min_liq=$MinLiquidity min_vol24h=$MinVolume24h min_edge_cents=$MinEdgeCents top_n=$TopN report_hours=$ReportHours discord_req=$discordRequested"

$observeArgs = @(
  $observeScript,
  "--max-pages", "$MaxPages",
  "--page-size", "$PageSize",
  "--min-liquidity", "$MinLiquidity",
  "--min-volume-24h", "$MinVolume24h",
  "--min-days-to-end", "$MinDaysToEnd",
  "--max-days-to-end", "$MaxDaysToEnd",
  "--min-edge-cents", "$MinEdgeCents",
  "--min-leg-price", "$MinLegPrice",
  "--max-leg-price", "$MaxLegPrice",
  "--min-confidence", "$MinConfidence",
  "--top-n", "$TopN",
  "--kelly-fraction", "$KellyFraction",
  "--max-kelly-fraction", "$MaxKellyFraction",
  "--bankroll-usd", "$BankrollUsd",
  "--min-bet-usd", "$MinBetUsd",
  "--max-bet-usd", "$MaxBetUsd",
  "--log-file", $observeLogFile,
  "--signals-file", $signalsFile,
  "--metrics-file", $metricsFile
)
if (-not [string]::IsNullOrWhiteSpace($IncludeRegex)) {
  $observeArgs += @("--include-regex", $IncludeRegex)
}
if (-not [string]::IsNullOrWhiteSpace($ExcludeRegex)) {
  $observeArgs += @("--exclude-regex", $ExcludeRegex)
}
if ($IncludeNonEvent.IsPresent) {
  $observeArgs += "--include-non-event"
}

Log "observe run start"
Run-Python $observeArgs
Log "observe run done"

$reportArgs = @(
  $reportScript,
  "--hours", "$ReportHours",
  "--thresholds-cents", $ThresholdsCents,
  "--signals-file", $signalsFile,
  "--metrics-file", $metricsFile
)

Log "report build start"
$reportOutput = & $PythonExe @reportArgs 2>&1
if ($LASTEXITCODE -ne 0) {
  throw "report failed with code ${LASTEXITCODE}"
}
$reportText = ($reportOutput -join [Environment]::NewLine)
$summaryParts = @(
  "=== Observation Summary ===",
  ($reportText.TrimEnd())
)
Write-Host $reportText
Log "report build done"

if (-not $SkipProfitWindow.IsPresent) {
  $profitArgs = @(
    $profitReportScript,
    "--hours", "$ReportHours",
    "--signals-file", $signalsFile,
    "--metrics-file", $metricsFile,
    "--thresholds-cents", $ProfitThresholdsCents,
    "--capture-ratios", $ProfitCaptureRatios,
    "--target-monthly-return-pct", "$ProfitTargetMonthlyReturnPct",
    "--assumed-bankroll-usd", "$ProfitAssumedBankrollUsd",
    "--max-ev-multiple-of-stake", "$ProfitMaxEvMultipleOfStake",
    "--out-json", $profitJson,
    "--out-txt", $profitTxt,
    "--pretty"
  )
  Log "profit-window build start"
  $profitOutput = & $PythonExe @profitArgs 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "profit-window report failed with code ${LASTEXITCODE}"
  }
  $profitOutputText = ($profitOutput -join [Environment]::NewLine)
  Write-Host $profitOutputText
  Log "profit-window build done"

  if (Test-Path $profitTxt) {
    $profitText = Get-Content -Path $profitTxt -Raw -ErrorAction SilentlyContinue
    if (-not [string]::IsNullOrWhiteSpace($profitText)) {
      $summaryParts += ""
      $summaryParts += "=== Profit Window Summary ==="
      $summaryParts += ($profitText.TrimEnd())
    }
  }

  if ($FailOnNoGo.IsPresent) {
    if (-not (Test-Path $profitJson)) {
      throw "profit-window json missing: $profitJson"
    }
    $profitObj = Get-Content -Path $profitJson -Raw | ConvertFrom-Json
    $profitDecision = [string]$profitObj.decision.decision
    if ([string]::IsNullOrWhiteSpace($profitDecision)) {
      throw "profit-window decision missing in $profitJson"
    }
    if ($profitDecision -ne "GO") {
      throw "profit-window decision=$profitDecision (FailOnNoGo)"
    }
    Log "profit-window decision GO"
  }
}

$summaryText = ($summaryParts -join [Environment]::NewLine)
$summaryText | Out-File -FilePath $summaryTxt -Encoding utf8
$summaryText | Out-File -FilePath $runLog -Append -Encoding utf8
Log "summary write done -> $summaryTxt"

if ($discordRequested) {
  $webhook = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhook)) {
    Log "discord skip: webhook missing"
  } else {
    Log "report discord post start"
    $discordArgs = @($reportArgs + @("--discord"))
    & $PythonExe @discordArgs 2>&1 | Out-File -FilePath $runLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
      throw "report discord failed with code ${LASTEXITCODE}"
    }
    Log "report discord post done"
  }
}

Log "done"

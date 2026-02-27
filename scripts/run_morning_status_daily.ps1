[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$StrategyId = "weather_clob_arb_buckets_observe",
  [int]$MinRealizedDays = 30,
  [string]$SnapshotJson = "logs/strategy_register_latest.json",
  [string]$HealthJson = "logs/automation_health_latest.json",
  [string]$UncorrelatedJson = "logs/uncorrelated_portfolio_proxy_analysis_latest.json",
  [string]$UncorrelatedStrategyIds = "weather_clob_arb_buckets_observe,no_longshot_daily_observe,link_intake_walletseed_cohort_observe,gamma_eventpair_exec_edge_filter_observe,hourly_updown_highprob_calibration_observe",
  [double]$UncorrelatedCorrThresholdAbs = 0.30,
  [int]$UncorrelatedMinOverlapDays = 2,
  [int]$UncorrelatedMinRealizedDaysForCorrelation = 7,
  [string]$GateAlarmStateJson = "logs/strategy_gate_alarm_state.json",
  [string]$GateAlarmLogFile = "logs/strategy_gate_alarm.log",
  [string]$SimmerAbDecisionJson = "logs/simmer-ab-decision-latest.json",
  [double]$SimmerAbMaxStaleHours = 30.0,
  [string]$DiscordWebhookEnv = "CLOBBOT_DISCORD_WEBHOOK_URL_CHECK_MORNING_STATUS",
  [switch]$NoRefresh,
  [switch]$SkipHealth,
  [switch]$SkipGateAlarm,
  [switch]$SkipUncorrelatedPortfolio,
  [switch]$SkipImplementationLedger,
  [switch]$SkipSimmerAb,
  [switch]$SkipProcessScan,
  [switch]$DiscordGateAlarm,
  [switch]$FailOnGateNotReady,
  [switch]$FailOnStageNotFinal,
  [switch]$FailOnHealthNoGo,
  [switch]$FailOnSimmerAbFinalNoGo,
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

if (-not (Test-Path $PythonExe)) {
  if ($PythonExe -notmatch "^[A-Za-z]:\\") {
    $resolved = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
      throw "Python executable not found: $PythonExe"
    }
    $PythonExe = $resolved.Source
  }
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}
$runLog = Join-Path $logsDir "morning_status_daily_run.log"

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
}

function Log-NoLongshotMonthlyKpi {
  param(
    [Parameter(Mandatory = $true)][string]$StageTag
  )

  $kpiTool = Join-Path $RepoRoot "scripts\report_no_longshot_monthly_return.py"
  if (-not (Test-Path $kpiTool)) {
    Log ("kpi[{0}] skipped: tool_missing path={1}" -f $StageTag, $kpiTool)
    return
  }

  try {
    $kpiOutput = & $PythonExe $kpiTool "--json" "--snapshot-json" $SnapshotJson 2>&1
    $kpiExitCode = $LASTEXITCODE
    if ($kpiExitCode -ne 0) {
      $errText = (@($kpiOutput) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join " | "
      if ([string]::IsNullOrWhiteSpace($errText)) { $errText = "-" }
      Log ("kpi[{0}] failed: rc={1} detail={2}" -f $StageTag, $kpiExitCode, $errText)
      return
    }

    $kpiRaw = (@($kpiOutput) | ForEach-Object { [string]$_ }) -join "`n"
    if ([string]::IsNullOrWhiteSpace($kpiRaw)) {
      Log ("kpi[{0}] failed: empty_output" -f $StageTag)
      return
    }

    $kpi = $kpiRaw | ConvertFrom-Json -ErrorAction Stop
    $monthlyNowText = [string]$kpi.monthly_return_now_text
    $monthlyNowSource = [string]$kpi.monthly_return_now_source
    $monthlyNewText = [string]$kpi.monthly_return_now_new_condition_text
    $monthlyAllText = [string]$kpi.monthly_return_now_all_text
    $resolvedTrades = [string]$kpi.rolling_30d_resolved_trades
    $gateDecision = [string]$kpi.realized_30d_gate_decision
    Log ("kpi[{0}] no_longshot.monthly_return_now_text={1} no_longshot.monthly_return_now_source={2} no_longshot.monthly_return_now_new_condition_text={3} no_longshot.monthly_return_now_all_text={4} no_longshot.rolling_30d_resolved_trades={5} realized_30d_gate.decision={6}" -f `
      $StageTag, $monthlyNowText, $monthlyNowSource, $monthlyNewText, $monthlyAllText, $resolvedTrades, $gateDecision)
  }
  catch {
    Log ("kpi[{0}] failed: parse_error={1}" -f $StageTag, $_.Exception.Message)
  }
}

$tool = Join-Path $RepoRoot "scripts\check_morning_status.py"
if (-not (Test-Path $tool)) {
  throw "tool not found: $tool"
}

$uncorrelatedStrategyIds = [string]$UncorrelatedStrategyIds
if ($null -eq $uncorrelatedStrategyIds) {
  $uncorrelatedStrategyIds = ""
}
$uncorrelatedStrategyIds = $uncorrelatedStrategyIds.Trim()

$args = @(
  $tool,
  "--strategy-id", $StrategyId,
  "--min-realized-days", "$MinRealizedDays",
  "--snapshot-json", $SnapshotJson,
  "--health-json", $HealthJson,
  "--uncorrelated-json", $UncorrelatedJson,
  "--uncorrelated-corr-threshold-abs", ([string]$UncorrelatedCorrThresholdAbs),
  "--uncorrelated-min-overlap-days", "$UncorrelatedMinOverlapDays",
  "--uncorrelated-min-realized-days-for-correlation", "$UncorrelatedMinRealizedDaysForCorrelation",
  "--gate-alarm-state-json", $GateAlarmStateJson,
  "--gate-alarm-log-file", $GateAlarmLogFile,
  "--simmer-ab-decision-json", $SimmerAbDecisionJson,
  "--simmer-ab-max-stale-hours", ([string]$SimmerAbMaxStaleHours)
)

if (-not [string]::IsNullOrWhiteSpace($uncorrelatedStrategyIds)) {
  $args += "--uncorrelated-strategy-ids"
  $args += $uncorrelatedStrategyIds
}
if ($NoRefresh.IsPresent) { $args += "--no-refresh" }
if ($SkipHealth.IsPresent) { $args += "--skip-health" }
if ($SkipGateAlarm.IsPresent) { $args += "--skip-gate-alarm" }
if ($SkipUncorrelatedPortfolio.IsPresent) { $args += "--skip-uncorrelated-portfolio" }
if ($SkipImplementationLedger.IsPresent) { $args += "--skip-implementation-ledger" }
if ($SkipSimmerAb.IsPresent) { $args += "--skip-simmer-ab" }
if ($SkipProcessScan.IsPresent) { $args += "--skip-process-scan" }
if ($DiscordGateAlarm.IsPresent) {
  $args += "--discord-gate-alarm"
  if (-not [string]::IsNullOrWhiteSpace($DiscordWebhookEnv)) {
    $args += "--discord-webhook-env"
    $args += $DiscordWebhookEnv
  }
}
if ($FailOnGateNotReady.IsPresent) { $args += "--fail-on-gate-not-ready" }
if ($FailOnStageNotFinal.IsPresent) { $args += "--fail-on-stage-not-final" }
if ($FailOnHealthNoGo.IsPresent) { $args += "--fail-on-health-no-go" }
if ($FailOnSimmerAbFinalNoGo.IsPresent) { $args += "--fail-on-simmer-ab-final-no-go" }

$discordWebhookEnvLog = if ($DiscordGateAlarm.IsPresent -and -not [string]::IsNullOrWhiteSpace($DiscordWebhookEnv)) { $DiscordWebhookEnv } else { "-" }
Log ("start strategy_id={0} min_days={1} no_refresh={2} skip_health={3} skip_gate_alarm={4} skip_uncorrelated_portfolio={5} skip_implementation_ledger={6} skip_simmer_ab={7} skip_process_scan={8} fail_simmer_final_no_go={9} simmer_max_stale_h={10} uncorrelated_strategy_ids={11} uncorrelated_corr_th={12} uncorrelated_min_overlap={13} uncorrelated_min_realized={14} discord_webhook_env={15}" -f `
  $StrategyId, $MinRealizedDays, $NoRefresh.IsPresent, $SkipHealth.IsPresent, $SkipGateAlarm.IsPresent, $SkipUncorrelatedPortfolio.IsPresent, $SkipImplementationLedger.IsPresent, $SkipSimmerAb.IsPresent, $SkipProcessScan.IsPresent, $FailOnSimmerAbFinalNoGo.IsPresent, $SimmerAbMaxStaleHours, $uncorrelatedStrategyIds, $UncorrelatedCorrThresholdAbs, $UncorrelatedMinOverlapDays, $UncorrelatedMinRealizedDaysForCorrelation, $discordWebhookEnvLog)

$prevPyUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
$prevPyIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
[Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")

$output = & $PythonExe @args 2>&1
$exitCode = $LASTEXITCODE
foreach ($line in @($output)) {
  $s = [string]$line
  if ([string]::IsNullOrWhiteSpace($s)) { continue }
  Log $s
}

[Environment]::SetEnvironmentVariable("PYTHONUTF8", $prevPyUtf8, "Process")
[Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $prevPyIoEncoding, "Process")

Log-NoLongshotMonthlyKpi -StageTag "post"

if ($exitCode -ne 0) {
  throw "morning status failed with exit code $exitCode"
}

Log "done"

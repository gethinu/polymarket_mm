[CmdletBinding()]
param(
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
  [string]$NoLongshotPracticalDecisionDate = "2026-03-02",
  [int]$NoLongshotPracticalSlideDays = 3,
  [int]$NoLongshotPracticalMinResolvedTrades = 30,
  [string]$SimmerAbDecisionJson = "logs/simmer-ab-decision-latest.json",
  [ValidateSet("7d", "14d")]
  [string]$SimmerAbInterimTarget = "7d",
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
  [switch]$FailOnSimmerAbInterimNoGo
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repoRoot "scripts\check_morning_status.py"
if (-not (Test-Path $py)) {
  throw "Script not found: $py"
}

$argsList = @(
  $py,
  "--strategy-id", $StrategyId,
  "--min-realized-days", [string]$MinRealizedDays,
  "--snapshot-json", $SnapshotJson,
  "--health-json", $HealthJson,
  "--uncorrelated-json", $UncorrelatedJson,
  "--uncorrelated-strategy-ids", $UncorrelatedStrategyIds,
  "--uncorrelated-corr-threshold-abs", ([string]$UncorrelatedCorrThresholdAbs),
  "--uncorrelated-min-overlap-days", [string]$UncorrelatedMinOverlapDays,
  "--uncorrelated-min-realized-days-for-correlation", [string]$UncorrelatedMinRealizedDaysForCorrelation,
  "--gate-alarm-state-json", $GateAlarmStateJson,
  "--gate-alarm-log-file", $GateAlarmLogFile,
  "--no-longshot-practical-decision-date", $NoLongshotPracticalDecisionDate,
  "--no-longshot-practical-slide-days", [string]([Math]::Max(1, [int]$NoLongshotPracticalSlideDays)),
  "--no-longshot-practical-min-resolved-trades", [string]([Math]::Max(1, [int]$NoLongshotPracticalMinResolvedTrades)),
  "--simmer-ab-decision-json", $SimmerAbDecisionJson,
  "--simmer-ab-interim-target", $SimmerAbInterimTarget,
  "--simmer-ab-max-stale-hours", ([string]$SimmerAbMaxStaleHours)
)

if ($NoRefresh.IsPresent) { $argsList += "--no-refresh" }
if ($SkipHealth.IsPresent) { $argsList += "--skip-health" }
if ($SkipGateAlarm.IsPresent) { $argsList += "--skip-gate-alarm" }
if ($SkipUncorrelatedPortfolio.IsPresent) { $argsList += "--skip-uncorrelated-portfolio" }
if ($SkipImplementationLedger.IsPresent) { $argsList += "--skip-implementation-ledger" }
if ($SkipSimmerAb.IsPresent) { $argsList += "--skip-simmer-ab" }
if ($SkipProcessScan.IsPresent) { $argsList += "--skip-process-scan" }
if ($DiscordGateAlarm.IsPresent) {
  $argsList += "--discord-gate-alarm"
  if (-not [string]::IsNullOrWhiteSpace($DiscordWebhookEnv)) {
    $argsList += "--discord-webhook-env"
    $argsList += $DiscordWebhookEnv
  }
}
if ($FailOnGateNotReady.IsPresent) { $argsList += "--fail-on-gate-not-ready" }
if ($FailOnStageNotFinal.IsPresent) { $argsList += "--fail-on-stage-not-final" }
if ($FailOnHealthNoGo.IsPresent) { $argsList += "--fail-on-health-no-go" }
if ($FailOnSimmerAbFinalNoGo.IsPresent) { $argsList += "--fail-on-simmer-ab-final-no-go" }
if ($FailOnSimmerAbInterimNoGo.IsPresent) { $argsList += "--fail-on-simmer-ab-interim-no-go" }

& python @argsList
exit $LASTEXITCODE

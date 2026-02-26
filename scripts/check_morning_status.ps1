[CmdletBinding()]
param(
  [string]$StrategyId = "weather_clob_arb_buckets_observe",
  [int]$MinRealizedDays = 30,
  [string]$SnapshotJson = "logs/strategy_register_latest.json",
  [string]$HealthJson = "logs/automation_health_latest.json",
  [string]$GateAlarmStateJson = "logs/strategy_gate_alarm_state.json",
  [string]$GateAlarmLogFile = "logs/strategy_gate_alarm.log",
  [string]$SimmerAbDecisionJson = "logs/simmer-ab-decision-latest.json",
  [double]$SimmerAbMaxStaleHours = 30.0,
  [string]$DiscordWebhookEnv = "CLOBBOT_DISCORD_WEBHOOK_URL_CHECK_MORNING_STATUS",
  [switch]$NoRefresh,
  [switch]$SkipHealth,
  [switch]$SkipGateAlarm,
  [switch]$SkipImplementationLedger,
  [switch]$SkipSimmerAb,
  [switch]$SkipProcessScan,
  [switch]$DiscordGateAlarm,
  [switch]$FailOnGateNotReady,
  [switch]$FailOnStageNotFinal,
  [switch]$FailOnHealthNoGo,
  [switch]$FailOnSimmerAbFinalNoGo
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
  "--gate-alarm-state-json", $GateAlarmStateJson,
  "--gate-alarm-log-file", $GateAlarmLogFile,
  "--simmer-ab-decision-json", $SimmerAbDecisionJson,
  "--simmer-ab-max-stale-hours", ([string]$SimmerAbMaxStaleHours)
)

if ($NoRefresh.IsPresent) { $argsList += "--no-refresh" }
if ($SkipHealth.IsPresent) { $argsList += "--skip-health" }
if ($SkipGateAlarm.IsPresent) { $argsList += "--skip-gate-alarm" }
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

& python @argsList
exit $LASTEXITCODE

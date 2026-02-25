param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$UserFile = "configs\weather_mimic_target_users.txt",
  [string]$ProfileName = "weather_7acct_auto",
  [int]$Limit = 500,
  [int]$MaxTrades = 4000,
  [double]$SleepSec = 0.2,
  [int]$TopMarkets = 10,
  [double]$MinWeatherSharePct = 60.0,
  [int]$MinTrades = 500,
  [double]$MinRealizedPnl = 0.0,
  [int]$ScanMaxPages = 80,
  [int]$ScanPageSize = 500,
  [double]$ScanIntervalSec = 300.0,
  [double]$MinLiquidity = 500.0,
  [double]$MinVolume24h = 100.0,
  [int]$TopN = 30,
  [switch]$LateprobDisableWeatherFilter,
  [ValidateSet("balanced", "liquidity", "edge")]
  [string]$ConsensusScoreMode = "edge",
  [double]$ConsensusWeightOverlap = [double]::NaN,
  [double]$ConsensusWeightNetYield = [double]::NaN,
  [double]$ConsensusWeightMaxProfit = [double]::NaN,
  [double]$ConsensusWeightLiquidity = [double]::NaN,
  [double]$ConsensusWeightVolume = [double]::NaN,
  [switch]$NoRunScans,
  [switch]$Discord,
  [switch]$FailOnReadinessNoGo,
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

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}
$runLog = Join-Path $logsDir "weather_mimic_pipeline_daily_run.log"

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

function Resolve-PathFromRepo([string]$Root, [string]$Raw) {
  if ([string]::IsNullOrWhiteSpace($Raw)) { return "" }
  $p = $Raw
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $Root $p)
}

function Send-DiscordSummary([string]$SummaryPath) {
  if (-not (Test-Path $SummaryPath)) {
    Log "discord skip: summary not found"
    return
  }
  $webhookUrl = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhookUrl)) {
    Log "discord skip: webhook missing"
    return
  }

  $mention = Get-EnvAny "CLOBBOT_DISCORD_MENTION"
  $summaryObj = Get-Content -Path $SummaryPath -Raw | ConvertFrom-Json

  $text = @()
  $text += "Weather Mimic Daily Summary"
  $text += ("profile={0}" -f [string]$summaryObj.meta.profile_name)
  $text += ("generated={0}" -f [string]$summaryObj.meta.generated_at_utc)
  $text += ("inputs={0} winners={1}" -f [int]$summaryObj.inputs.count, [int]$summaryObj.winner_filter.winner_count)
  $text += (
    "winner_filter weather>=${0} trades>=${1} realized>=${2}" -f `
      [double]$summaryObj.winner_filter.min_weather_share_pct, `
      [int]$summaryObj.winner_filter.min_trades, `
      [double]$summaryObj.winner_filter.min_realized_pnl
  )
  $text += ("consensus_json={0}" -f [string]$summaryObj.artifacts.consensus_json)

  $profileName = [string]$summaryObj.meta.profile_name
  if (-not [string]::IsNullOrWhiteSpace($profileName)) {
    $readinessPath = Join-Path $RepoRoot ("logs\{0}_top30_readiness_latest.json" -f $profileName)
    if (Test-Path $readinessPath) {
      try {
        $readinessObj = Get-Content -Path $readinessPath -Raw | ConvertFrom-Json
        $decision = [string]$readinessObj.decision
        $reason = [string]$readinessObj.decision_reason
        if ($reason.Length -gt 160) { $reason = $reason.Substring(0, 160) + "..." }
        $text += ("readiness={0}" -f $decision)
        if (-not [string]::IsNullOrWhiteSpace($reason)) {
          $text += ("readiness_reason={0}" -f $reason)
        }
      } catch {
        Log "discord note: failed to parse readiness json"
      }
    }
  }

  $consensusPath = Resolve-PathFromRepo -Root $RepoRoot -Raw ([string]$summaryObj.artifacts.consensus_json)
  if (-not [string]::IsNullOrWhiteSpace($consensusPath) -and (Test-Path $consensusPath)) {
    try {
      $consensus = Get-Content -Path $consensusPath -Raw | ConvertFrom-Json
      $topRows = @()
      if ($null -ne $consensus -and $consensus.PSObject.Properties["top"]) {
        $topRows = @($consensus.top)
      }
      if ($topRows.Count -gt 0) {
        $text += ""
        $text += "Top consensus:"
        foreach ($r in ($topRows | Select-Object -First 5)) {
          $q = [string]$r.question
          if ($q.Length -gt 90) { $q = $q.Substring(0, 90) }
          $text += ("- rank={0} score={1:0.00} yes={2:0.0000} | {3}" -f `
            [int]$r.rank, [double]$r.score_total, [double]$r.yes_price, $q)
        }
      }
    } catch {
      Log "discord note: failed to parse consensus json"
    }
  }

  $bodyText = [string]::Join("`n", $text)
  if ($bodyText.Length -gt 1700) {
    $bodyText = $bodyText.Substring(0, 1700) + "`n...(truncated)"
  }
  $content = '```text' + "`n" + $bodyText + "`n" + '```'
  if (-not [string]::IsNullOrWhiteSpace($mention)) {
    $content = "$mention $content"
  }
  $payload = @{ content = $content } | ConvertTo-Json -Depth 4

  try {
    Invoke-RestMethod -Method Post -Uri $webhookUrl -Body $payload -ContentType "application/json" -TimeoutSec 15 | Out-Null
    Log "discord post: sent"
  } catch {
    Log "discord post: failed ($($_.Exception.GetType().Name))"
  }
}

$tool = Join-Path $RepoRoot "scripts\run_weather_mimic_pipeline.py"
if (-not (Test-Path $tool)) {
  throw "tool not found: $tool"
}
$snapshotTool = Join-Path $RepoRoot "scripts\render_weather_consensus_snapshot.py"
$consensusOverviewTool = Join-Path $RepoRoot "scripts\render_weather_consensus_overview.py"
$readinessTool = Join-Path $RepoRoot "scripts\judge_weather_top30_readiness.py"
$realizedDailyTool = Join-Path $RepoRoot "scripts\record_simmer_realized_daily.py"
$strategyRealizedTool = Join-Path $RepoRoot "scripts\materialize_strategy_realized_daily.py"
$strategySnapshotTool = Join-Path $RepoRoot "scripts\render_strategy_register_snapshot.py"
$strategyGateAlarmTool = Join-Path $RepoRoot "scripts\check_strategy_gate_alarm.py"
$automationHealthTool = Join-Path $RepoRoot "scripts\report_automation_health.py"

$userFilePath = Resolve-PathFromRepo -Root $RepoRoot -Raw $UserFile
if ([string]::IsNullOrWhiteSpace($userFilePath) -or -not (Test-Path $userFilePath)) {
  throw "user file not found: $userFilePath"
}

$discordRequested = $Discord.IsPresent -or (Parse-Bool (Get-EnvAny "WEATHER_MIMIC_DAILY_DISCORD"))

Log "start profile=$ProfileName user_file=$userFilePath score_mode=$ConsensusScoreMode no_run_scans=$($NoRunScans.IsPresent) lateprob_disable_weather_filter=$($LateprobDisableWeatherFilter.IsPresent) discord_req=$discordRequested fail_on_readiness_no_go=$($FailOnReadinessNoGo.IsPresent)"

$args = @(
  $tool,
  "--user-file", $userFilePath,
  "--profile-name", $ProfileName,
  "--limit", "$Limit",
  "--max-trades", "$MaxTrades",
  "--sleep-sec", "$SleepSec",
  "--top-markets", "$TopMarkets",
  "--min-weather-share-pct", "$MinWeatherSharePct",
  "--min-trades", "$MinTrades",
  "--min-realized-pnl", "$MinRealizedPnl",
  "--scan-max-pages", "$ScanMaxPages",
  "--scan-page-size", "$ScanPageSize",
  "--scan-interval-sec", "$ScanIntervalSec",
  "--min-liquidity", "$MinLiquidity",
  "--min-volume-24h", "$MinVolume24h",
  "--top-n", "$TopN",
  "--consensus-score-mode", $ConsensusScoreMode,
  "--pretty"
)

if ($NoRunScans.IsPresent) { $args += "--no-run-scans" }
if ($LateprobDisableWeatherFilter.IsPresent) { $args += "--lateprob-disable-weather-filter" }
if (-not [double]::IsNaN($ConsensusWeightOverlap)) { $args += @("--consensus-weight-overlap", "$ConsensusWeightOverlap") }
if (-not [double]::IsNaN($ConsensusWeightNetYield)) { $args += @("--consensus-weight-net-yield", "$ConsensusWeightNetYield") }
if (-not [double]::IsNaN($ConsensusWeightMaxProfit)) { $args += @("--consensus-weight-max-profit", "$ConsensusWeightMaxProfit") }
if (-not [double]::IsNaN($ConsensusWeightLiquidity)) { $args += @("--consensus-weight-liquidity", "$ConsensusWeightLiquidity") }
if (-not [double]::IsNaN($ConsensusWeightVolume)) { $args += @("--consensus-weight-volume", "$ConsensusWeightVolume") }

Run-Python $args

$summaryPattern = ("{0}_pipeline_summary_*.json" -f $ProfileName)
$summaryFile = Get-ChildItem -Path $logsDir -File -Filter $summaryPattern | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $summaryFile) {
  throw "pipeline summary not found with pattern: $summaryPattern"
}

Log "summary done -> $($summaryFile.FullName)"

$summaryObj = $null
$consensusRel = ""
$consensusPath = ""
try {
  $summaryObj = Get-Content -Path $summaryFile.FullName -Raw | ConvertFrom-Json
  if ($null -ne $summaryObj -and $summaryObj.PSObject.Properties["artifacts"]) {
    $consensusRel = [string]$summaryObj.artifacts.consensus_json
  }
  $consensusPath = Resolve-PathFromRepo -Root $RepoRoot -Raw $consensusRel
} catch {
  Log "summary parse failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $snapshotTool)) {
    Log "snapshot skip: renderer not found"
  } else {
    if ([string]::IsNullOrWhiteSpace($consensusPath) -or -not (Test-Path $consensusPath)) {
      Log "snapshot skip: consensus json missing ($consensusPath)"
    } else {
      $snapshotOut = Join-Path $logsDir ("{0}_consensus_snapshot_latest.html" -f $ProfileName)
      Run-Python @(
        $snapshotTool,
        "--consensus-json", $consensusPath,
        "--profile-name", $ProfileName,
        "--top-n", "$TopN",
        "--out-html", $snapshotOut
      )
      Log "snapshot done -> $snapshotOut"
    }
  }
} catch {
  Log "snapshot failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $readinessTool)) {
    Log "readiness skip: judge not found"
  } elseif ([string]::IsNullOrWhiteSpace($consensusPath) -or -not (Test-Path $consensusPath)) {
    Log "readiness skip: consensus json missing ($consensusPath)"
  } else {
    $readinessLatest = Join-Path $logsDir ("{0}_top30_readiness_latest.json" -f $ProfileName)
    $executionPlan = Join-Path $logsDir ("{0}_execution_plan_latest.json" -f $ProfileName)
    Run-Python @(
      $readinessTool,
      "--consensus-json", $consensusPath,
      "--execution-plan-file", $executionPlan,
      "--out-latest-json", $readinessLatest,
      "--pretty"
    )
    if (Test-Path $readinessLatest) {
      try {
        $readinessObj = Get-Content -Path $readinessLatest -Raw | ConvertFrom-Json
        $decision = [string]$readinessObj.decision
        $reason = [string]$readinessObj.decision_reason
        Log ("readiness done -> {0} decision={1} reason={2}" -f $readinessLatest, $decision, $reason)
      } catch {
        Log "readiness done -> $readinessLatest"
      }
    } else {
      Log "readiness warning: latest json not found after run"
    }
  }
} catch {
  Log "readiness failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $realizedDailyTool)) {
    Log "realized_daily skip: recorder not found"
  } else {
    Run-Python @(
      $realizedDailyTool,
      "--pretty"
    )
    $realizedDailyOut = Join-Path $logsDir "clob_arb_realized_daily.jsonl"
    Log "realized_daily done -> $realizedDailyOut"
  }
} catch {
  Log "realized_daily failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $strategyRealizedTool)) {
    Log "strategy_realized_daily skip: materializer not found"
  } else {
    Run-Python @(
      $strategyRealizedTool,
      "--strategy-id", "weather_clob_arb_buckets_observe",
      "--source-jsonl", "logs/clob_arb_realized_daily.jsonl",
      "--out-jsonl", "logs/strategy_realized_pnl_daily.jsonl",
      "--out-latest-json", "logs/strategy_realized_latest.json",
      "--pretty"
    )
    $strategyRealizedOut = Join-Path $logsDir "strategy_realized_pnl_daily.jsonl"
    Log "strategy_realized_daily done -> $strategyRealizedOut"
  }
} catch {
  Log "strategy_realized_daily failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $strategySnapshotTool)) {
    Log "strategy_snapshot skip: renderer not found"
  } else {
    Run-Python @(
      $strategySnapshotTool,
      "--pretty"
    )
    $strategySnapshotOut = Join-Path $logsDir "strategy_register_latest.html"
    Log "strategy_snapshot done -> $strategySnapshotOut"
  }
} catch {
  Log "strategy_snapshot failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $consensusOverviewTool)) {
    Log "consensus_overview skip: renderer not found"
  } else {
    Run-Python @(
      $consensusOverviewTool,
      "--top-n", "$TopN"
    )
    $overviewOut = Join-Path $logsDir "weather_consensus_overview_latest.html"
    Log "consensus_overview done -> $overviewOut"
  }
} catch {
  Log "consensus_overview failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $strategyGateAlarmTool)) {
    Log "strategy_gate_alarm skip: checker not found"
  } else {
    $alarmArgs = @(
      $strategyGateAlarmTool,
      "--snapshot-json", "logs/strategy_register_latest.json",
      "--state-json", "logs/strategy_gate_alarm_state.json",
      "--log-file", "logs/strategy_gate_alarm.log",
      "--strategy-id", "weather_clob_arb_buckets_observe",
      "--pretty"
    )
    if ($discordRequested) {
      $alarmArgs += "--discord"
    }
    Run-Python $alarmArgs
    $strategyGateAlarmOut = Join-Path $logsDir "strategy_gate_alarm_state.json"
    Log "strategy_gate_alarm done -> $strategyGateAlarmOut"
  }
} catch {
  Log "strategy_gate_alarm failed (non-fatal): $($_.Exception.Message)"
}

try {
  if (-not (Test-Path $automationHealthTool)) {
    Log "automation_health skip: reporter not found"
  } else {
    Run-Python @(
      $automationHealthTool,
      "--pretty"
    )
    $healthOut = Join-Path $logsDir "automation_health_latest.json"
    Log "automation_health done -> $healthOut"
  }
} catch {
  Log "automation_health failed (non-fatal): $($_.Exception.Message)"
}

if ($discordRequested) {
  Send-DiscordSummary -SummaryPath $summaryFile.FullName
}

if ($FailOnReadinessNoGo.IsPresent) {
  $readinessLatest = Join-Path $logsDir ("{0}_top30_readiness_latest.json" -f $ProfileName)
  if (-not (Test-Path $readinessLatest)) {
    throw "readiness guard failed: latest readiness json not found ($readinessLatest)"
  }
  try {
    $readinessObj = Get-Content -Path $readinessLatest -Raw | ConvertFrom-Json
    $decision = ([string]$readinessObj.decision).Trim().ToUpperInvariant()
    $reason = [string]$readinessObj.decision_reason
    if ($decision -ne "GO") {
      throw "readiness guard failed: decision=$decision reason=$reason"
    }
    Log "readiness guard pass: decision=GO"
  } catch {
    throw $_
  }
}

Log "done"

param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,
  [switch]$SkipRefresh,
  [double]$YesMin = 0.4,
  [double]$YesMax = 0.6,
  [double]$PerTradeCost = 0.002,
  [int]$MinHistoryPoints = 71,
  [double]$MaxStaleHours = 1.0,
  [int]$MaxOpenPositions = 20,
  [int]$MaxOpenPerCategory = 4,
  [int]$GuardMaxOpenPositions = 16,
  [int]$GuardMaxOpenPerCategory = 3,
  [int]$AllMinTrainN = 20,
  [int]$AllMinTestN = 1,
  [int]$GuardMinTrainN = 20,
  [int]$GuardMinTestN = 20,
  [int]$ScreenMaxPages = 6,
  [int]$RealizedFastMaxPages = 120,
  [double]$RealizedFastYesMin = 0.16,
  [double]$RealizedFastYesMax = 0.20,
  [double]$RealizedFastMaxHoursToEnd = 72.0,
  [double]$ScreenMinLiquidity = 50000,
  [double]$ScreenMinVolume24h = 1000,
  [int]$GapMaxPages = 6,
  [int]$GapFallbackMaxPages = 20,
  [double]$GapYesMin = 0.01,
  [double]$GapYesMax = 0.99,
  [double]$GapMinLiquidity = 1000,
  [double]$GapMinVolume24h = 0,
  [double]$GapMinGrossEdgeCents = 0.3,
  [double]$GapMinNetEdgeCents = 0.0,
  [double]$GapSummaryMinNetEdgeCents = 0.5,
  [ValidateSet("auto", "fixed")]
  [string]$GapSummaryMode = "auto",
  [ValidateSet("fixed", "events_ratio")]
  [string]$GapSummaryTargetMode = "events_ratio",
  [int]$GapSummaryTargetUniqueEvents = 3,
  [double]$GapSummaryTargetEventsRatio = 0.2,
  [int]$GapSummaryTargetUniqueEventsMin = 2,
  [int]$GapSummaryTargetUniqueEventsMax = 6,
  [string]$GapSummaryThresholdGrid = "0.5,1.0,2.0",
  [double]$GapPerLegCost = 0.002,
  [double]$GapMaxDaysToEnd = 180.0,
  [double]$GapFallbackMaxDaysToEnd = 180.0,
  [double]$GapMaxHoursToEnd = 6.0,
  [double]$GapFallbackMaxHoursToEnd = 48.0,
  [switch]$GapFallbackNoHourCap,
  [string]$GapRelation = "both",
  [string]$GapOutcomeTag = "prod",
  [int]$GapErrorAlertMinRuns7d = 5,
  [double]$GapErrorAlertRate7d = 0.2,
  [switch]$FailOnGapScanError,
  [switch]$FailOnGapErrorRateHigh,
  [int]$GapMaxPairsPerEvent = 20,
  [int]$GapTopN = 30,
  [switch]$Discord
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

$tool = Join-Path $RepoRoot "scripts\polymarket_no_longshot_observe.py"
$logDir = Join-Path $RepoRoot "logs"
$sampleCsv = Join-Path $logDir "no_longshot_daily_samples.csv"
$screenCsv = Join-Path $logDir "no_longshot_daily_screen.csv"
$screenJson = Join-Path $logDir "no_longshot_daily_screen.json"
$fastScreenCsv = Join-Path $logDir "no_longshot_fast_screen_lowyes_latest.csv"
$fastScreenJson = Join-Path $logDir "no_longshot_fast_screen_lowyes_latest.json"
$gapCsv = Join-Path $logDir "no_longshot_daily_gap.csv"
$gapJson = Join-Path $logDir "no_longshot_daily_gap.json"
$oosAllJson = Join-Path $logDir "no_longshot_daily_oos_allfolds.json"
$oosGuardJson = Join-Path $logDir "no_longshot_daily_oos_guarded.json"
$summaryTxt = Join-Path $logDir "no_longshot_daily_summary.txt"
$runLog = Join-Path $logDir "no_longshot_daily_run.log"
$realizedTool = Join-Path $RepoRoot "scripts\record_no_longshot_realized_daily.py"
$realizedPositionsJson = Join-Path $logDir "no_longshot_forward_positions.json"
$realizedDailyJsonl = Join-Path $logDir "no_longshot_realized_daily.jsonl"
$realizedLatestJson = Join-Path $logDir "no_longshot_realized_latest.json"
$realizedMonthlyTxt = Join-Path $logDir "no_longshot_monthly_return_latest.txt"
$realizedEntryTopN = 0

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}

if ($realizedFastYesMin -lt 0.0 -or $realizedFastYesMax -gt 1.0 -or $realizedFastYesMin -gt $realizedFastYesMax) {
  throw "Invalid fast realized yes range: [$realizedFastYesMin,$realizedFastYesMax] (expected 0<=min<=max<=1)"
}
if ($realizedFastMaxPages -lt 1) {
  throw "Invalid RealizedFastMaxPages: $realizedFastMaxPages (expected >=1)"
}
if ($realizedFastMaxHoursToEnd -le 0.0) {
  throw "Invalid RealizedFastMaxHoursToEnd: $realizedFastMaxHoursToEnd (expected >0)"
}

$ExcludeKeywordsArg = ","

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
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

function Parse-FloatList([string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return @()
  }
  $vals = @()
  foreach ($token in $raw.Split(",")) {
    $t = $token.Trim()
    if ([string]::IsNullOrWhiteSpace($t)) {
      continue
    }
    try {
      $v = [double]$t
      if ($v -ge 0.0) {
        $vals += $v
      }
    } catch {
    }
  }
  if ($vals.Count -eq 0) {
    return @()
  }
  return @($vals | Sort-Object -Unique)
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

function Send-DiscordSummary([string]$summaryPath) {
  if (-not (Test-Path $summaryPath)) {
    Log "discord skip: summary not found"
    return
  }
  $webhookUrl = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhookUrl)) {
    Log "discord skip: webhook missing"
    return
  }

  $mention = Get-EnvAny "CLOBBOT_DISCORD_MENTION"
  $raw = Get-Content -Path $summaryPath -Raw
  $bodyText = [string]$raw
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

function Run-Python([string[]]$CmdArgs) {
  & $PythonExe @CmdArgs
  if ($LASTEXITCODE -ne 0) {
    throw "python failed with code ${LASTEXITCODE}: $($CmdArgs -join ' ')"
  }
}

function Run-PythonSafe([string[]]$CmdArgs, [string]$Label) {
  try {
    & $PythonExe @CmdArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
      Log "$Label failed: exit=$code"
      return $false
    }
    return $true
  } catch {
    Log "$Label failed: exception=$($_.Exception.GetType().Name)"
    return $false
  }
}

function Remove-FileSafe([string]$Path) {
  try {
    if (Test-Path $Path) {
      Remove-Item -Path $Path -Force
    }
  } catch {
  }
}

function Read-GapCounts([string]$SummaryPath) {
  $ret = @{
    markets_total = 0
    interval_markets = 0
    events_considered = 0
    pairs_scanned = 0
    candidates = 0
  }
  if (-not (Test-Path $SummaryPath)) {
    return $ret
  }
  try {
    $obj = Get-Content -Path $SummaryPath -Raw | ConvertFrom-Json
    if ($null -ne $obj -and $obj.PSObject.Properties["counts"]) {
      $ret.markets_total = [int]($obj.counts.markets_total)
      $ret.interval_markets = [int]($obj.counts.interval_markets)
      $ret.events_considered = [int]($obj.counts.events_considered)
      $ret.pairs_scanned = [int]($obj.counts.pairs_scanned)
      $ret.candidates = [int]($obj.counts.candidates)
    }
  } catch {
  }
  return $ret
}

function Read-GapErrorStats([string]$LogPath, [int]$LookbackDays, [string]$IncludeTag = "prod") {
  $ret = @{
    runs = 0
    error_runs = 0
    error_rate = 0.0
  }
  if ($LookbackDays -lt 1) {
    return $ret
  }
  if (-not (Test-Path $LogPath)) {
    return $ret
  }
  $includeTagNorm = "prod"
  if (-not [string]::IsNullOrWhiteSpace($IncludeTag)) {
    $includeTagNorm = $IncludeTag.Trim().ToLowerInvariant()
  }
  $cutoff = (Get-Date).AddDays(-1.0 * [double]$LookbackDays)
  $pattern = '^\[(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] gap scan outcome: stage=(?<stage>\S+) had_error=(?<had>true|false)(?: tag=(?<tag>\S+))?$'
  try {
    foreach ($line in (Get-Content -Path $LogPath)) {
      $m = [regex]::Match($line, $pattern)
      if (-not $m.Success) {
        continue
      }
      $ts = $null
      try {
        $ts = [DateTime]::ParseExact(
          $m.Groups["ts"].Value,
          "yyyy-MM-dd HH:mm:ss",
          [System.Globalization.CultureInfo]::InvariantCulture
        )
      } catch {
        continue
      }
      if ($ts -lt $cutoff) {
        continue
      }
      $tagNorm = "prod"
      $tagRaw = [string]$m.Groups["tag"].Value
      if (-not [string]::IsNullOrWhiteSpace($tagRaw)) {
        $tagNorm = $tagRaw.Trim().ToLowerInvariant()
      }
      if ($tagNorm -ne $includeTagNorm) {
        continue
      }
      $ret.runs = [int]$ret.runs + 1
      if ($m.Groups["had"].Value -eq "true") {
        $ret.error_runs = [int]$ret.error_runs + 1
      }
    }
  } catch {
  }
  if ([int]$ret.runs -gt 0) {
    $ret.error_rate = [double]$ret.error_runs / [double]$ret.runs
  }
  return $ret
}

function Select-BestGapRowsPerEvent([object[]]$Rows) {
  if ($null -eq $Rows -or $Rows.Count -eq 0) {
    return @()
  }
  $bestRows = @()
  $groups = $Rows | Group-Object -Property event_key
  foreach ($g in $groups) {
    $best = $g.Group |
      Sort-Object `
        @{ Expression = { [double]($_.net_edge_cents) }; Descending = $true }, `
        @{ Expression = { [double]($_.gross_edge_cents) }; Descending = $true }, `
        @{ Expression = { [double]($_.liquidity_sum) }; Descending = $true } |
      Select-Object -First 1
    if ($null -ne $best) {
      $bestRows += $best
    }
  }
  $bestRows = $bestRows |
    Sort-Object `
      @{ Expression = { [double]($_.net_edge_cents) }; Descending = $true }, `
      @{ Expression = { [double]($_.gross_edge_cents) }; Descending = $true }, `
      @{ Expression = { [double]($_.liquidity_sum) }; Descending = $true }
  return @($bestRows)
}

trap {
  try {
    Log ("fatal: {0}" -f $_.Exception.Message)
  } catch {
  }
  exit 1
}

Log "bootstrap init"

$discordRequested = $Discord.IsPresent
if (-not $discordRequested) {
  try {
    $discordRequested = Parse-Bool (Get-EnvAny "NO_LONGSHOT_DAILY_DISCORD")
  } catch {
    Log "warn: discord flag env lookup failed ($($_.Exception.GetType().Name))"
    $discordRequested = $false
  }
}

Log "start yes=[$YesMin,$YesMax] cost=$PerTradeCost min_hist=$MinHistoryPoints stale<=$MaxStaleHours open<=$MaxOpenPositions cat_open<=$MaxOpenPerCategory guard_open<=$GuardMaxOpenPositions guard_cat_open<=$GuardMaxOpenPerCategory all_n>=($AllMinTrainN/$AllMinTestN) guard_n>=($GuardMinTrainN/$GuardMinTestN) screen_pages=$ScreenMaxPages fast_screen_pages=$realizedFastMaxPages fast_yes=[$realizedFastYesMin,$realizedFastYesMax] fast_max_h=$realizedFastMaxHoursToEnd gap_pages=$GapMaxPages/$GapFallbackMaxPages gap=yes[$GapYesMin,$GapYesMax] gap_liq>=$GapMinLiquidity gap_vol>=$GapMinVolume24h gross>=$GapMinGrossEdgeCents net>=$GapMinNetEdgeCents summary_base_net>=$GapSummaryMinNetEdgeCents summary_mode=$GapSummaryMode summary_target_mode=$GapSummaryTargetMode summary_target_base=$GapSummaryTargetUniqueEvents summary_target_ratio=$GapSummaryTargetEventsRatio summary_target_minmax=[$GapSummaryTargetUniqueEventsMin,$GapSummaryTargetUniqueEventsMax] max_d=$GapMaxDaysToEnd/$GapFallbackMaxDaysToEnd max_h=$GapMaxHoursToEnd fallback_h=$GapFallbackMaxHoursToEnd fallback_no_cap=$($GapFallbackNoHourCap.IsPresent) rel=$GapRelation gap_tag=$GapOutcomeTag gap_alert_7d=$GapErrorAlertRate7d/$GapErrorAlertMinRuns7d fail_on_gap_scan=$($FailOnGapScanError.IsPresent) fail_on_gap_rate=$($FailOnGapErrorRateHigh.IsPresent) discord_req=$discordRequested"

if (-not $SkipRefresh) {
  Log "refresh samples start"
  Run-Python @(
    $tool, "walkforward",
    "--sampling-mode", "stratified",
    "--offset-step", "5000",
    "--max-offset", "425000",
    "--min-liquidity", "0",
    "--min-volume-24h", "0",
    "--min-history-points", "0",
    "--yes-min", "$YesMin",
    "--yes-max", "$YesMax",
    "--yes-min-grid", "$YesMin",
    "--yes-max-grid", "$YesMax",
    "--exclude-keywords", $ExcludeKeywordsArg,
    "--per-trade-cost", "$PerTradeCost",
    "--max-open-positions", "$MaxOpenPositions",
    "--max-open-per-category", "$MaxOpenPerCategory",
    "--min-train-n", "$AllMinTrainN",
    "--min-test-n", "$AllMinTestN",
    "--out-samples-csv", $sampleCsv,
    "--out-summary-json", (Join-Path $logDir "no_longshot_daily_refresh_tmp.json")
  )
  Log "refresh samples done -> $sampleCsv"
} else {
  Log "skip refresh"
}

if (-not (Test-Path $sampleCsv)) {
  throw "sample csv not found: $sampleCsv"
}

Log "walkforward allfolds"
Run-Python @(
  $tool, "walkforward",
  "--input-csv", $sampleCsv,
  "--yes-min", "$YesMin",
  "--yes-max", "$YesMax",
  "--yes-min-grid", "$YesMin",
  "--yes-max-grid", "$YesMax",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--per-trade-cost", "$PerTradeCost",
  "--max-open-positions", "$MaxOpenPositions",
  "--max-open-per-category", "$MaxOpenPerCategory",
  "--min-liquidity", "0",
  "--min-volume-24h", "0",
  "--min-history-points", "$MinHistoryPoints",
  "--max-stale-hours", "$MaxStaleHours",
  "--min-train-n", "$AllMinTrainN",
  "--min-test-n", "$AllMinTestN",
  "--out-summary-json", $oosAllJson
)

Log "walkforward guarded"
Run-Python @(
  $tool, "walkforward",
  "--input-csv", $sampleCsv,
  "--yes-min", "$YesMin",
  "--yes-max", "$YesMax",
  "--yes-min-grid", "$YesMin",
  "--yes-max-grid", "$YesMax",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--per-trade-cost", "$PerTradeCost",
  "--max-open-positions", "$GuardMaxOpenPositions",
  "--max-open-per-category", "$GuardMaxOpenPerCategory",
  "--min-liquidity", "0",
  "--min-volume-24h", "0",
  "--min-history-points", "$MinHistoryPoints",
  "--max-stale-hours", "$MaxStaleHours",
  "--min-train-n", "$GuardMinTrainN",
  "--min-test-n", "$GuardMinTestN",
  "--out-summary-json", $oosGuardJson
)

Log "screen active candidates"
Run-Python @(
  $tool, "screen",
  "--max-pages", "$ScreenMaxPages",
  "--yes-min", "$YesMin",
  "--yes-max", "$YesMax",
  "--min-days-to-end", "14",
  "--min-liquidity", "$ScreenMinLiquidity",
  "--min-volume-24h", "$ScreenMinVolume24h",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--top-n", "30",
  "--out-csv", $screenCsv,
  "--out-json", $screenJson
)

$fastScreenStatus = "not_run"
$fastScreenOk = $false
Log "screen fast candidates for near-term realized tracking"
Remove-FileSafe -Path $fastScreenCsv
Remove-FileSafe -Path $fastScreenJson
$fastScreenOk = Run-PythonSafe @(
  $tool, "screen",
  "--max-pages", "$realizedFastMaxPages",
  "--page-size", "500",
  "--yes-min", "$realizedFastYesMin",
  "--yes-max", "$realizedFastYesMax",
  "--min-days-to-end", "0",
  "--max-hours-to-end", "$realizedFastMaxHoursToEnd",
  "--min-liquidity", "1000",
  "--min-volume-24h", "0",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--per-trade-cost", "$PerTradeCost",
  "--sort-by", "net_yield_per_day_desc",
  "--top-n", "30",
  "--out-csv", $fastScreenCsv,
  "--out-json", $fastScreenJson
) -Label "screen fast"
if ($fastScreenOk) {
  $fastScreenStatus = "ok"
} else {
  $fastScreenStatus = "failed"
}

Log "scan logical gaps"
$gapScanStage = "primary"
Remove-FileSafe -Path $gapCsv
Remove-FileSafe -Path $gapJson
$gapPrimaryOk = Run-PythonSafe @(
  $tool, "gap",
  "--max-pages", "$GapMaxPages",
  "--relation", "$GapRelation",
  "--yes-min", "$GapYesMin",
  "--yes-max", "$GapYesMax",
  "--min-days-to-end", "0",
  "--max-days-to-end", "$GapMaxDaysToEnd",
  "--max-hours-to-end", "$GapMaxHoursToEnd",
  "--max-end-diff-hours", "$GapMaxHoursToEnd",
  "--min-liquidity", "$GapMinLiquidity",
  "--min-volume-24h", "$GapMinVolume24h",
  "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
  "--min-net-edge-cents", "$GapMinNetEdgeCents",
  "--per-leg-cost", "$GapPerLegCost",
  "--max-pairs-per-event", "$GapMaxPairsPerEvent",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--top-n", "$GapTopN",
  "--out-csv", $gapCsv,
  "--out-json", $gapJson
 ) -Label "gap primary"

$gapRows = @()
if ($gapPrimaryOk -and (Test-Path $gapCsv)) {
  $gapRows = Import-Csv -Path $gapCsv
}
$gapCounts = Read-GapCounts -SummaryPath $gapJson
$gapScanDaysUsed = [double]$GapMaxDaysToEnd
$gapScanHoursUsed = [double]$GapMaxHoursToEnd
$gapFallbackUsed = $false
if (-not $gapPrimaryOk) {
  $gapScanStage = "primary_error"
}
if (($gapRows.Count -eq 0) -and (($GapFallbackMaxDaysToEnd -gt $GapMaxDaysToEnd) -or ($GapFallbackMaxHoursToEnd -gt $GapMaxHoursToEnd) -or ($GapFallbackMaxPages -gt $GapMaxPages))) {
  $gapScanStage = "fallback_window"
  Log "scan logical gaps fallback max_d=$GapFallbackMaxDaysToEnd max_h=$GapFallbackMaxHoursToEnd max_pages=$GapFallbackMaxPages"
  Remove-FileSafe -Path $gapCsv
  Remove-FileSafe -Path $gapJson
  $gapFallbackWindowOk = Run-PythonSafe @(
    $tool, "gap",
    "--max-pages", "$GapFallbackMaxPages",
    "--relation", "$GapRelation",
    "--yes-min", "$GapYesMin",
    "--yes-max", "$GapYesMax",
    "--min-days-to-end", "0",
    "--max-days-to-end", "$GapFallbackMaxDaysToEnd",
    "--max-hours-to-end", "$GapFallbackMaxHoursToEnd",
    "--max-end-diff-hours", "$GapFallbackMaxHoursToEnd",
    "--min-liquidity", "$GapMinLiquidity",
    "--min-volume-24h", "$GapMinVolume24h",
    "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
    "--min-net-edge-cents", "$GapMinNetEdgeCents",
    "--per-leg-cost", "$GapPerLegCost",
    "--max-pairs-per-event", "$GapMaxPairsPerEvent",
    "--exclude-keywords", $ExcludeKeywordsArg,
    "--top-n", "$GapTopN",
    "--out-csv", $gapCsv,
    "--out-json", $gapJson
   ) -Label "gap fallback_window"
  $gapRows = @()
  if ($gapFallbackWindowOk -and (Test-Path $gapCsv)) {
    $gapRows = Import-Csv -Path $gapCsv
  }
  $gapCounts = Read-GapCounts -SummaryPath $gapJson
  $gapScanDaysUsed = [double]$GapFallbackMaxDaysToEnd
  $gapScanHoursUsed = [double]$GapFallbackMaxHoursToEnd
  $gapFallbackUsed = $true
  if (-not $gapFallbackWindowOk) {
    $gapScanStage = "fallback_window_error"
  }
}

$shouldRunNoHourFallback = $GapFallbackNoHourCap.IsPresent -or ($gapCounts.interval_markets -eq 0)
if (($gapRows.Count -eq 0) -and $shouldRunNoHourFallback) {
  $fallbackReason = "manual_switch"
  $gapScanStage = "fallback_no_hour_cap"
  if (-not $GapFallbackNoHourCap.IsPresent) {
    $fallbackReason = "auto_interval_markets_zero"
    $gapScanStage = "fallback_no_hour_cap_auto"
  }
  Log "scan logical gaps fallback no hour cap max_pages=$GapFallbackMaxPages reason=$fallbackReason"
  Remove-FileSafe -Path $gapCsv
  Remove-FileSafe -Path $gapJson
  $gapNoHourOk = Run-PythonSafe @(
    $tool, "gap",
    "--max-pages", "$GapFallbackMaxPages",
    "--relation", "$GapRelation",
    "--yes-min", "$GapYesMin",
    "--yes-max", "$GapYesMax",
    "--min-days-to-end", "0",
    "--max-days-to-end", "$GapFallbackMaxDaysToEnd",
    "--max-hours-to-end", "0",
    "--max-end-diff-hours", "0",
    "--min-liquidity", "$GapMinLiquidity",
    "--min-volume-24h", "$GapMinVolume24h",
    "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
    "--min-net-edge-cents", "$GapMinNetEdgeCents",
    "--per-leg-cost", "$GapPerLegCost",
    "--max-pairs-per-event", "$GapMaxPairsPerEvent",
    "--exclude-keywords", $ExcludeKeywordsArg,
    "--top-n", "$GapTopN",
    "--out-csv", $gapCsv,
    "--out-json", $gapJson
   ) -Label "gap fallback_no_hour_cap"
  $gapRows = @()
  if ($gapNoHourOk -and (Test-Path $gapCsv)) {
    $gapRows = Import-Csv -Path $gapCsv
  }
  $gapCounts = Read-GapCounts -SummaryPath $gapJson
  $gapScanDaysUsed = [double]$GapFallbackMaxDaysToEnd
  $gapScanHoursUsed = 0.0
  $gapFallbackUsed = $true
  if (-not $gapNoHourOk) {
    $gapScanStage = ("{0}_error" -f $gapScanStage)
  }
}
$gapScanHadError = [bool]($gapScanStage -like "*_error")
$gapScanHadErrorText = if ($gapScanHadError) { "true" } else { "false" }
Log ("gap scan outcome: stage={0} had_error={1} tag={2}" -f $gapScanStage, $gapScanHadErrorText, $GapOutcomeTag)
$gapErrorStatsTag = "prod"
$gapErrorAlertMinRuns7dApplied = [int]$GapErrorAlertMinRuns7d
if ($gapErrorAlertMinRuns7dApplied -lt 1) {
  $gapErrorAlertMinRuns7dApplied = 1
}
$gapErrorAlertRate7dApplied = [double]$GapErrorAlertRate7d
if ($gapErrorAlertRate7dApplied -lt 0.0) {
  $gapErrorAlertRate7dApplied = 0.0
}
if ($gapErrorAlertRate7dApplied -gt 1.0) {
  $gapErrorAlertRate7dApplied = 1.0
}
$gapErrorStats7d = Read-GapErrorStats -LogPath $runLog -LookbackDays 7 -IncludeTag $gapErrorStatsTag
$gapErrorStats30d = Read-GapErrorStats -LogPath $runLog -LookbackDays 30 -IncludeTag $gapErrorStatsTag
$gapErrorRuns7dText = "n/a (no samples)"
$gapErrorRuns30dText = "n/a (no samples)"
if ([int]$gapErrorStats7d.runs -gt 0) {
  $gapErrorRuns7dText = ("{0}/{1} ({2:0.0%})" -f [int]$gapErrorStats7d.error_runs, [int]$gapErrorStats7d.runs, [double]$gapErrorStats7d.error_rate)
}
if ([int]$gapErrorStats30d.runs -gt 0) {
  $gapErrorRuns30dText = ("{0}/{1} ({2:0.0%})" -f [int]$gapErrorStats30d.error_runs, [int]$gapErrorStats30d.runs, [double]$gapErrorStats30d.error_rate)
}
$gapErrorRateAlert7d = $false
if (
  ([int]$gapErrorStats7d.runs -ge $gapErrorAlertMinRuns7dApplied) -and
  ([double]$gapErrorStats7d.error_rate -ge $gapErrorAlertRate7dApplied)
) {
  $gapErrorRateAlert7d = $true
  Log ("warn: gap error rate high tag={0} 7d={1} threshold>={2:0.0%} min_runs={3}" -f $gapErrorStatsTag, $gapErrorRuns7dText, [double]$gapErrorAlertRate7dApplied, [int]$gapErrorAlertMinRuns7dApplied)
}
$gapSummaryThresholds = Parse-FloatList -raw $GapSummaryThresholdGrid
if ($gapSummaryThresholds.Count -eq 0) {
  $gapSummaryThresholds = @(
    [double]$GapSummaryMinNetEdgeCents
    1.0
    2.0
  )
}
$gapSummaryThresholds = @(
  $gapSummaryThresholds + [double]$GapSummaryMinNetEdgeCents
) | Where-Object { [double]$_ -ge 0.0 } | Sort-Object -Unique

$gapSummaryStats = @()
foreach ($thr in $gapSummaryThresholds) {
  $rowsAtThr = @(
    $gapRows | Where-Object { [double]($_.net_edge_cents) -ge [double]$thr }
  )
  $uniqueAtThr = @(
    $rowsAtThr | Group-Object event_key
  )
  $gapSummaryStats += [pscustomobject]@{
    threshold = [double]$thr
    rows = [int]$rowsAtThr.Count
    unique_events = [int]$uniqueAtThr.Count
  }
}

$gapSummaryTargetMin = [int]$GapSummaryTargetUniqueEventsMin
$gapSummaryTargetMax = [int]$GapSummaryTargetUniqueEventsMax
if ($gapSummaryTargetMin -lt 1) {
  $gapSummaryTargetMin = 1
}
if ($gapSummaryTargetMax -lt 1) {
  $gapSummaryTargetMax = 1
}
if ($gapSummaryTargetMax -lt $gapSummaryTargetMin) {
  $tmp = $gapSummaryTargetMin
  $gapSummaryTargetMin = $gapSummaryTargetMax
  $gapSummaryTargetMax = $tmp
}

$gapSummaryAppliedTargetUniqueEvents = [int]$GapSummaryTargetUniqueEvents
if ($gapSummaryAppliedTargetUniqueEvents -lt 1) {
  $gapSummaryAppliedTargetUniqueEvents = 1
}
if ($GapSummaryMode -eq "auto" -and $GapSummaryTargetMode -eq "events_ratio") {
  $eventRef = [int]$gapCounts.events_considered
  if ($eventRef -gt 0) {
    $ratioTarget = [int][math]::Round(
      ([double]$eventRef * [double]$GapSummaryTargetEventsRatio),
      0,
      [System.MidpointRounding]::AwayFromZero
    )
    if ($ratioTarget -lt 1) {
      $ratioTarget = 1
    }
    if ($ratioTarget -lt $gapSummaryTargetMin) {
      $ratioTarget = $gapSummaryTargetMin
    }
    if ($ratioTarget -gt $gapSummaryTargetMax) {
      $ratioTarget = $gapSummaryTargetMax
    }
    $gapSummaryAppliedTargetUniqueEvents = $ratioTarget
  }
}

$gapSummarySelectedThreshold = [double]$GapSummaryMinNetEdgeCents
if ($GapSummaryMode -eq "auto" -and $gapSummaryAppliedTargetUniqueEvents -gt 0 -and $gapSummaryStats.Count -gt 0) {
  $eligible = @(
    $gapSummaryStats |
      Where-Object { [int]$_.unique_events -ge [int]$gapSummaryAppliedTargetUniqueEvents } |
      Sort-Object threshold
  )
  if ($eligible.Count -gt 0) {
    $gapSummarySelectedThreshold = [double]$eligible[-1].threshold
  } else {
    $nonZero = @(
      $gapSummaryStats |
        Where-Object { [int]$_.unique_events -gt 0 } |
        Sort-Object threshold
    )
    if ($nonZero.Count -gt 0) {
      $gapSummarySelectedThreshold = [double]$nonZero[0].threshold
    } else {
      $gapSummarySelectedThreshold = [double]($gapSummaryStats | Sort-Object threshold | Select-Object -First 1).threshold
    }
  }
}

$gapRowsSummaryFiltered = @(
  $gapRows | Where-Object { [double]($_.net_edge_cents) -ge $gapSummarySelectedThreshold }
)
$gapRowsBestPerEvent = Select-BestGapRowsPerEvent -Rows $gapRowsSummaryFiltered
$gapSummaryThresholdLines = @()
foreach ($stat in $gapSummaryStats) {
  $selectedTag = ""
  if ([math]::Abs([double]$stat.threshold - [double]$gapSummarySelectedThreshold) -lt 1e-9) {
    $selectedTag = " [selected]"
  }
  $gapSummaryThresholdLines += (
    "- net>={0:0.00}c: rows={1} unique_events={2}{3}" -f `
    [double]$stat.threshold, `
    [int]$stat.rows, `
    [int]$stat.unique_events, `
    $selectedTag
  )
}

$realizedMonthlyReturnText = "n/a"
$realizedMonthlyReturnPct = $null
$realizedObservedDays = 0
$realizedOpenPositions = 0
$realizedResolvedPositions = 0
$realizedRollingTrades = 0
$realizedScreenCsv = $screenCsv
$realizedEntrySource = "primary_screen"
$realizedEntryCandidates = 0
if (Test-Path $screenCsv) {
  try {
    $baseRows = Import-Csv -Path $screenCsv
    $realizedEntryCandidates = [int]$baseRows.Count
  } catch {
    $realizedEntryCandidates = 0
  }
}
if ($fastScreenOk -and (Test-Path $fastScreenCsv)) {
  try {
    $fastRows = Import-Csv -Path $fastScreenCsv
    if ($fastRows.Count -gt 0) {
      $realizedScreenCsv = $fastScreenCsv
      $realizedEntrySource = "fast_72h_lowyes"
      $realizedEntryCandidates = [int]$fastRows.Count
    }
  } catch {
  }
} elseif (-not $fastScreenOk) {
  Log "fast screen unavailable -> fallback to primary screen"
}
if (Test-Path $realizedTool) {
  Log "update realized monthly tracker source=$realizedEntrySource candidates=$realizedEntryCandidates"
  try {
    Run-Python @(
      $realizedTool,
      "--screen-csv", $realizedScreenCsv,
      "--positions-json", $realizedPositionsJson,
      "--out-daily-jsonl", $realizedDailyJsonl,
      "--out-latest-json", $realizedLatestJson,
      "--out-monthly-txt", $realizedMonthlyTxt,
      "--entry-top-n", "$realizedEntryTopN",
      "--per-trade-cost", "$PerTradeCost"
    )
    if (Test-Path $realizedLatestJson) {
      try {
        $realizedObj = Get-Content -Path $realizedLatestJson -Raw | ConvertFrom-Json
        $metricsProp = $realizedObj.PSObject.Properties["metrics"]
        if ($metricsProp -and $null -ne $metricsProp.Value) {
          $m = $metricsProp.Value
          $obsProp = $m.PSObject.Properties["observed_days"]
          if ($obsProp -and $null -ne $obsProp.Value) {
            $realizedObservedDays = [int]$obsProp.Value
          }
          $openProp = $m.PSObject.Properties["open_positions"]
          if ($openProp -and $null -ne $openProp.Value) {
            $realizedOpenPositions = [int]$openProp.Value
          }
          $resolvedProp = $m.PSObject.Properties["resolved_positions"]
          if ($resolvedProp -and $null -ne $resolvedProp.Value) {
            $realizedResolvedPositions = [int]$resolvedProp.Value
          }
          $rollProp = $m.PSObject.Properties["rolling_30d"]
          if ($rollProp -and $null -ne $rollProp.Value) {
            $roll = $rollProp.Value
            $rollTradesProp = $roll.PSObject.Properties["resolved_trades"]
            if ($rollTradesProp -and $null -ne $rollTradesProp.Value) {
              $realizedRollingTrades = [int]$rollTradesProp.Value
            }
            $retProp = $roll.PSObject.Properties["return_pct"]
            if ($retProp -and $null -ne $retProp.Value) {
              $realizedMonthlyReturnPct = [double]$retProp.Value
              $realizedMonthlyReturnText = "{0:+0.00%;-0.00%;+0.00%}" -f $realizedMonthlyReturnPct
            }
          }
        }
      } catch {
      }
    }
    Log "realized monthly tracker done monthly_30d=$realizedMonthlyReturnText observed_days=$realizedObservedDays open=$realizedOpenPositions resolved=$realizedResolvedPositions source=$realizedEntrySource"
  } catch {
    Log "realized monthly tracker failed (non-fatal): $($_.Exception.Message)"
  }
} else {
  Log "realized monthly tracker skip: tool not found"
}

Log "compose summary"
$oosAll = Get-Content -Path $oosAllJson -Raw | ConvertFrom-Json
$oosGuard = Get-Content -Path $oosGuardJson -Raw | ConvertFrom-Json
$monthlyNowText = $realizedMonthlyReturnText
$monthlyNowSource = "realized_rolling_30d"
if ($null -eq $realizedMonthlyReturnPct) {
  $monthlyNowText = "n/a"
  $monthlyNowSource = "n/a"
  try {
    $wProp = $oosGuard.PSObject.Properties["walkforward_oos_window"]
    if ($wProp -and $null -ne $wProp.Value) {
      $w = $wProp.Value
      $annProp = $w.PSObject.Properties["annualized_return"]
      if ($annProp -and $null -ne $annProp.Value) {
        $ann = [double]$annProp.Value
        if ($ann -gt -1.0) {
          $monthlyNow = [math]::Pow(1.0 + $ann, 1.0 / 12.0) - 1.0
          $monthlyNowText = "{0:+0.00%;-0.00%;+0.00%}" -f $monthlyNow
          $monthlyNowSource = "backtest_oos_ann_to_monthly"
        }
      }
    }
  } catch {
  }
}
$gapSummary = $null
if (Test-Path $gapJson) {
  try {
    $gapSummary = Get-Content -Path $gapJson -Raw | ConvertFrom-Json
  } catch {
    $gapSummary = $null
  }
}
$gapMarketsTotal = 0
$gapIntervalMarkets = 0
$gapEvents = 0
$gapPairs = 0
if ($null -ne $gapSummary -and $gapSummary.PSObject.Properties["counts"]) {
  $c = $gapSummary.counts
  $gapMarketsTotal = [int]($c.markets_total)
  $gapIntervalMarkets = [int]($c.interval_markets)
  $gapEvents = [int]($c.events_considered)
  $gapPairs = [int]($c.pairs_scanned)
}
$screenRows = @()
if (Test-Path $screenCsv) {
  $screenRows = Import-Csv -Path $screenCsv
}

function Format-Metric([string]$Name, $Obj) {
  $m = $Obj.walkforward_oos
  $base = "{0}: n={1} ret={2:+0.0000%;-0.0000%;+0.0000%} win={3:0.00%} worst={4:+0.000;-0.000;+0.000}" -f `
    $Name, `
    [int]$m.n, `
    [double]$m.capital_return, `
    [double]$m.win_rate, `
    [double]$m.worst_loss
  $windowProp = $Obj.PSObject.Properties["walkforward_oos_window"]
  if (-not $windowProp) {
    return $base
  }
  $w = $windowProp.Value
  if ($null -eq $w) {
    return $base
  }
  $spanDays = 0.0
  try {
    $spanDays = [double]$w.span_days
  } catch {
    $spanDays = 0.0
  }
  if ($spanDays -le 0.0) {
    return $base
  }
  $annText = "na"
  $annProp = $w.PSObject.Properties["annualized_return"]
  if ($annProp -and $null -ne $annProp.Value) {
    $annText = "{0:+0.00%;-0.00%;+0.00%}" -f [double]$annProp.Value
  }
  $warn = ""
  $lowConfProp = $w.PSObject.Properties["annualized_low_confidence"]
  if ($lowConfProp -and $null -ne $lowConfProp.Value -and [bool]$lowConfProp.Value) {
    $minDays = 90.0
    $minDaysProp = $w.PSObject.Properties["annualized_min_confidence_days"]
    if ($minDaysProp -and $null -ne $minDaysProp.Value) {
      $minDays = [double]$minDaysProp.Value
    }
    $warn = " [LOW_CONF span<{0:F0}d]" -f $minDays
  }
  $minEnd = [string]$w.min_end_iso
  $maxEnd = [string]$w.max_end_iso
  $spanText = "{0:F1}" -f $spanDays
  return $base + (" | span={0}d ann={1}{2} ({3} -> {4})" -f $spanText, $annText, $warn, $minEnd, $maxEnd)
}

$settings = $oosAll.settings
$lines = @(
  "No-Longshot Daily Summary ($([DateTime]::UtcNow.ToString('o')))"
  ""
  "Settings:"
  ("- yes range: [{0},{1}]" -f [double]$settings.yes_min, [double]$settings.yes_max)
  ("- per_trade_cost: {0}" -f [double]$settings.per_trade_cost)
  ("- min_history_points: {0}" -f [int]$settings.min_history_points)
  ("- max_stale_hours: {0}" -f [double]$settings.max_stale_hours)
  ("- max_open_positions: {0}" -f [int]$settings.max_open_positions)
  ("- max_open_per_category: {0}" -f [int]$settings.max_open_per_category)
  ("- guard_max_open_positions: {0}" -f [int]$GuardMaxOpenPositions)
  ("- guard_max_open_per_category: {0}" -f [int]$GuardMaxOpenPerCategory)
  ("- allfold min_n (train/test): {0}/{1}" -f [int]$AllMinTrainN, [int]$AllMinTestN)
  ("- guarded min_n (train/test): {0}/{1}" -f [int]$GuardMinTrainN, [int]$GuardMinTestN)
  ("- screen max pages: {0}" -f [int]$ScreenMaxPages)
  ("- fast screen max pages: {0}" -f [int]$realizedFastMaxPages)
  ("- fast screen yes range: [{0},{1}]" -f [double]$realizedFastYesMin, [double]$realizedFastYesMax)
  ("- fast screen max hours to end: {0}" -f [double]$realizedFastMaxHoursToEnd)
  ("- fast screen status: {0}" -f $fastScreenStatus)
  ("- gap max pages: {0}" -f [int]$GapMaxPages)
  ("- gap fallback max pages: {0}" -f [int]$GapFallbackMaxPages)
  ("- gap min liquidity: {0}" -f [double]$GapMinLiquidity)
  ("- gap min volume24h: {0}" -f [double]$GapMinVolume24h)
  ("- gap max days: {0}" -f [double]$GapMaxDaysToEnd)
  ("- gap fallback max days: {0}" -f [double]$GapFallbackMaxDaysToEnd)
  ("- gap summary base min net edge (cents): {0}" -f [double]$GapSummaryMinNetEdgeCents)
  ("- gap summary mode: {0}" -f [string]$GapSummaryMode)
  ("- gap summary target mode: {0}" -f [string]$GapSummaryTargetMode)
  ("- gap summary target unique events (base): {0}" -f [int]$GapSummaryTargetUniqueEvents)
  ("- gap summary target events ratio: {0}" -f [double]$GapSummaryTargetEventsRatio)
  ("- gap summary target unique events min/max: {0}/{1}" -f [int]$gapSummaryTargetMin, [int]$gapSummaryTargetMax)
  ("- gap summary target unique events (applied): {0}" -f [int]$gapSummaryAppliedTargetUniqueEvents)
  ("- gap summary threshold grid: {0}" -f [string]$GapSummaryThresholdGrid)
  ("- gap summary selected net edge (cents): {0}" -f [double]$gapSummarySelectedThreshold)
  ("- gap scan stage: {0}" -f $gapScanStage)
  ("- gap window days: {0}" -f $gapScanDaysUsed)
  ("- gap window hours: {0}" -f $gapScanHoursUsed)
  ("- gap fallback used: {0}" -f $gapFallbackUsed)
  ("- gap error stats tag: {0}" -f $gapErrorStatsTag)
  ("- gap error alert 7d threshold/min_runs: {0:0.0%}/{1}" -f [double]$gapErrorAlertRate7dApplied, [int]$gapErrorAlertMinRuns7dApplied)
  ("- fail_on_gap_scan_error: {0}" -f [bool]$FailOnGapScanError.IsPresent)
  ("- fail_on_gap_error_rate_high: {0}" -f [bool]$FailOnGapErrorRateHigh.IsPresent)
  ("- gap error runs 7d: {0}" -f $gapErrorRuns7dText)
  ("- gap error runs 30d: {0}" -f $gapErrorRuns30dText)
  ("- gap universe: markets={0} interval={1} events={2} pairs={3}" -f $gapMarketsTotal, $gapIntervalMarkets, $gapEvents, $gapPairs)
  ""
  "Backtest:"
  ("- " + (Format-Metric "Allfolds" $oosAll))
  ("- " + (Format-Metric "Guarded" $oosGuard))
  ""
  "Forward measured (realized):"
  ("- rolling_30d_monthly_return: {0}" -f $realizedMonthlyReturnText)
  ("- monthly_return_now: {0}" -f $monthlyNowText)
  ("- monthly_return_now_source: {0}" -f $monthlyNowSource)
  ("- realized_entry_source: {0}" -f $realizedEntrySource)
  ("- realized_entry_candidates: {0}" -f [int]$realizedEntryCandidates)
  ("- realized_entry_top_n: {0}" -f [int]$realizedEntryTopN)
  ("- rolling_30d_resolved_trades: {0}" -f [int]$realizedRollingTrades)
  ("- observed_days: {0}" -f [int]$realizedObservedDays)
  ("- open_positions: {0}" -f [int]$realizedOpenPositions)
  ("- resolved_positions: {0}" -f [int]$realizedResolvedPositions)
  ""
  ("Screen candidates now: {0}" -f $screenRows.Count)
)

foreach ($r in ($screenRows | Select-Object -First 10)) {
  $q = [string]$r.question
  if ($q.Length -gt 120) {
    $q = $q.Substring(0, 120)
  }
  $lines += ("- YES={0:0.0000} NO={1:0.0000} liq={2:0} vol24h={3:0} | {4}" -f `
    [double]$r.yes_price, `
    [double]$r.no_price, `
    [double]$r.liquidity_num, `
    [double]$r.volume_24h, `
    $q)
}

$lines += ""
if ($gapErrorRateAlert7d) {
  $lines += ("WARNING: gap scan error rate high for tag={0} over 7d: {1} (threshold>={2:0.0%}, min_runs={3})." -f $gapErrorStatsTag, $gapErrorRuns7dText, [double]$gapErrorAlertRate7dApplied, [int]$gapErrorAlertMinRuns7dApplied)
}
if ($gapScanHadError) {
  $lines += ("WARNING: gap scan had errors (stage={0}); gap candidates may be incomplete." -f $gapScanStage)
}
$lines += ("Logical gaps now: raw={0} filtered={1} unique_events={2} (net>={3:0.00}c)" -f $gapRows.Count, $gapRowsSummaryFiltered.Count, $gapRowsBestPerEvent.Count, [double]$gapSummarySelectedThreshold)
$lines += "Logical gaps threshold stats:"
$lines += ("- target_unique_events(applied)={0}" -f [int]$gapSummaryAppliedTargetUniqueEvents)
$lines += $gapSummaryThresholdLines
foreach ($r in ($gapRowsBestPerEvent | Select-Object -First 10)) {
  $qa = [string]$r.market_a_question
  if ($qa.Length -gt 80) {
    $qa = $qa.Substring(0, 80)
  }
  $qb = [string]$r.market_b_question
  if ($qb.Length -gt 80) {
    $qb = $qb.Substring(0, 80)
  }
  $lines += ("- {0} net={1:+0.00;-0.00;+0.00}c gross={2:+0.00;-0.00;+0.00}c cost={3:0.0000} | {4} || {5}" -f `
    [string]$r.relation, `
    [double]$r.net_edge_cents, `
    [double]$r.gross_edge_cents, `
    [double]$r.basket_cost, `
    $qa, `
    $qb)
}

$lines | Out-File -FilePath $summaryTxt -Encoding utf8

Log "done summary=$summaryTxt"
if ($discordRequested) {
  Send-DiscordSummary -summaryPath $summaryTxt
}

if ($FailOnGapScanError.IsPresent -and $gapScanHadError) {
  Log ("fail: gap scan had errors stage={0}" -f $gapScanStage)
  exit 3
}

if ($FailOnGapErrorRateHigh.IsPresent -and $gapErrorRateAlert7d) {
  Log ("fail: gap error rate high for tag={0} over 7d ({1})" -f $gapErrorStatsTag, $gapErrorRuns7dText)
  exit 2
}

exit 0

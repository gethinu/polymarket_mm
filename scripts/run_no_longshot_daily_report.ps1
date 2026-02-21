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
  [int]$AllMinTrainN = 30,
  [int]$AllMinTestN = 1,
  [int]$GuardMinTrainN = 60,
  [int]$GuardMinTestN = 20,
  [int]$ScreenMaxPages = 6,
  [double]$ScreenMinLiquidity = 50000,
  [double]$ScreenMinVolume24h = 1000,
  [int]$GapMaxPages = 6,
  [int]$GapFallbackMaxPages = 20,
  [double]$GapYesMin = 0.01,
  [double]$GapYesMax = 0.99,
  [double]$GapMinGrossEdgeCents = 0.3,
  [double]$GapMinNetEdgeCents = 0.0,
  [double]$GapPerLegCost = 0.002,
  [double]$GapMaxHoursToEnd = 6.0,
  [double]$GapFallbackMaxHoursToEnd = 48.0,
  [switch]$GapFallbackNoHourCap,
  [string]$GapRelation = "both",
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
$gapCsv = Join-Path $logDir "no_longshot_daily_gap.csv"
$gapJson = Join-Path $logDir "no_longshot_daily_gap.json"
$oosAllJson = Join-Path $logDir "no_longshot_daily_oos_allfolds.json"
$oosGuardJson = Join-Path $logDir "no_longshot_daily_oos_guarded.json"
$summaryTxt = Join-Path $logDir "no_longshot_daily_summary.txt"
$runLog = Join-Path $logDir "no_longshot_daily_run.log"

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
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

$discordRequested = $Discord.IsPresent -or (Parse-Bool (Get-EnvAny "NO_LONGSHOT_DAILY_DISCORD"))

Log "start yes=[$YesMin,$YesMax] cost=$PerTradeCost min_hist=$MinHistoryPoints stale<=$MaxStaleHours open<=$MaxOpenPositions cat_open<=$MaxOpenPerCategory all_n>=($AllMinTrainN/$AllMinTestN) guard_n>=($GuardMinTrainN/$GuardMinTestN) screen_pages=$ScreenMaxPages gap_pages=$GapMaxPages/$GapFallbackMaxPages gap=yes[$GapYesMin,$GapYesMax] gross>=$GapMinGrossEdgeCents net>=$GapMinNetEdgeCents max_h=$GapMaxHoursToEnd fallback_h=$GapFallbackMaxHoursToEnd fallback_no_cap=$($GapFallbackNoHourCap.IsPresent) rel=$GapRelation discord_req=$discordRequested"

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
  "--max-open-positions", "$MaxOpenPositions",
  "--max-open-per-category", "$MaxOpenPerCategory",
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

Log "scan logical gaps"
Run-Python @(
  $tool, "gap",
  "--max-pages", "$GapMaxPages",
  "--relation", "$GapRelation",
  "--yes-min", "$GapYesMin",
  "--yes-max", "$GapYesMax",
  "--min-days-to-end", "0",
  "--max-hours-to-end", "$GapMaxHoursToEnd",
  "--max-end-diff-hours", "$GapMaxHoursToEnd",
  "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
  "--min-net-edge-cents", "$GapMinNetEdgeCents",
  "--per-leg-cost", "$GapPerLegCost",
  "--max-pairs-per-event", "$GapMaxPairsPerEvent",
  "--exclude-keywords", $ExcludeKeywordsArg,
  "--top-n", "$GapTopN",
  "--out-csv", $gapCsv,
  "--out-json", $gapJson
)

$gapRows = @()
if (Test-Path $gapCsv) {
  $gapRows = Import-Csv -Path $gapCsv
}
$gapScanHoursUsed = [double]$GapMaxHoursToEnd
$gapFallbackUsed = $false
$gapScanStage = "primary"
if (($gapRows.Count -eq 0) -and (($GapFallbackMaxHoursToEnd -gt $GapMaxHoursToEnd) -or ($GapFallbackMaxPages -gt $GapMaxPages))) {
  Log "scan logical gaps fallback max_h=$GapFallbackMaxHoursToEnd max_pages=$GapFallbackMaxPages"
  Run-Python @(
    $tool, "gap",
    "--max-pages", "$GapFallbackMaxPages",
    "--relation", "$GapRelation",
    "--yes-min", "$GapYesMin",
    "--yes-max", "$GapYesMax",
    "--min-days-to-end", "0",
    "--max-hours-to-end", "$GapFallbackMaxHoursToEnd",
    "--max-end-diff-hours", "$GapFallbackMaxHoursToEnd",
    "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
    "--min-net-edge-cents", "$GapMinNetEdgeCents",
    "--per-leg-cost", "$GapPerLegCost",
    "--max-pairs-per-event", "$GapMaxPairsPerEvent",
    "--exclude-keywords", $ExcludeKeywordsArg,
    "--top-n", "$GapTopN",
    "--out-csv", $gapCsv,
    "--out-json", $gapJson
  )
  $gapRows = @()
  if (Test-Path $gapCsv) {
    $gapRows = Import-Csv -Path $gapCsv
  }
  $gapScanHoursUsed = [double]$GapFallbackMaxHoursToEnd
  $gapFallbackUsed = $true
  $gapScanStage = "fallback_window"
}

if (($gapRows.Count -eq 0) -and $GapFallbackNoHourCap.IsPresent) {
  Log "scan logical gaps fallback no hour cap max_pages=$GapFallbackMaxPages"
  Run-Python @(
    $tool, "gap",
    "--max-pages", "$GapFallbackMaxPages",
    "--relation", "$GapRelation",
    "--yes-min", "$GapYesMin",
    "--yes-max", "$GapYesMax",
    "--min-days-to-end", "0",
    "--max-hours-to-end", "0",
    "--max-end-diff-hours", "0",
    "--min-gross-edge-cents", "$GapMinGrossEdgeCents",
    "--min-net-edge-cents", "$GapMinNetEdgeCents",
    "--per-leg-cost", "$GapPerLegCost",
    "--max-pairs-per-event", "$GapMaxPairsPerEvent",
    "--exclude-keywords", $ExcludeKeywordsArg,
    "--top-n", "$GapTopN",
    "--out-csv", $gapCsv,
    "--out-json", $gapJson
  )
  $gapRows = @()
  if (Test-Path $gapCsv) {
    $gapRows = Import-Csv -Path $gapCsv
  }
  $gapScanHoursUsed = 0.0
  $gapFallbackUsed = $true
  $gapScanStage = "fallback_no_hour_cap"
}

Log "compose summary"
$oosAll = Get-Content -Path $oosAllJson -Raw | ConvertFrom-Json
$oosGuard = Get-Content -Path $oosGuardJson -Raw | ConvertFrom-Json
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
  ("- allfold min_n (train/test): {0}/{1}" -f [int]$AllMinTrainN, [int]$AllMinTestN)
  ("- guarded min_n (train/test): {0}/{1}" -f [int]$GuardMinTrainN, [int]$GuardMinTestN)
  ("- screen max pages: {0}" -f [int]$ScreenMaxPages)
  ("- gap max pages: {0}" -f [int]$GapMaxPages)
  ("- gap fallback max pages: {0}" -f [int]$GapFallbackMaxPages)
  ("- gap scan stage: {0}" -f $gapScanStage)
  ("- gap window hours: {0}" -f $gapScanHoursUsed)
  ("- gap fallback used: {0}" -f $gapFallbackUsed)
  ("- gap universe: markets={0} interval={1} events={2} pairs={3}" -f $gapMarketsTotal, $gapIntervalMarkets, $gapEvents, $gapPairs)
  ""
  "Backtest:"
  ("- " + (Format-Metric "Allfolds" $oosAll))
  ("- " + (Format-Metric "Guarded" $oosGuard))
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
$lines += ("Logical gaps now: {0}" -f $gapRows.Count)
foreach ($r in ($gapRows | Select-Object -First 10)) {
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

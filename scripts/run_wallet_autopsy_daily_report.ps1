param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,
  [string]$Glob = "autopsy_*_top*.json",
  [switch]$LatestPerMarket,
  [string]$Statuses = "ARBITRAGE_CANDIDATE",
  [double]$MinProfitablePct = 70.0,
  [double]$MinHedgeEdgePct = 1.0,
  [int]$MinTrades = 10,
  [string]$TimingProfile = "endgame_consistent",
  [string]$TimingSides = "BUY",
  [int]$TimingMaxTrades = 1500,
  [int]$MinTimingTradeCount = 30,
  [int]$Top = 50,
  [int]$BatchTop = 20,
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
  if (-not (Test-Path $summaryPath)) { return }
  $webhookUrl = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhookUrl)) { return }
  $mention = Get-EnvAny "CLOBBOT_DISCORD_MENTION"
  $bodyText = Get-Content -Path $summaryPath -Raw
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
  } catch {
  }
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runLog = Join-Path $logsDir "wallet_autopsy_daily_run.log"
$candidateOut = Join-Path $logsDir ("wallet_autopsy_daily_candidates_{0}.json" -f $stamp)
$timingOut = Join-Path $logsDir ("wallet_autopsy_daily_timing_{0}.json" -f $stamp)
$summaryOut = Join-Path $logsDir ("wallet_autopsy_daily_summary_{0}.txt" -f $stamp)
$summaryJsonOut = Join-Path $logsDir ("wallet_autopsy_daily_summary_{0}.json" -f $stamp)

$candidateScript = Join-Path $RepoRoot "scripts\report_wallet_autopsy_candidates.py"
$batchScript = Join-Path $RepoRoot "scripts\report_wallet_entry_timing_batch.py"

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

Log "start glob=$Glob latest=$($LatestPerMarket.IsPresent) status=$Statuses timing_profile=$TimingProfile timing_sides=$TimingSides"

$candidateArgs = @(
  $candidateScript,
  "--glob", $Glob,
  "--statuses", $Statuses,
  "--min-profitable-pct", "$MinProfitablePct",
  "--min-hedge-edge-pct", "$MinHedgeEdgePct",
  "--min-trades", "$MinTrades",
  "--timing-profile", "$TimingProfile",
  "--timing-sides", "$TimingSides",
  "--timing-max-trades", "$TimingMaxTrades",
  "--min-timing-trade-count", "$MinTimingTradeCount",
  "--top", "$Top",
  "--out", $candidateOut
)
if ($LatestPerMarket) {
  $candidateArgs += "--latest-per-market"
}

Run-Python $candidateArgs
Log "candidate report done -> $candidateOut"

$candidateObj = Get-Content -Path $candidateOut -Raw | ConvertFrom-Json
$candidateRows = @()
if ($null -ne $candidateObj -and $candidateObj.PSObject.Properties["rows"]) {
  $candidateRows = @($candidateObj.rows)
}
$timingRows = @()

if ($candidateRows.Count -gt 0) {
  Run-Python @(
    $batchScript,
    $candidateOut,
    "--top", "$BatchTop",
    "--timing-profile", "$TimingProfile",
    "--sides", "$TimingSides",
    "--max-trades", "$TimingMaxTrades",
    "--out", $timingOut
  )
  Log "timing batch done -> $timingOut"
} else {
  Log "timing batch skipped: no candidates"
}

$lines = @()
$lines += "Wallet Autopsy Daily Summary ($([DateTime]::UtcNow.ToString('o')))"
$lines += ""
$lines += "Settings:"
$lines += ("- glob: {0}" -f $Glob)
$lines += ("- latest_per_market: {0}" -f $LatestPerMarket.IsPresent)
$lines += ("- statuses: {0}" -f $Statuses)
$lines += ("- min_profitable_pct: {0}" -f $MinProfitablePct)
$lines += ("- min_hedge_edge_pct: {0}" -f $MinHedgeEdgePct)
$lines += ("- min_trades: {0}" -f $MinTrades)
$lines += ("- timing_profile: {0}" -f $TimingProfile)
$lines += ("- timing_sides: {0}" -f $TimingSides)
$lines += ("- timing_max_trades: {0}" -f $TimingMaxTrades)
$lines += ("- min_timing_trade_count: {0}" -f $MinTimingTradeCount)
$lines += ("- top: {0}" -f $Top)
$lines += ("- batch_top: {0}" -f $BatchTop)
$lines += ""
$lines += ("Candidates: {0}" -f $candidateRows.Count)

foreach ($r in ($candidateRows | Select-Object -First 10)) {
  $market = [string]$r.market
  if ($market.Length -gt 80) { $market = $market.Substring(0, 80) }
  $lines += ("- wallet={0} edge={1:+0.00;-0.00;+0.00}% prof={2:0.0}% p10/p50/p90={3:0}/{4:0}/{5:0}s trades={6} | {7}" -f `
    [string]$r.wallet, `
    [double]$r.hedge_edge_pct, `
    [double]$r.time_profitable_pct, `
    [double]$r.time_to_end_sec_p10, `
    [double]$r.time_to_end_sec_p50, `
    [double]$r.time_to_end_sec_p90, `
    [int]$r.trade_count, `
    $market)
}

if (Test-Path $timingOut) {
  $timingObj = Get-Content -Path $timingOut -Raw | ConvertFrom-Json
  if ($null -ne $timingObj -and $timingObj.PSObject.Properties["rows"]) {
    $timingRows = @($timingObj.rows)
  }
  $lines += ""
  $lines += ("Timing rows: {0}" -f $timingRows.Count)
  foreach ($r in ($timingRows | Select-Object -First 10)) {
    $market = [string]$r.market
    if ($market.Length -gt 80) { $market = $market.Substring(0, 80) }
    $lines += ("- wallet={0} tte_p50={1:0}s tte_p90={2:0}s interval_med={3:0.0}s trades={4} | {5}" -f `
      [string]$r.wallet, `
      [double]$r.time_to_end_sec_p50, `
      [double]$r.time_to_end_sec_p90, `
      [double]$r.median_interval_sec, `
      [int]$r.trade_count_included, `
      $market)
  }
}

$lines | Out-File -FilePath $summaryOut -Encoding utf8
Log "summary done -> $summaryOut"

$timingOutPathForSummary = ""
if (Test-Path $timingOut) {
  $timingOutPathForSummary = $timingOut
}

$summaryObj = [ordered]@{
  generated_at_utc = [DateTime]::UtcNow.ToString("o")
  settings = [ordered]@{
    glob = $Glob
    latest_per_market = $LatestPerMarket.IsPresent
    statuses = $Statuses
    min_profitable_pct = $MinProfitablePct
    min_hedge_edge_pct = $MinHedgeEdgePct
    min_trades = $MinTrades
    timing_profile = $TimingProfile
    timing_sides = $TimingSides
    timing_max_trades = $TimingMaxTrades
    min_timing_trade_count = $MinTimingTradeCount
    top = $Top
    batch_top = $BatchTop
  }
  counts = [ordered]@{
    candidates = $candidateRows.Count
    timing_rows = $timingRows.Count
  }
  outputs = [ordered]@{
    candidates_json = $candidateOut
    timing_json = $timingOutPathForSummary
    summary_txt = $summaryOut
    run_log = $runLog
  }
  top_candidates = @($candidateRows | Select-Object -First 10)
  top_timing_rows = @($timingRows | Select-Object -First 10)
}

$summaryObj | ConvertTo-Json -Depth 8 | Out-File -FilePath $summaryJsonOut -Encoding utf8
Log "summary json done -> $summaryJsonOut"

$discordRequested = $Discord.IsPresent -or (Parse-Bool (Get-EnvAny "WALLET_AUTOPSY_DAILY_DISCORD"))
if ($discordRequested) {
  Send-DiscordSummary -summaryPath $summaryOut
  Log "discord attempted"
}

Log "done"

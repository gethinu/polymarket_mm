[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$Background,
  [switch]$NoBackground,

  [switch]$SkipObserve,
  [int]$ObserveRunSeconds = 3600,
  [double]$ObserveMinEdgeCents = 1.0,
  [double]$ObserveShares = 5.0,
  [ValidateSet("buckets", "yes-no", "both")]
  [string]$ObserveStrategy = "buckets",
  [double]$ObserveSummaryEverySec = 30.0,
  [int]$ObserveMaxSubscribeTokens = 400,
  [string]$ObserveLogFile = "logs/clob-arb-weather-profit-observe.log",
  [string]$ObserveStateFile = "logs/clob_arb_weather_profit_state.json",

  [double]$ReportHours = 24.0,
  [string]$ReportThresholdsCents = "1,1.5,2,3,4",
  [string]$ReportCaptureRatios = "0.25,0.35,0.50",
  [double]$ReportBaseCaptureRatio = 0.35,
  [double]$AssumedBankrollUsd = 100.0,
  [double]$TargetMonthlyReturnPct = 15.0,
  [double]$MinSpanHours = 6.0,
  [int]$MinRows = 120,
  [double]$MinPositiveRowsPct = 30.0,
  [double]$MinOpportunitiesPerDay = 2.0,
  [int]$MinUniqueEvents = 4,
  [double]$MinRowsHitPct = 5.0,
  [string]$ReportJson = "logs/weather_arb_profit_window_latest.json",
  [string]$ReportTxt = "logs/weather_arb_profit_window_latest.txt",
  [switch]$FailOnNoGo,
  [switch]$Discord,
  [string]$TransitionStateJson = "logs/weather_arb_profit_window_transition_state.json",
  [string]$TransitionLogFile = "logs/weather_arb_profit_window_transition.log"
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

function Resolve-RepoPath {
  param([Parameter(Mandatory = $true)][string]$PathText, [Parameter(Mandatory = $true)][string]$BaseDir)
  if ([System.IO.Path]::IsPathRooted($PathText)) {
    return $PathText
  }
  return (Join-Path $BaseDir $PathText)
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

function Write-JsonFile {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][object]$Payload
  )
  $json = $Payload | ConvertTo-Json -Depth 8
  Set-Content -Path $Path -Value ($json + [Environment]::NewLine) -Encoding utf8
}

function Append-JsonLine {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][object]$Payload
  )
  $line = $Payload | ConvertTo-Json -Depth 8 -Compress
  Add-Content -Path $Path -Value $line -Encoding utf8
}

function Send-DiscordTransitionAlert {
  param(
    [Parameter(Mandatory = $true)][string]$PreviousDecision,
    [Parameter(Mandatory = $true)][string]$CurrentDecision,
    [Parameter(Mandatory = $true)][double]$TargetMonthlyReturnPct,
    [Parameter(Mandatory = $true)][string]$ReportTxtPath,
    [Nullable[Double]]$ProjectedMonthlyPct = $null
  )

  $webhook = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhook)) {
    return "webhook_missing"
  }

  $inv = [System.Globalization.CultureInfo]::InvariantCulture
  $projectedText = "n/a"
  if ($ProjectedMonthlyPct.HasValue) {
    $projectedText = $ProjectedMonthlyPct.Value.ToString("0.00", $inv) + "%"
  }
  $targetText = $TargetMonthlyReturnPct.ToString("0.00", $inv) + "%"

  $content = @(
    "[Weather Profit Window Transition]"
    "$PreviousDecision -> $CurrentDecision"
    "projected_monthly=$projectedText target=$targetText"
  ) -join "`n"

  if (Test-Path $ReportTxtPath) {
    try {
      $bodyText = [string](Get-Content -Path $ReportTxtPath -Raw)
      if ($bodyText.Length -gt 1200) {
        $bodyText = $bodyText.Substring(0, 1200) + "`n...(truncated)"
      }
      $content = $content + [Environment]::NewLine + '```text' + [Environment]::NewLine + $bodyText + [Environment]::NewLine + '```'
    }
    catch {
    }
  }

  $mention = Get-EnvAny "CLOBBOT_DISCORD_MENTION"
  if (-not [string]::IsNullOrWhiteSpace($mention)) {
    $content = "$mention $content"
  }
  $payload = @{ content = $content } | ConvertTo-Json -Depth 4

  try {
    Invoke-RestMethod -Method Post -Uri $webhook -Body $payload -ContentType "application/json" -TimeoutSec 15 | Out-Null
    return "sent"
  }
  catch {
    return ("failed:{0}" -f $_.Exception.GetType().Name)
  }
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$baseDir = (Resolve-Path $RepoRoot).Path
$observeRunner = Join-Path $baseDir "scripts\run_weather_arb_observe.ps1"
$reportTool = Join-Path $baseDir "scripts\report_weather_arb_profit_window.py"
$strategySnapshotTool = Join-Path $baseDir "scripts\render_strategy_register_snapshot.py"

if (-not (Test-Path $observeRunner)) {
  throw "observe runner not found: $observeRunner"
}
if (-not (Test-Path $reportTool)) {
  throw "report tool not found: $reportTool"
}

$obsLogPath = Resolve-RepoPath -PathText $ObserveLogFile -BaseDir $baseDir
$obsStatePath = Resolve-RepoPath -PathText $ObserveStateFile -BaseDir $baseDir
$reportJsonPath = Resolve-RepoPath -PathText $ReportJson -BaseDir $baseDir
$reportTxtPath = Resolve-RepoPath -PathText $ReportTxt -BaseDir $baseDir
$transitionStatePath = Resolve-RepoPath -PathText $TransitionStateJson -BaseDir $baseDir
$transitionLogPath = Resolve-RepoPath -PathText $TransitionLogFile -BaseDir $baseDir

$obsLogDir = Split-Path -Parent $obsLogPath
if (-not (Test-Path $obsLogDir)) { New-Item -ItemType Directory -Path $obsLogDir -Force | Out-Null }
$obsStateDir = Split-Path -Parent $obsStatePath
if (-not (Test-Path $obsStateDir)) { New-Item -ItemType Directory -Path $obsStateDir -Force | Out-Null }
$reportJsonDir = Split-Path -Parent $reportJsonPath
if (-not (Test-Path $reportJsonDir)) { New-Item -ItemType Directory -Path $reportJsonDir -Force | Out-Null }
$reportTxtDir = Split-Path -Parent $reportTxtPath
if (-not (Test-Path $reportTxtDir)) { New-Item -ItemType Directory -Path $reportTxtDir -Force | Out-Null }
$transitionStateDir = Split-Path -Parent $transitionStatePath
if (-not (Test-Path $transitionStateDir)) { New-Item -ItemType Directory -Path $transitionStateDir -Force | Out-Null }
$transitionLogDir = Split-Path -Parent $transitionLogPath
if (-not (Test-Path $transitionLogDir)) { New-Item -ItemType Directory -Path $transitionLogDir -Force | Out-Null }

if (-not $SkipObserve.IsPresent) {
  Write-Host ("[weather-profit] observe start strategy={0} run_seconds={1} min_edge_cents={2}" -f $ObserveStrategy, $ObserveRunSeconds, (To-Arg $ObserveMinEdgeCents))
  $obsArgs = @(
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", $observeRunner,
    "-NoBackground",
    "-RepoRoot", $baseDir,
    "-PythonExe", $PythonExe,
    "-RunSeconds", (To-Arg $ObserveRunSeconds),
    "-MinEdgeCents", (To-Arg $ObserveMinEdgeCents),
    "-Shares", (To-Arg $ObserveShares),
    "-Strategy", $ObserveStrategy,
    "-SummaryEverySec", (To-Arg $ObserveSummaryEverySec),
    "-MaxSubscribeTokens", (To-Arg $ObserveMaxSubscribeTokens),
    "-LogFile", $obsLogPath,
    "-StateFile", $obsStatePath
  )
  & powershell.exe @obsArgs
  if ($LASTEXITCODE -ne 0) {
    throw "observe runner failed (exit=$LASTEXITCODE)"
  }
  Write-Host "[weather-profit] observe done"
}
else {
  Write-Host "[weather-profit] observe skipped (using existing log)"
}

Write-Host "[weather-profit] report start"
$reportArgs = @(
  $reportTool,
  "--log-file", $obsLogPath,
  "--hours", (To-Arg $ReportHours),
  "--thresholds-cents", $ReportThresholdsCents,
  "--capture-ratios", $ReportCaptureRatios,
  "--base-capture-ratio", (To-Arg $ReportBaseCaptureRatio),
  "--assumed-bankroll-usd", (To-Arg $AssumedBankrollUsd),
  "--target-monthly-return-pct", (To-Arg $TargetMonthlyReturnPct),
  "--min-span-hours", (To-Arg $MinSpanHours),
  "--min-rows", (To-Arg $MinRows),
  "--min-positive-rows-pct", (To-Arg $MinPositiveRowsPct),
  "--min-opportunities-per-day", (To-Arg $MinOpportunitiesPerDay),
  "--min-unique-events", (To-Arg $MinUniqueEvents),
  "--min-rows-hit-pct", (To-Arg $MinRowsHitPct),
  "--out-json", $reportJsonPath,
  "--out-txt", $reportTxtPath,
  "--pretty"
)
& $PythonExe @reportArgs
if ($LASTEXITCODE -ne 0) {
  throw "report tool failed (exit=$LASTEXITCODE)"
}
Write-Host ("[weather-profit] report done json={0}" -f $reportJsonPath)

$decision = "UNKNOWN"
$projectedMonthlyPct = $null
if (Test-Path $reportJsonPath) {
  try {
    $payload = Get-Content -Path $reportJsonPath -Raw | ConvertFrom-Json
    if ($payload.decision -and $payload.decision.decision) {
      $decision = [string]$payload.decision.decision
    }
    if ($payload.decision -and $null -ne $payload.decision.projected_monthly_return) {
      $projectedMonthlyPct = [double]$payload.decision.projected_monthly_return * 100.0
    }
  }
  catch {
    $decision = "PARSE_ERROR"
  }
}
$decision = ([string]$decision).Trim().ToUpperInvariant()
if ([string]::IsNullOrWhiteSpace($decision)) {
  $decision = "UNKNOWN"
}

$previousDecision = ""
if (Test-Path $transitionStatePath) {
  try {
    $prevState = Get-Content -Path $transitionStatePath -Raw | ConvertFrom-Json
    if ($prevState -and $prevState.last_decision) {
      $previousDecision = ([string]$prevState.last_decision).Trim().ToUpperInvariant()
    }
  }
  catch {
    $previousDecision = ""
  }
}
$goTransition = ($previousDecision -eq "NO_GO" -and $decision -eq "GO")

$nowUtc = [DateTime]::UtcNow.ToString("o")
$statePayload = [ordered]@{
  updated_utc = $nowUtc
  last_decision = $decision
  previous_decision = $(if ([string]::IsNullOrWhiteSpace($previousDecision)) { $null } else { $previousDecision })
  last_projected_monthly_pct = $(if ($null -ne $projectedMonthlyPct) { [double]$projectedMonthlyPct } else { $null })
  target_monthly_return_pct = [double]$TargetMonthlyReturnPct
  report_json = $reportJsonPath
  report_txt = $reportTxtPath
  go_transition = [bool]$goTransition
}
Write-JsonFile -Path $transitionStatePath -Payload $statePayload

if ($goTransition) {
  Write-Host ("[weather-profit] transition detected: {0} -> {1}" -f $previousDecision, $decision)
  $eventPayload = [ordered]@{
    ts_utc = $nowUtc
    event = "weather_profit_window_go_transition"
    previous_decision = $previousDecision
    current_decision = $decision
    projected_monthly_pct = $(if ($null -ne $projectedMonthlyPct) { [double]$projectedMonthlyPct } else { $null })
    target_monthly_return_pct = [double]$TargetMonthlyReturnPct
    report_json = $reportJsonPath
    report_txt = $reportTxtPath
  }
  Append-JsonLine -Path $transitionLogPath -Payload $eventPayload

  if ($Discord.IsPresent) {
    $discordStatus = Send-DiscordTransitionAlert `
      -PreviousDecision $previousDecision `
      -CurrentDecision $decision `
      -TargetMonthlyReturnPct $TargetMonthlyReturnPct `
      -ReportTxtPath $reportTxtPath `
      -ProjectedMonthlyPct $projectedMonthlyPct
    Write-Host ("[weather-profit] transition discord={0}" -f $discordStatus)
  }
  else {
    Write-Host "[weather-profit] transition discord skipped (flag not set)"
  }
}
elseif ($Discord.IsPresent) {
  Write-Host "[weather-profit] discord armed: no NO_GO->GO transition"
}

if ($null -ne $projectedMonthlyPct) {
  Write-Host ("[weather-profit] decision={0} projected_monthly={1}%" -f $decision, $projectedMonthlyPct.ToString("0.00", [System.Globalization.CultureInfo]::InvariantCulture))
}
else {
  Write-Host ("[weather-profit] decision={0}" -f $decision)
}

if (Test-Path $strategySnapshotTool) {
  try {
    & $PythonExe $strategySnapshotTool | Out-Null
  }
  catch {
    # Snapshot refresh is best-effort.
  }
}

if ($FailOnNoGo.IsPresent -and $decision -ne "GO") {
  Write-Error ("[weather-profit] FailOnNoGo triggered: decision={0}" -f $decision)
  exit 9
}

exit 0

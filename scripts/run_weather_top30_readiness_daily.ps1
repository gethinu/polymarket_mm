param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$Profiles = "weather_7acct_auto,weather_visual_test",
  [switch]$FailOnNoGo,
  [switch]$Discord,
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

function Parse-Profiles([string]$raw) {
  $out = @()
  if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
  $parts = $raw -split "[,\r\n\t ]+"
  foreach ($p in $parts) {
    $v = [string]$p
    if ([string]::IsNullOrWhiteSpace($v)) { continue }
    $v = $v.Trim()
    if ([string]::IsNullOrWhiteSpace($v)) { continue }
    if ($out -contains $v) { continue }
    $out += $v
  }
  return $out
}

function Resolve-PathFromRepo([string]$Root, [string]$Raw) {
  if ([string]::IsNullOrWhiteSpace($Raw)) { return "" }
  if ([System.IO.Path]::IsPathRooted($Raw)) { return $Raw }
  return (Join-Path $Root $Raw)
}

function Run-Python([string[]]$CmdArgs) {
  & $PythonExe @CmdArgs
  if ($LASTEXITCODE -ne 0) {
    throw "python failed with code ${LASTEXITCODE}: $($CmdArgs -join ' ')"
  }
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}
$runLog = Join-Path $logsDir "weather_top30_readiness_daily_run.log"

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
}

function Send-DiscordSummary([string]$SummaryTxtPath) {
  if (-not (Test-Path $SummaryTxtPath)) {
    Log "discord skip: summary txt not found"
    return
  }
  $webhookUrl = Get-DiscordWebhookUrl
  if ([string]::IsNullOrWhiteSpace($webhookUrl)) {
    Log "discord skip: webhook missing"
    return
  }

  $mention = Get-EnvAny "CLOBBOT_DISCORD_MENTION"
  $bodyText = [string](Get-Content -Path $SummaryTxtPath -Raw)
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

$judgeTool = Join-Path $RepoRoot "scripts\judge_weather_top30_readiness.py"
$reportTool = Join-Path $RepoRoot "scripts\report_weather_top30_readiness.py"
$realizedDailyTool = Join-Path $RepoRoot "scripts\record_simmer_realized_daily.py"
$strategySnapshotTool = Join-Path $RepoRoot "scripts\render_strategy_register_snapshot.py"
if (-not (Test-Path $judgeTool)) { throw "judge tool not found: $judgeTool" }
if (-not (Test-Path $reportTool)) { throw "report tool not found: $reportTool" }

$profileList = Parse-Profiles $Profiles
if ($profileList.Count -le 0) {
  throw "no profiles specified"
}

$discordRequested = $Discord.IsPresent -or (Parse-Bool (Get-EnvAny "WEATHER_TOP30_READINESS_DAILY_DISCORD"))
Log "start profiles=$($profileList -join ',') fail_on_no_go=$($FailOnNoGo.IsPresent) discord_req=$discordRequested"

$strictDecisions = @{}
foreach ($profile in $profileList) {
  $consensusPath = Join-Path $logsDir ("{0}_consensus_watchlist_latest.json" -f $profile)
  $supervisorPath = Join-Path $logsDir ("bot_supervisor.{0}.observe.json" -f $profile)
  $planPath = Join-Path $logsDir ("{0}_execution_plan_latest.json" -f $profile)
  $strictLatest = Join-Path $logsDir ("{0}_top30_readiness_strict_latest.json" -f $profile)
  $qualityLatest = Join-Path $logsDir ("{0}_top30_readiness_quality_latest.json" -f $profile)

  if (-not (Test-Path $consensusPath)) {
    throw "consensus json not found for profile=$profile path=$consensusPath"
  }

  Log "judge strict profile=$profile"
  Run-Python @(
    $judgeTool,
    "--consensus-json", $consensusPath,
    "--supervisor-config", $supervisorPath,
    "--execution-plan-file", $planPath,
    "--out-latest-json", $strictLatest,
    "--pretty"
  )

  if (-not (Test-Path $strictLatest)) {
    throw "strict readiness output missing for profile=$profile"
  }
  $strictObj = Get-Content -Path $strictLatest -Raw | ConvertFrom-Json
  $strictDecision = ([string]$strictObj.decision).Trim().ToUpperInvariant()
  $strictReason = [string]$strictObj.decision_reason
  $strictDecisions[$profile] = $strictDecision
  Log ("strict result profile={0} decision={1} reason={2}" -f $profile, $strictDecision, $strictReason)

  Log "judge quality profile=$profile"
  Run-Python @(
    $judgeTool,
    "--consensus-json", $consensusPath,
    "--supervisor-config", $supervisorPath,
    "--execution-plan-file", $planPath,
    "--no-require-execution-plan",
    "--out-latest-json", $qualityLatest,
    "--pretty"
  )
}

Run-Python @(
  $reportTool,
  "--glob", "logs/*_top30_readiness_*latest.json",
  "--out-json", "logs/weather_top30_readiness_report_latest.json",
  "--out-txt", "logs/weather_top30_readiness_report_latest.txt",
  "--pretty"
)
Run-Python @(
  $reportTool,
  "--glob", "logs/*_top30_readiness_*latest.json",
  "--mode", "strict",
  "--out-json", "logs/weather_top30_readiness_report_strict_latest.json",
  "--out-txt", "logs/weather_top30_readiness_report_strict_latest.txt",
  "--pretty"
)

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

$strictTxt = Join-Path $logsDir "weather_top30_readiness_report_strict_latest.txt"
if (Test-Path $strictTxt) {
  Log "strict summary:"
  $txt = Get-Content -Path $strictTxt -Raw
  foreach ($line in ($txt -split "`r?`n")) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    Log $line
  }
}

if ($discordRequested) {
  $reportTxt = Join-Path $logsDir "weather_top30_readiness_report_latest.txt"
  Send-DiscordSummary -SummaryTxtPath $reportTxt
}

if ($FailOnNoGo.IsPresent) {
  $failed = @()
  foreach ($profile in $profileList) {
    if ($strictDecisions.ContainsKey($profile)) {
      if ($strictDecisions[$profile] -ne "GO") {
        $failed += $profile
      }
    } else {
      $failed += $profile
    }
  }
  if ($failed.Count -gt 0) {
    throw "strict readiness failed for profile(s): $($failed -join ',')"
  }
  Log "strict readiness guard pass: all profiles GO"
}

Log "done"

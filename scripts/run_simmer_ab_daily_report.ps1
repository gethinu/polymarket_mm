param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "C:\Users\stair\AppData\Local\Programs\Python\Python311\python.exe",
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

$reportScript = Join-Path $RepoRoot "scripts\report_simmer_observation.py"
$compareScript = Join-Path $RepoRoot "scripts\compare_simmer_ab_daily.py"
$logFile = Join-Path $RepoRoot "logs\simmer-ab-daily-report.log"
$compareLatestFile = Join-Path $RepoRoot "logs\simmer-ab-daily-compare-latest.txt"
$compareHistoryFile = Join-Path $RepoRoot "logs\simmer-ab-daily-compare-history.jsonl"

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

$since = (Get-Date).Date.AddDays(-1).ToString("yyyy-MM-dd HH:mm:ss")
$until = (Get-Date).Date.ToString("yyyy-MM-dd HH:mm:ss")
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")

"[$stamp] start since=$since until=$until" | Out-File -FilePath $logFile -Append -Encoding utf8

& $PythonExe $reportScript `
  --metrics-file (Join-Path $RepoRoot "logs\simmer-ab-baseline-metrics.jsonl") `
  --log-file (Join-Path $RepoRoot "logs\simmer-ab-baseline.log") `
  --state-file (Join-Path $RepoRoot "logs\simmer_ab_baseline_state.json") `
  --since $since --until $until 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

& $PythonExe $reportScript `
  --metrics-file (Join-Path $RepoRoot "logs\simmer-ab-candidate-metrics.jsonl") `
  --log-file (Join-Path $RepoRoot "logs\simmer-ab-candidate.log") `
  --state-file (Join-Path $RepoRoot "logs\simmer_ab_candidate_state.json") `
  --since $since --until $until 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

$discordRequested = Parse-Bool (Get-EnvAny "SIMMER_AB_DAILY_COMPARE_DISCORD")
$webhookUrl = Get-EnvAny "CLOBBOT_DISCORD_WEBHOOK_URL"
if ([string]::IsNullOrWhiteSpace($webhookUrl)) {
  $webhookUrl = Get-EnvAny "DISCORD_WEBHOOK_URL"
}
$discordEnabled = $discordRequested -and (-not [string]::IsNullOrWhiteSpace($webhookUrl))
$discordNote = if ($discordEnabled) { "on" } elseif ($discordRequested) { "requested_but_webhook_missing" } else { "off" }

"[$((Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))] compare discord=$discordNote" | Out-File -FilePath $logFile -Append -Encoding utf8

$compareArgs = @(
  $compareScript,
  "--since", $since,
  "--until", $until,
  "--output-file", $compareLatestFile,
  "--history-file", $compareHistoryFile
)
if ($discordEnabled) {
  $compareArgs += "--discord"
}

& $PythonExe @compareArgs 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

"[$((Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))] done" | Out-File -FilePath $logFile -Append -Encoding utf8

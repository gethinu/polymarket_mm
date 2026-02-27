param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "C:\Users\stair\AppData\Local\Programs\Python\Python311\python.exe",
  [int]$JudgeMinDays = 25,
  [double]$JudgeExpectancyRatioThreshold = 0.9,
  [string]$JudgeDecisionDate = "2026-03-22",
  [double]$JudgeMinWindowHours = 20.0,
  [switch]$FailOnFinalNoGo,
  [switch]$SkipJudge,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"
$script:RunLockHeld = $false
$script:RunLockPath = ""

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

function Get-LockOwnerPid([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path $Path)) {
    return 0
  }
  try {
    $raw = Get-Content -Path $Path -Raw -ErrorAction Stop
  } catch {
    return 0
  }
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return 0
  }
  try {
    $obj = $raw | ConvertFrom-Json -ErrorAction Stop
    if ($null -ne $obj -and $obj.PSObject.Properties["pid"] -and $null -ne $obj.pid) {
      return [int]$obj.pid
    }
  } catch {
  }
  try {
    return [int]$raw.Trim()
  } catch {
    return 0
  }
}

function Test-PidRunning([int]$ProcessId) {
  if ($ProcessId -le 0) {
    return $false
  }
  try {
    $p = Get-Process -Id $ProcessId -ErrorAction Stop
    return $null -ne $p
  } catch {
    return $false
  }
}

function Release-RunLock {
  if (-not $script:RunLockHeld) {
    return
  }
  try {
    $ownerPid = Get-LockOwnerPid -Path $script:RunLockPath
    if ($ownerPid -gt 0 -and $ownerPid -ne $PID) {
      return
    }
    if (-not [string]::IsNullOrWhiteSpace($script:RunLockPath) -and (Test-Path $script:RunLockPath)) {
      Remove-Item -Path $script:RunLockPath -Force -ErrorAction SilentlyContinue
    }
  } catch {
  }
  $script:RunLockHeld = $false
}

function Acquire-RunLock([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) {
    return $false
  }
  for ($attempt = 0; $attempt -lt 2; $attempt++) {
    try {
      $fs = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
      )
      try {
        $payload = [ordered]@{
          pid         = [int]$PID
          acquired_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        } | ConvertTo-Json -Depth 4
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload + "`n")
        $fs.Write($bytes, 0, $bytes.Length)
      } finally {
        $fs.Dispose()
      }
      $script:RunLockPath = $Path
      $script:RunLockHeld = $true
      return $true
    } catch [System.IO.IOException] {
      $ownerPid = Get-LockOwnerPid -Path $Path
      if ($ownerPid -gt 0 -and $ownerPid -ne $PID -and (Test-PidRunning -ProcessId $ownerPid)) {
        return $false
      }
      try {
        if (Test-Path $Path) {
          Remove-Item -Path $Path -Force -ErrorAction Stop
        }
      } catch {
        return $false
      }
    } catch {
      return $false
    }
  }
  return $false
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$reportScript = Join-Path $RepoRoot "scripts\report_simmer_observation.py"
$compareScript = Join-Path $RepoRoot "scripts\compare_simmer_ab_daily.py"
$judgeScript = Join-Path $RepoRoot "scripts\judge_simmer_ab_decision.py"
$logFile = Join-Path $RepoRoot "logs\simmer-ab-daily-report.log"
$compareLatestFile = Join-Path $RepoRoot "logs\simmer-ab-daily-compare-latest.txt"
$compareHistoryFile = Join-Path $RepoRoot "logs\simmer-ab-daily-compare-history.jsonl"
$judgeLatestFile = Join-Path $RepoRoot "logs\simmer-ab-decision-latest.txt"
$judgeLatestJson = Join-Path $RepoRoot "logs\simmer-ab-decision-latest.json"
$runLock = Join-Path $RepoRoot "logs\simmer-ab-daily-report.lock"

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

if (-not (Acquire-RunLock -Path $runLock)) {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  Write-Host ("[{0}] skip: another simmer A/B daily run is active lock={1}" -f $ts, $runLock)
  exit 4
}

try {
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

  if (-not $SkipJudge) {
    "[$((Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))] judge min_days=$JudgeMinDays min_window_hours=$JudgeMinWindowHours exp_ratio=$JudgeExpectancyRatioThreshold decision_date=$JudgeDecisionDate fail_on_final_no_go=$($FailOnFinalNoGo.IsPresent)" | Out-File -FilePath $logFile -Append -Encoding utf8
    $judgeArgs = @(
      $judgeScript,
      "--history-file", $compareHistoryFile,
      "--min-days", ([string]$JudgeMinDays),
      "--min-window-hours", ([string]$JudgeMinWindowHours),
      "--expectancy-ratio-threshold", ([string]$JudgeExpectancyRatioThreshold),
      "--decision-date", ([string]$JudgeDecisionDate),
      "--output-file", $judgeLatestFile,
      "--output-json", $judgeLatestJson
    )
    if ($FailOnFinalNoGo.IsPresent) {
      $judgeArgs += "--fail-on-final-no-go"
    }
    & $PythonExe @judgeArgs 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
      throw "judge_simmer_ab_decision.py failed with exit code $LASTEXITCODE"
    }
  }

  "[$((Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))] done" | Out-File -FilePath $logFile -Append -Encoding utf8
} finally {
  Release-RunLock
}

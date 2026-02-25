[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$StrategyId = "weather_clob_arb_buckets_observe",
  [int]$MinRealizedDays = 30,
  [string]$SnapshotJson = "logs/strategy_register_latest.json",
  [string]$HealthJson = "logs/automation_health_latest.json",
  [string]$GateAlarmStateJson = "logs/strategy_gate_alarm_state.json",
  [string]$GateAlarmLogFile = "logs/strategy_gate_alarm.log",
  [switch]$NoRefresh,
  [switch]$SkipHealth,
  [switch]$SkipGateAlarm,
  [switch]$SkipProcessScan,
  [switch]$DiscordGateAlarm,
  [switch]$FailOnGateNotReady,
  [switch]$FailOnStageNotFinal,
  [switch]$FailOnHealthNoGo,
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

if (-not (Test-Path $PythonExe)) {
  if ($PythonExe -notmatch "^[A-Za-z]:\\") {
    $resolved = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
      throw "Python executable not found: $PythonExe"
    }
    $PythonExe = $resolved.Source
  }
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}
$runLog = Join-Path $logsDir "morning_status_daily_run.log"

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
}

$tool = Join-Path $RepoRoot "scripts\check_morning_status.py"
if (-not (Test-Path $tool)) {
  throw "tool not found: $tool"
}

$args = @(
  $tool,
  "--strategy-id", $StrategyId,
  "--min-realized-days", "$MinRealizedDays",
  "--snapshot-json", $SnapshotJson,
  "--health-json", $HealthJson,
  "--gate-alarm-state-json", $GateAlarmStateJson,
  "--gate-alarm-log-file", $GateAlarmLogFile
)

if ($NoRefresh.IsPresent) { $args += "--no-refresh" }
if ($SkipHealth.IsPresent) { $args += "--skip-health" }
if ($SkipGateAlarm.IsPresent) { $args += "--skip-gate-alarm" }
if ($SkipProcessScan.IsPresent) { $args += "--skip-process-scan" }
if ($DiscordGateAlarm.IsPresent) { $args += "--discord-gate-alarm" }
if ($FailOnGateNotReady.IsPresent) { $args += "--fail-on-gate-not-ready" }
if ($FailOnStageNotFinal.IsPresent) { $args += "--fail-on-stage-not-final" }
if ($FailOnHealthNoGo.IsPresent) { $args += "--fail-on-health-no-go" }

Log ("start strategy_id={0} min_days={1} no_refresh={2} skip_health={3} skip_gate_alarm={4} skip_process_scan={5}" -f `
  $StrategyId, $MinRealizedDays, $NoRefresh.IsPresent, $SkipHealth.IsPresent, $SkipGateAlarm.IsPresent, $SkipProcessScan.IsPresent)

$prevPyUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
$prevPyIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
[Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")

$output = & $PythonExe @args 2>&1
$exitCode = $LASTEXITCODE
foreach ($line in @($output)) {
  $s = [string]$line
  if ([string]::IsNullOrWhiteSpace($s)) { continue }
  Log $s
}

[Environment]::SetEnvironmentVariable("PYTHONUTF8", $prevPyUtf8, "Process")
[Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $prevPyIoEncoding, "Process")

if ($exitCode -ne 0) {
  throw "morning status failed with exit code $exitCode"
}

Log "done"

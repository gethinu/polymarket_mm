[CmdletBinding()]
param(
  [string]$TaskName = "MorningStrategyStatusDaily",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "08:05",
  [string]$PowerShellExe = "powershell.exe",
  [string]$StrategyId = "weather_clob_arb_buckets_observe",
  [int]$MinRealizedDays = 30,
  [string]$SnapshotJson = "logs/strategy_register_latest.json",
  [string]$HealthJson = "logs/automation_health_latest.json",
  [string]$UncorrelatedJson = "logs/uncorrelated_portfolio_proxy_analysis_latest.json",
  [string]$UncorrelatedStrategyIds = "weather_clob_arb_buckets_observe,no_longshot_daily_observe,link_intake_walletseed_cohort_observe,hourly_updown_highprob_calibration_observe",
  [double]$UncorrelatedCorrThresholdAbs = 0.30,
  [int]$UncorrelatedMinOverlapDays = 2,
  [int]$UncorrelatedMinRealizedDaysForCorrelation = 7,
  [string]$GateAlarmStateJson = "logs/strategy_gate_alarm_state.json",
  [string]$GateAlarmLogFile = "logs/strategy_gate_alarm.log",
  [string]$NoLongshotPracticalDecisionDate = "2026-03-02",
  [int]$NoLongshotPracticalSlideDays = 3,
  [int]$NoLongshotPracticalMinResolvedTrades = 30,
  [string]$SimmerAbDecisionJson = "logs/simmer-ab-decision-latest.json",
  [ValidateSet("7d", "14d")]
  [string]$SimmerAbInterimTarget = "7d",
  [double]$SimmerAbMaxStaleHours = 30.0,
  [string]$DiscordWebhookEnv = "CLOBBOT_DISCORD_WEBHOOK_URL_CHECK_MORNING_STATUS",
  [Alias("h")]
  [switch]$Help,
  [switch]$NoRefresh,
  [switch]$SkipHealth,
  [switch]$SkipGateAlarm,
  [switch]$SkipUncorrelatedPortfolio,
  [switch]$SkipImplementationLedger,
  [switch]$SkipSimmerAb,
  [switch]$SkipProcessScan,
  [switch]$DiscordGateAlarm,
  [switch]$FailOnGateNotReady,
  [switch]$FailOnStageNotFinal,
  [switch]$FailOnHealthNoGo,
  [switch]$FailOnSimmerAbFinalNoGo,
  [switch]$FailOnSimmerAbInterimNoGo,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_morning_status_daily_task.ps1 -NoBackground [-TaskName MorningStrategyStatusDaily] [-StartTime 08:05] [-StrategyId weather_clob_arb_buckets_observe] [-FailOnStageNotFinal] [-FailOnSimmerAbFinalNoGo] [-SimmerAbInterimTarget 7d|14d] [-SimmerAbMaxStaleHours 30] [-NoLongshotPracticalDecisionDate 2026-03-02] [-NoLongshotPracticalSlideDays 3] [-NoLongshotPracticalMinResolvedTrades 30] [-SkipImplementationLedger] [-SkipUncorrelatedPortfolio] [-UncorrelatedStrategyIds weather_clob_arb_buckets_observe,no_longshot_daily_observe,link_intake_walletseed_cohort_observe,hourly_updown_highprob_calibration_observe] [-UncorrelatedCorrThresholdAbs 0.30] [-DiscordWebhookEnv CLOBBOT_DISCORD_WEBHOOK_URL_CHECK_MORNING_STATUS] [-RunNow]"
  Write-Host "  note: installer always enforces -FailOnSimmerAbInterimNoGo on task action/run-now."
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_morning_status_daily_task.ps1 -NoBackground -?"
}

$invLine = [string]$MyInvocation.Line
if ($Help.IsPresent -or ($invLine -match '(^|\s)(-\?|/\?|--help|-h)(\s|$)')) {
  Show-Usage
  exit 0
}

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

if (-not (Test-Path $PowerShellExe)) {
  if ($PowerShellExe -notmatch "^[A-Za-z]:\\") {
    $resolved = Get-Command $PowerShellExe -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
      throw "PowerShell executable not found: $PowerShellExe"
    }
    $PowerShellExe = $resolved.Source
  }
}

$runnerPath = Join-Path $RepoRoot "scripts\run_morning_status_daily.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}

$parsed = $null
try {
  $parsed = [datetime]::ParseExact($StartTime, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture)
} catch {
  try {
    $parsed = [datetime]::ParseExact($StartTime, "H:mm", [System.Globalization.CultureInfo]::InvariantCulture)
  } catch {
    throw "Invalid StartTime format: $StartTime (expected HH:mm)"
  }
}

$now = Get-Date
$at = Get-Date -Hour $parsed.Hour -Minute $parsed.Minute -Second 0
if ($at -le $now.AddMinutes(1)) {
  $at = $at.AddDays(1)
}

$uncorrelatedStrategyIds = [string]$UncorrelatedStrategyIds
if ($null -eq $uncorrelatedStrategyIds) {
  $uncorrelatedStrategyIds = ""
}
$uncorrelatedStrategyIds = $uncorrelatedStrategyIds.Trim()

$argList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-StrategyId", $StrategyId,
  "-MinRealizedDays", "$MinRealizedDays",
  "-SnapshotJson", $SnapshotJson,
  "-HealthJson", $HealthJson,
  "-UncorrelatedJson", $UncorrelatedJson,
  "-UncorrelatedCorrThresholdAbs", "$UncorrelatedCorrThresholdAbs",
  "-UncorrelatedMinOverlapDays", "$UncorrelatedMinOverlapDays",
  "-UncorrelatedMinRealizedDaysForCorrelation", "$UncorrelatedMinRealizedDaysForCorrelation",
  "-GateAlarmStateJson", $GateAlarmStateJson,
  "-GateAlarmLogFile", $GateAlarmLogFile,
  "-NoLongshotPracticalDecisionDate", $NoLongshotPracticalDecisionDate,
  "-NoLongshotPracticalSlideDays", "$NoLongshotPracticalSlideDays",
  "-NoLongshotPracticalMinResolvedTrades", "$NoLongshotPracticalMinResolvedTrades",
  "-SimmerAbDecisionJson", $SimmerAbDecisionJson,
  "-SimmerAbInterimTarget", $SimmerAbInterimTarget,
  "-SimmerAbMaxStaleHours", "$SimmerAbMaxStaleHours"
)
# Enforce interim fail gate by default to avoid task-argument drift.
$argList += "-FailOnSimmerAbInterimNoGo"
if (-not [string]::IsNullOrWhiteSpace($uncorrelatedStrategyIds)) {
  $argList += "-UncorrelatedStrategyIds"
  $argList += $uncorrelatedStrategyIds
}
if ($NoRefresh.IsPresent) { $argList += "-NoRefresh" }
if ($SkipHealth.IsPresent) { $argList += "-SkipHealth" }
if ($SkipGateAlarm.IsPresent) { $argList += "-SkipGateAlarm" }
if ($SkipUncorrelatedPortfolio.IsPresent) { $argList += "-SkipUncorrelatedPortfolio" }
if ($SkipImplementationLedger.IsPresent) { $argList += "-SkipImplementationLedger" }
if ($SkipSimmerAb.IsPresent) { $argList += "-SkipSimmerAb" }
if ($SkipProcessScan.IsPresent) { $argList += "-SkipProcessScan" }
if ($DiscordGateAlarm.IsPresent) {
  $argList += "-DiscordGateAlarm"
  if (-not [string]::IsNullOrWhiteSpace($DiscordWebhookEnv)) {
    $argList += "-DiscordWebhookEnv"
    $argList += $DiscordWebhookEnv
  }
}
if ($FailOnGateNotReady.IsPresent) { $argList += "-FailOnGateNotReady" }
if ($FailOnStageNotFinal.IsPresent) { $argList += "-FailOnStageNotFinal" }
if ($FailOnHealthNoGo.IsPresent) { $argList += "-FailOnHealthNoGo" }
if ($FailOnSimmerAbFinalNoGo.IsPresent) { $argList += "-FailOnSimmerAbFinalNoGo" }
$actionArgs = ($argList -join " ")

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet `
  -Hidden `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries
$desc = "Run morning strategy status check (observe-only)"

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($currentUser)) {
  if (-not [string]::IsNullOrWhiteSpace($env:USERDOMAIN) -and -not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
    $currentUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
  } else {
    $currentUser = $env:USERNAME
  }
}

$registered = $false
$principalMode = "default"
if (-not [string]::IsNullOrWhiteSpace($currentUser)) {
  foreach ($logonType in @("S4U", "Interactive")) {
    try {
      $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $logonType -RunLevel Limited
      Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
      $registered = $true
      $principalMode = "${logonType}:$currentUser"
      break
    } catch {
    }
  }
}
if (-not $registered) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
}
Write-Host ("Registered principal mode: {0}" -f $principalMode)
try {
  Enable-ScheduledTask -TaskName $TaskName | Out-Null
} catch {
}

if ($RunNow.IsPresent) {
  $runArgs = @(
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", $runnerPath,
    "-NoBackground",
    "-StrategyId", $StrategyId,
    "-MinRealizedDays", "$MinRealizedDays",
    "-SnapshotJson", $SnapshotJson,
    "-HealthJson", $HealthJson,
    "-UncorrelatedJson", $UncorrelatedJson,
    "-UncorrelatedCorrThresholdAbs", "$UncorrelatedCorrThresholdAbs",
    "-UncorrelatedMinOverlapDays", "$UncorrelatedMinOverlapDays",
    "-UncorrelatedMinRealizedDaysForCorrelation", "$UncorrelatedMinRealizedDaysForCorrelation",
    "-GateAlarmStateJson", $GateAlarmStateJson,
    "-GateAlarmLogFile", $GateAlarmLogFile,
    "-NoLongshotPracticalDecisionDate", $NoLongshotPracticalDecisionDate,
    "-NoLongshotPracticalSlideDays", "$NoLongshotPracticalSlideDays",
    "-NoLongshotPracticalMinResolvedTrades", "$NoLongshotPracticalMinResolvedTrades",
    "-SimmerAbDecisionJson", $SimmerAbDecisionJson,
    "-SimmerAbInterimTarget", $SimmerAbInterimTarget,
    "-SimmerAbMaxStaleHours", "$SimmerAbMaxStaleHours"
  )
  # Keep run-now behavior aligned with scheduled task action defaults.
  $runArgs += "-FailOnSimmerAbInterimNoGo"
  if (-not [string]::IsNullOrWhiteSpace($uncorrelatedStrategyIds)) {
    $runArgs += "-UncorrelatedStrategyIds"
    $runArgs += $uncorrelatedStrategyIds
  }
  if ($NoRefresh.IsPresent) { $runArgs += "-NoRefresh" }
  if ($SkipHealth.IsPresent) { $runArgs += "-SkipHealth" }
  if ($SkipGateAlarm.IsPresent) { $runArgs += "-SkipGateAlarm" }
  if ($SkipUncorrelatedPortfolio.IsPresent) { $runArgs += "-SkipUncorrelatedPortfolio" }
  if ($SkipImplementationLedger.IsPresent) { $runArgs += "-SkipImplementationLedger" }
  if ($SkipSimmerAb.IsPresent) { $runArgs += "-SkipSimmerAb" }
  if ($SkipProcessScan.IsPresent) { $runArgs += "-SkipProcessScan" }
  if ($DiscordGateAlarm.IsPresent) {
    $runArgs += "-DiscordGateAlarm"
    if (-not [string]::IsNullOrWhiteSpace($DiscordWebhookEnv)) {
      $runArgs += "-DiscordWebhookEnv"
      $runArgs += $DiscordWebhookEnv
    }
  }
  if ($FailOnGateNotReady.IsPresent) { $runArgs += "-FailOnGateNotReady" }
  if ($FailOnStageNotFinal.IsPresent) { $runArgs += "-FailOnStageNotFinal" }
  if ($FailOnHealthNoGo.IsPresent) { $runArgs += "-FailOnHealthNoGo" }
  if ($FailOnSimmerAbFinalNoGo.IsPresent) { $runArgs += "-FailOnSimmerAbFinalNoGo" }

  Write-Host "RunNow: executing runner directly (observe-only) ..."
  & $PowerShellExe @runArgs
  if ($LASTEXITCODE -ne 0) {
    throw "RunNow direct runner failed with exit code $LASTEXITCODE"
  }
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult,NextRunTime

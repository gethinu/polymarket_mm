[CmdletBinding()]
param(
  [string]$TaskName = "WeatherArbProfitWindowWeekly",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "01:00",
  [string]$PowerShellExe = "powershell.exe",
  [string]$PythonExe = "python",
  [int]$ObserveRunSeconds = 3600,
  [double]$ObserveMinEdgeCents = 1.0,
  [double]$ObserveShares = 5.0,
  [ValidateSet("buckets", "yes-no", "both")]
  [string]$ObserveStrategy = "buckets",
  [double]$ReportHours = 24.0,
  [double]$AssumedBankrollUsd = [double]::NaN,
  [double]$TargetMonthlyReturnPct = 15.0,
  [Alias("h")]
  [switch]$Help,
  [switch]$SkipObserve,
  [switch]$FailOnNoGo,
  [switch]$Discord,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_profit_window_weekly_task.ps1 -NoBackground [-TaskName WeatherArbProfitWindowWeekly] [-StartTime 01:00] [-AssumedBankrollUsd 100] [-TargetMonthlyReturnPct 15] [-Discord] [-RunNow]"
  Write-Host "  (omit -AssumedBankrollUsd to use STRATEGY bankroll policy default)"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_profit_window_weekly_task.ps1 -NoBackground -?"
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

$runnerPath = Join-Path $RepoRoot "scripts\run_weather_arb_profit_window.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}

$parsed = $null
try {
  $parsed = [datetime]::ParseExact($StartTime, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture)
}
catch {
  try {
    $parsed = [datetime]::ParseExact($StartTime, "H:mm", [System.Globalization.CultureInfo]::InvariantCulture)
  }
  catch {
    throw "Invalid StartTime format: $StartTime (expected HH:mm)"
  }
}

$now = Get-Date
$firstRun = Get-Date -Hour $parsed.Hour -Minute $parsed.Minute -Second 0
$daysUntilMonday = (([int][DayOfWeek]::Monday - [int]$firstRun.DayOfWeek + 7) % 7)
$firstRun = $firstRun.AddDays($daysUntilMonday)
if ($firstRun -le $now.AddMinutes(1)) {
  $firstRun = $firstRun.AddDays(7)
}

$argList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-RepoRoot", $RepoRoot,
  "-PythonExe", $PythonExe,
  "-ObserveRunSeconds", "$ObserveRunSeconds",
  "-ObserveMinEdgeCents", "$ObserveMinEdgeCents",
  "-ObserveShares", "$ObserveShares",
  "-ObserveStrategy", $ObserveStrategy,
  "-ReportHours", "$ReportHours",
  "-TargetMonthlyReturnPct", "$TargetMonthlyReturnPct"
)
if (-not [double]::IsNaN($AssumedBankrollUsd)) { $argList += @("-AssumedBankrollUsd", "$AssumedBankrollUsd") }
if ($SkipObserve.IsPresent) { $argList += "-SkipObserve" }
if ($FailOnNoGo.IsPresent) { $argList += "-FailOnNoGo" }
if ($Discord.IsPresent) { $argList += "-Discord" }
$actionArgs = ($argList -join " ")

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday -At $firstRun
$settings = New-ScheduledTaskSettingsSet `
  -Hidden `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries
$desc = "Run weather arb profit-window weekly report (observe-only)"

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
    "-RepoRoot", $RepoRoot,
    "-PythonExe", $PythonExe,
    "-ObserveRunSeconds", "$ObserveRunSeconds",
    "-ObserveMinEdgeCents", "$ObserveMinEdgeCents",
    "-ObserveShares", "$ObserveShares",
    "-ObserveStrategy", $ObserveStrategy,
    "-ReportHours", "$ReportHours",
    "-TargetMonthlyReturnPct", "$TargetMonthlyReturnPct"
  )
  if (-not [double]::IsNaN($AssumedBankrollUsd)) { $runArgs += @("-AssumedBankrollUsd", "$AssumedBankrollUsd") }
  if ($SkipObserve.IsPresent) { $runArgs += "-SkipObserve" }
  if ($FailOnNoGo.IsPresent) { $runArgs += "-FailOnNoGo" }
  if ($Discord.IsPresent) { $runArgs += "-Discord" }

  Write-Host "RunNow: executing runner directly (observe-only) ..."
  & $PowerShellExe @runArgs
  if ($LASTEXITCODE -ne 0) {
    throw "RunNow direct runner failed with exit code $LASTEXITCODE"
  }
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

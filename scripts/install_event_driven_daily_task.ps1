[CmdletBinding()]
param(
  [string]$TaskName = "EventDrivenDailyReport",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "00:15",
  [string]$PowerShellExe = "powershell.exe",
  [int]$MaxPages = 12,
  [double]$MinLiquidity = 5000,
  [double]$MinVolume24h = 250,
  [double]$MinEdgeCents = 0.8,
  [int]$TopN = 20,
  [double]$ReportHours = 24,
  [string]$IncludeRegex = "",
  [string]$ExcludeRegex = "",
  [switch]$IncludeNonEvent,
  [string]$ProfitThresholdsCents = "0.8,1,2,3,5",
  [string]$ProfitCaptureRatios = "0.25,0.35,0.50",
  [double]$ProfitTargetMonthlyReturnPct = 12,
  [double]$ProfitAssumedBankrollUsd = [double]::NaN,
  [double]$ProfitMaxEvMultipleOfStake = 0.35,
  [switch]$SkipProfitWindow,
  [switch]$FailOnNoGo,
  [ValidateSet("auto", "default", "s4u", "interactive")]
  [string]$PrincipalMode = "auto",
  [ValidateSet("cmd", "powershell")]
  [string]$ActionMode = "powershell",
  [switch]$Discord,
  [switch]$RunNow,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1 -NoBackground [-TaskName EventDrivenDailyReport] [-StartTime 00:15] [-PrincipalMode auto|default|s4u|interactive] [-ActionMode cmd|powershell] [-FailOnNoGo] [-RunNow]"
  Write-Host "  (omit -ProfitAssumedBankrollUsd to use STRATEGY bankroll policy default)"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1 -NoBackground -?"
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

$runnerPath = Join-Path $RepoRoot "scripts\run_event_driven_daily_report.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}
$wrapperCmdPath = Join-Path $RepoRoot "scripts\run_event_driven_daily_task_wrapper.cmd"
$logDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
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
$at = Get-Date -Hour $parsed.Hour -Minute $parsed.Minute -Second 0
if ($at -le $now.AddMinutes(1)) {
  $at = $at.AddDays(1)
}

function Quote-CmdToken([string]$token) {
  if ($null -eq $token) { return '""' }
  $t = [string]$token
  if ($t -match '[\s&|<>^()]') {
    return '"' + ($t.Replace('"', '""')) + '"'
  }
  return $t
}

$runnerArgList = @(
  "-NoBackground",
  "-MaxPages", "$MaxPages",
  "-MinLiquidity", "$MinLiquidity",
  "-MinVolume24h", "$MinVolume24h",
  "-MinEdgeCents", "$MinEdgeCents",
  "-TopN", "$TopN",
  "-ReportHours", "$ReportHours",
  "-ProfitThresholdsCents", "$ProfitThresholdsCents",
  "-ProfitCaptureRatios", "$ProfitCaptureRatios",
  "-ProfitTargetMonthlyReturnPct", "$ProfitTargetMonthlyReturnPct",
  "-ProfitMaxEvMultipleOfStake", "$ProfitMaxEvMultipleOfStake"
)
if (-not [double]::IsNaN($ProfitAssumedBankrollUsd)) {
  $runnerArgList += @("-ProfitAssumedBankrollUsd", "$ProfitAssumedBankrollUsd")
}
if (-not [string]::IsNullOrWhiteSpace($IncludeRegex)) {
  $runnerArgList += @("-IncludeRegex", $IncludeRegex)
}
if (-not [string]::IsNullOrWhiteSpace($ExcludeRegex)) {
  $runnerArgList += @("-ExcludeRegex", $ExcludeRegex)
}
if ($IncludeNonEvent.IsPresent) {
  $runnerArgList += "-IncludeNonEvent"
}
if ($SkipProfitWindow.IsPresent) {
  $runnerArgList += "-SkipProfitWindow"
}
if ($FailOnNoGo.IsPresent) {
  $runnerArgList += "-FailOnNoGo"
}
if ($Discord.IsPresent) {
  $runnerArgList += "-Discord"
}

$psArgList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runnerPath)
)
$psArgList += $runnerArgList
$psActionArgs = ($psArgList -join " ")

if ($ActionMode -eq "powershell") {
  $action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $psActionArgs -WorkingDirectory $RepoRoot
} else {
  if (-not (Test-Path $wrapperCmdPath)) {
    throw "Wrapper not found: $wrapperCmdPath"
  }
  $wrapperArgs = (@($runnerArgList | ForEach-Object { Quote-CmdToken $_ }) -join " ")
  $cmdArgs = "/c $wrapperCmdPath $wrapperArgs"
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $RepoRoot
}
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$desc = "Run event-driven observe + daily report"

# Recreate task cleanly so principal/logon changes are applied deterministically.
try {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($currentUser)) {
  if (-not [string]::IsNullOrWhiteSpace($env:USERDOMAIN) -and -not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
    $currentUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
  } else {
    $currentUser = $env:USERNAME
  }
}

$registered = $false
$principalModeUsed = "default"
$mode = ($PrincipalMode | ForEach-Object { [string]$_ }).Trim().ToLowerInvariant()
if ($mode -eq "default") {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
  $registered = $true
  $principalModeUsed = "default"
} elseif ($mode -in @("s4u", "interactive")) {
  if ([string]::IsNullOrWhiteSpace($currentUser)) {
    throw "Cannot use PrincipalMode=$mode because current user resolution failed."
  }
  $logonType = if ($mode -eq "s4u") { "S4U" } else { "Interactive" }
  $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $logonType -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
  $registered = $true
  $principalModeUsed = "${logonType}:$currentUser"
} else {
  if (-not [string]::IsNullOrWhiteSpace($currentUser)) {
    foreach ($logonType in @("S4U", "Interactive")) {
      try {
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $logonType -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
        $registered = $true
        $principalModeUsed = "${logonType}:$currentUser"
        break
      } catch {
      }
    }
  }
  if (-not $registered) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
    $registered = $true
    $principalModeUsed = "default"
  }
}

Write-Host ("Registered principal mode: {0}" -f $principalModeUsed)

if ($RunNow.IsPresent) {
  $runArgs = @(
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", $runnerPath
  )
  $runArgs += $runnerArgList
  Write-Host "RunNow: executing runner directly (observe-only) ..."
  & $PowerShellExe @runArgs
  if ($LASTEXITCODE -ne 0) {
    throw "RunNow direct runner failed with exit code $LASTEXITCODE"
  }
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

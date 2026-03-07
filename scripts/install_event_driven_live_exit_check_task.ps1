[CmdletBinding()]
param(
  [string]$TaskName = "EventDrivenLiveExitCheck60m",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [int]$IntervalMinutes = 60,
  [int]$DurationDays = 3650,
  [string]$PowerShellExe = "powershell.exe",
  [string]$LiveConfirm = "YES",
  [double]$WinThreshold = 0.99,
  [double]$LoseThreshold = 0.01,
  [double]$ApiTimeoutSec = 20,
  [ValidateSet("auto", "default", "s4u", "interactive")]
  [string]$PrincipalMode = "auto",
  [Alias("h")]
  [switch]$Help,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_live_exit_check_task.ps1 -NoBackground [-IntervalMinutes 60] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_live_exit_check_task.ps1 -NoBackground -?"
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

if ($IntervalMinutes -lt 1) {
  throw "IntervalMinutes must be >= 1"
}
if ($DurationDays -lt 1) {
  throw "DurationDays must be >= 1"
}
if ([string]$LiveConfirm -ne "YES") {
  throw "LiveConfirm must be YES for the live exit-check task"
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

$runnerPath = Join-Path $RepoRoot "scripts\run_event_driven_live_exit_check.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}

$actionParts = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-RepoRoot", ('"{0}"' -f $RepoRoot),
  "-LiveExecute",
  "-LiveConfirm", $LiveConfirm,
  "-WinThreshold", ([string]$WinThreshold),
  "-LoseThreshold", ([string]$LoseThreshold),
  "-ApiTimeoutSec", ([string]$ApiTimeoutSec)
)
$actionArgs = $actionParts -join " "

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$desc = "Run event-driven live resolution/exit checks periodically (no new entries)"

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($currentUser)) {
  if (-not [string]::IsNullOrWhiteSpace($env:USERDOMAIN) -and -not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
    $currentUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
  } else {
    $currentUser = $env:USERNAME
  }
}

function Try-Register([string]$Mode, [string]$LogonType) {
  if ([string]::IsNullOrWhiteSpace($currentUser)) { return $false }
  try {
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $LogonType -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($triggerRepeat, $triggerLogon) -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
    Write-Host ("Registered principal mode: {0}:{1}" -f $Mode, $currentUser)
    return $true
  } catch {
    return $false
  }
}

$registered = $false
switch ($PrincipalMode) {
  "s4u" { $registered = Try-Register -Mode "S4U" -LogonType "S4U" }
  "interactive" { $registered = Try-Register -Mode "Interactive" -LogonType "Interactive" }
  "auto" {
    $registered = (Try-Register -Mode "S4U" -LogonType "S4U")
    if (-not $registered) {
      $registered = (Try-Register -Mode "Interactive" -LogonType "Interactive")
    }
  }
}
if (-not $registered) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($triggerRepeat, $triggerLogon) -Settings $settings -Description $desc -Force | Out-Null
  Write-Host "Registered principal mode: default"
}

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
    "-LiveExecute",
    "-LiveConfirm", $LiveConfirm,
    "-WinThreshold", ([string]$WinThreshold),
    "-LoseThreshold", ([string]$LoseThreshold),
    "-ApiTimeoutSec", ([string]$ApiTimeoutSec)
  )
  Write-Host "RunNow: executing runner directly (live exit-only, no new entries) ..."
  & $PowerShellExe @runArgs
  if ($LASTEXITCODE -ne 0) {
    throw "RunNow direct runner failed with exit code $LASTEXITCODE"
  }
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

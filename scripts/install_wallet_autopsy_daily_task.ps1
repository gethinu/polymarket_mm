[CmdletBinding()]
param(
  [string]$TaskName = "WalletAutopsyDailyReport",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "00:10",
  [string]$PowerShellExe = "powershell.exe",
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
  [Alias("h")]
  [switch]$Help,
  [switch]$Discord,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_wallet_autopsy_daily_task.ps1 -NoBackground [-TaskName WalletAutopsyDailyReport] [-StartTime 00:10] [-LatestPerMarket] [-TimingProfile endgame_consistent] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_wallet_autopsy_daily_task.ps1 -NoBackground -?"
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

$runnerPath = Join-Path $RepoRoot "scripts\run_wallet_autopsy_daily_report.ps1"
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
$at = Get-Date -Hour $parsed.Hour -Minute $parsed.Minute -Second 0
if ($at -le $now.AddMinutes(1)) {
  $at = $at.AddDays(1)
}

$argList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-Statuses", $Statuses,
  "-MinProfitablePct", "$MinProfitablePct",
  "-MinHedgeEdgePct", "$MinHedgeEdgePct",
  "-MinTrades", "$MinTrades",
  "-TimingProfile", $TimingProfile,
  "-TimingSides", $TimingSides,
  "-TimingMaxTrades", "$TimingMaxTrades",
  "-MinTimingTradeCount", "$MinTimingTradeCount",
  "-Top", "$Top",
  "-BatchTop", "$BatchTop"
)

if ($LatestPerMarket.IsPresent) {
  $argList += "-LatestPerMarket"
}
if ($Discord.IsPresent) {
  $argList += "-Discord"
}

$actionArgs = ($argList -join " ")

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$desc = "Run wallet autopsy daily candidate + timing reports"

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
}
catch {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
}

if ($RunNow.IsPresent) {
  Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

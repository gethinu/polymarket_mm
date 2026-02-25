[CmdletBinding()]
param(
  [string]$TaskName = "WeatherMimicPipelineDaily",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$StartTime = "00:20",
  [string]$PowerShellExe = "powershell.exe",
  [string]$UserFile = "configs\weather_mimic_target_users.txt",
  [string]$ProfileName = "weather_7acct_auto",
  [double]$MinWeatherSharePct = 60.0,
  [int]$MinTrades = 500,
  [double]$MinRealizedPnl = 0.0,
  [switch]$LateprobDisableWeatherFilter,
  [ValidateSet("balanced", "liquidity", "edge")]
  [string]$ConsensusScoreMode = "edge",
  [double]$ConsensusWeightOverlap = [double]::NaN,
  [double]$ConsensusWeightNetYield = [double]::NaN,
  [double]$ConsensusWeightMaxProfit = [double]::NaN,
  [double]$ConsensusWeightLiquidity = [double]::NaN,
  [double]$ConsensusWeightVolume = [double]::NaN,
  [Alias("h")]
  [switch]$Help,
  [switch]$NoRunScans,
  [switch]$Discord,
  [switch]$FailOnReadinessNoGo,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground [-TaskName WeatherMimicPipelineDaily] [-StartTime 00:20] [-ProfileName weather_7acct_auto] [-LateprobDisableWeatherFilter] [-FailOnReadinessNoGo] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground -?"
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

$runnerPath = Join-Path $RepoRoot "scripts\run_weather_mimic_pipeline_daily.ps1"
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

$argList = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-UserFile", $UserFile,
  "-ProfileName", $ProfileName,
  "-MinWeatherSharePct", "$MinWeatherSharePct",
  "-MinTrades", "$MinTrades",
  "-MinRealizedPnl", "$MinRealizedPnl",
  "-ConsensusScoreMode", $ConsensusScoreMode
)

if ($NoRunScans.IsPresent) { $argList += "-NoRunScans" }
if ($LateprobDisableWeatherFilter.IsPresent) { $argList += "-LateprobDisableWeatherFilter" }
if ($Discord.IsPresent) { $argList += "-Discord" }
if ($FailOnReadinessNoGo.IsPresent) { $argList += "-FailOnReadinessNoGo" }
if (-not [double]::IsNaN($ConsensusWeightOverlap)) { $argList += @("-ConsensusWeightOverlap", "$ConsensusWeightOverlap") }
if (-not [double]::IsNaN($ConsensusWeightNetYield)) { $argList += @("-ConsensusWeightNetYield", "$ConsensusWeightNetYield") }
if (-not [double]::IsNaN($ConsensusWeightMaxProfit)) { $argList += @("-ConsensusWeightMaxProfit", "$ConsensusWeightMaxProfit") }
if (-not [double]::IsNaN($ConsensusWeightLiquidity)) { $argList += @("-ConsensusWeightLiquidity", "$ConsensusWeightLiquidity") }
if (-not [double]::IsNaN($ConsensusWeightVolume)) { $argList += @("-ConsensusWeightVolume", "$ConsensusWeightVolume") }

$actionArgs = ($argList -join " ")

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet `
  -Hidden `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$desc = "Run weather mimic pipeline daily (observe-only)"

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
} catch {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
}

if ($RunNow.IsPresent) {
  $runArgs = @(
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", $runnerPath,
    "-NoBackground",
    "-UserFile", $UserFile,
    "-ProfileName", $ProfileName,
    "-MinWeatherSharePct", "$MinWeatherSharePct",
    "-MinTrades", "$MinTrades",
    "-MinRealizedPnl", "$MinRealizedPnl",
    "-ConsensusScoreMode", $ConsensusScoreMode
  )
  if ($NoRunScans.IsPresent) { $runArgs += "-NoRunScans" }
  if ($LateprobDisableWeatherFilter.IsPresent) { $runArgs += "-LateprobDisableWeatherFilter" }
  if ($Discord.IsPresent) { $runArgs += "-Discord" }
  if ($FailOnReadinessNoGo.IsPresent) { $runArgs += "-FailOnReadinessNoGo" }
  if (-not [double]::IsNaN($ConsensusWeightOverlap)) { $runArgs += @("-ConsensusWeightOverlap", "$ConsensusWeightOverlap") }
  if (-not [double]::IsNaN($ConsensusWeightNetYield)) { $runArgs += @("-ConsensusWeightNetYield", "$ConsensusWeightNetYield") }
  if (-not [double]::IsNaN($ConsensusWeightMaxProfit)) { $runArgs += @("-ConsensusWeightMaxProfit", "$ConsensusWeightMaxProfit") }
  if (-not [double]::IsNaN($ConsensusWeightLiquidity)) { $runArgs += @("-ConsensusWeightLiquidity", "$ConsensusWeightLiquidity") }
  if (-not [double]::IsNaN($ConsensusWeightVolume)) { $runArgs += @("-ConsensusWeightVolume", "$ConsensusWeightVolume") }

  Write-Host "RunNow: executing runner directly (observe-only) ..."
  & $PowerShellExe @runArgs
  if ($LASTEXITCODE -ne 0) {
    throw "RunNow direct runner failed with exit code $LASTEXITCODE"
  }
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime

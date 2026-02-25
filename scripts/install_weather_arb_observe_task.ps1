[CmdletBinding()]
param(
  [string]$TaskName = "PolymarketWeatherArbObserve",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [int]$IntervalMinutes = 2,
  [int]$DurationDays = 3650,
  [int]$RunSeconds = 85,
  [double]$MinEdgeCents = 2.0,
  [double]$Shares = 5.0,
  [ValidateSet("buckets", "yes-no", "both")]
  [string]$Strategy = "buckets",
  [double]$SummaryEverySec = 0.0,
  [int]$MaxSubscribeTokens = 400,
  [switch]$RunNow,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_arb_observe_task.ps1 -NoBackground [-TaskName PolymarketWeatherArbObserve] [-RunSeconds 85] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_arb_observe_task.ps1 -NoBackground -?"
}

$invLine = [string]$MyInvocation.Line
if ($invLine -match '(^|\s)(-\?|/\?|--help|-h)(\s|$)') {
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

function To-Inv([object]$Value) {
  return [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0}", $Value)
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$baseDir = (Resolve-Path $RepoRoot).Path
$runnerPath = Join-Path $baseDir "scripts\run_weather_arb_observe.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$actionArgs = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-RunSeconds", (To-Inv $RunSeconds),
  "-MinEdgeCents", (To-Inv $MinEdgeCents),
  "-Shares", (To-Inv $Shares),
  "-Strategy", $Strategy,
  "-SummaryEverySec", (To-Inv $SummaryEverySec),
  "-MaxSubscribeTokens", (To-Inv $MaxSubscribeTokens)
)
$actionArgString = ($actionArgs -join " ")

$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $actionArgString -WorkingDirectory $baseDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$desc = "Run weather-only Polymarket CLOB arb observation monitor periodically"

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

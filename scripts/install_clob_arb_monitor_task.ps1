param(
  [string]$TaskName = "PolymarketClobArbMonitor",
  [int]$IntervalMinutes = 2,
  [int]$DurationDays = 3650,
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

$baseDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $baseDir "scripts\\run_clob_arb_monitor.ps1"
$hiddenLauncherPath = Join-Path $baseDir "scripts\\run_clob_arb_monitor_hidden.vbs"
if (-not (Test-Path $scriptPath)) {
  throw "Script not found: $scriptPath"
}
if (-not (Test-Path $hiddenLauncherPath)) {
  throw "Hidden launcher not found: $hiddenLauncherPath"
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"{0}"' -f $hiddenLauncherPath)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Run Polymarket CLOB arbitrage monitor periodically" -Force | Out-Null
}
catch {
  # Fallback for environments where S4U registration isn't available.
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Run Polymarket CLOB arbitrage monitor periodically" -Force | Out-Null
}

schtasks /Query /TN $TaskName /V /FO LIST

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("daemon", "task", "off")]
  [string]$Mode,
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$ConfigFile = "configs/bot_supervisor.observe.json",
  [string]$DaemonJobName = "weather_daily_daemon",
  [string]$MimicTaskName = "WeatherMimicPipelineDaily",
  [string]$Top30TaskName = "WeatherTop30ReadinessDaily",
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

function Resolve-PathFromRepo([string]$Root, [string]$Raw) {
  if ([string]::IsNullOrWhiteSpace($Raw)) {
    throw "path is empty"
  }
  if ([System.IO.Path]::IsPathRooted($Raw)) {
    return $Raw
  }
  return (Join-Path $Root $Raw)
}

function Write-FileUtf8NoBom([string]$Path, [string]$Content) {
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Set-WeatherDaemonEnabled([string]$ConfigPath, [string]$JobName, [bool]$Enabled) {
  if (-not (Test-Path $ConfigPath)) {
    throw "config not found: $ConfigPath"
  }
  $raw = Get-Content -Path $ConfigPath -Raw -Encoding UTF8
  $cfg = $raw | ConvertFrom-Json
  if ($null -eq $cfg -or $null -eq $cfg.jobs) {
    throw "invalid config (jobs missing): $ConfigPath"
  }
  $target = $null
  foreach ($j in @($cfg.jobs)) {
    if ([string]$j.name -eq $JobName) {
      $target = $j
      break
    }
  }
  if ($null -eq $target) {
    throw "job not found in config: $JobName"
  }
  $current = [bool]$target.enabled
  if ($current -eq [bool]$Enabled) {
    return $false
  }
  $target.enabled = [bool]$Enabled

  $json = (($cfg | ConvertTo-Json -Depth 100).TrimEnd() + "`n")
  Write-FileUtf8NoBom -Path $ConfigPath -Content $json
  return $true
}

function Set-TaskEnabled([string]$TaskName, [bool]$Enabled) {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($null -eq $task) {
    return "MISSING"
  }
  if ($Enabled) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
  } else {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
  }
  $updated = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($null -eq $updated) {
    return "MISSING"
  }
  return [string]$updated.State
}

$configPath = Resolve-PathFromRepo -Root $RepoRoot -Raw $ConfigFile

$daemonEnabled = $false
$tasksEnabled = $false
switch ($Mode) {
  "daemon" {
    $daemonEnabled = $true
    $tasksEnabled = $false
  }
  "task" {
    $daemonEnabled = $false
    $tasksEnabled = $true
  }
  "off" {
    $daemonEnabled = $false
    $tasksEnabled = $false
  }
}

$daemonConfigChanged = Set-WeatherDaemonEnabled -ConfigPath $configPath -JobName $DaemonJobName -Enabled:$daemonEnabled
$mimicState = Set-TaskEnabled -TaskName $MimicTaskName -Enabled:$tasksEnabled
$top30State = Set-TaskEnabled -TaskName $Top30TaskName -Enabled:$tasksEnabled

Write-Host ("mode={0}" -f $Mode)
Write-Host ("config={0}" -f $configPath)
Write-Host ("daemon_job={0} enabled={1}" -f $DaemonJobName, $daemonEnabled)
Write-Host ("config_changed={0}" -f $daemonConfigChanged)
Write-Host ("task={0} state={1}" -f $MimicTaskName, $mimicState)
Write-Host ("task={0} state={1}" -f $Top30TaskName, $top30State)

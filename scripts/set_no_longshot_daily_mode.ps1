[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("daemon", "task", "off")]
  [string]$Mode,
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$ConfigFile = "configs/bot_supervisor.observe.json",
  [string]$DaemonJobName = "no_longshot_daily_daemon",
  [string]$TaskName = "NoLongshotDailyReport",
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

function Set-DaemonJobEnabled([string]$ConfigPath, [string]$JobName, [bool]$Enabled) {
  if (-not (Test-Path $ConfigPath)) {
    throw "config not found: $ConfigPath"
  }
  $raw = Get-Content -Path $ConfigPath -Raw -Encoding UTF8
  $cfg = $raw | ConvertFrom-Json
  if ($null -eq $cfg -or $null -eq $cfg.jobs) {
    throw "invalid config (jobs missing): $ConfigPath"
  }

  $target = $null
  foreach ($job in @($cfg.jobs)) {
    if ([string]$job.name -eq $JobName) {
      $target = $job
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

function Set-TaskEnabled([string]$Name, [bool]$Enabled) {
  $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if ($null -eq $task) {
    return "MISSING"
  }

  if ($Enabled) {
    Enable-ScheduledTask -TaskName $Name | Out-Null
  } else {
    if ([string]$task.State -eq "Running") {
      try {
        Stop-ScheduledTask -TaskName $Name | Out-Null
      } catch {
      }
    }
    Disable-ScheduledTask -TaskName $Name | Out-Null
  }

  $updated = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if ($null -eq $updated) {
    return "MISSING"
  }
  return [string]$updated.State
}

function Stop-NoLongshotDaemonProcesses([string]$RepoRoot) {
  $rows = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
      $_.Name -like "python*" -and
      $_.CommandLine -like "*no_longshot_daily_daemon.py*" -and
      $_.CommandLine -like "*$RepoRoot*"
    }

  $stopped = @()
  foreach ($row in @($rows)) {
    $procId = [int]$row.ProcessId
    if ($procId -le 0) { continue }
    try {
      Stop-Process -Id $procId -Force -ErrorAction Stop
      $stopped += $procId
    } catch {
    }
  }
  return @($stopped)
}

$configPath = Resolve-PathFromRepo -Root $RepoRoot -Raw $ConfigFile

$daemonEnabled = $false
$taskEnabled = $false
switch ($Mode) {
  "daemon" {
    $daemonEnabled = $true
    $taskEnabled = $false
  }
  "task" {
    $daemonEnabled = $false
    $taskEnabled = $true
  }
  "off" {
    $daemonEnabled = $false
    $taskEnabled = $false
  }
}

$configChanged = Set-DaemonJobEnabled -ConfigPath $configPath -JobName $DaemonJobName -Enabled:$daemonEnabled
$taskState = Set-TaskEnabled -Name $TaskName -Enabled:$taskEnabled
$stoppedDaemonPids = @()
if ($Mode -in @("task", "off")) {
  $stoppedDaemonPids = Stop-NoLongshotDaemonProcesses -RepoRoot $RepoRoot
}

Write-Host ("mode={0}" -f $Mode)
Write-Host ("config={0}" -f $configPath)
Write-Host ("daemon_job={0} enabled={1}" -f $DaemonJobName, $daemonEnabled)
Write-Host ("config_changed={0}" -f $configChanged)
Write-Host ("task={0} state={1}" -f $TaskName, $taskState)
Write-Host ("daemon_processes_stopped={0}" -f (@($stoppedDaemonPids).Count))
if (@($stoppedDaemonPids).Count -gt 0) {
  Write-Host ("daemon_stopped_pids={0}" -f ((@($stoppedDaemonPids) -join ",")))
}

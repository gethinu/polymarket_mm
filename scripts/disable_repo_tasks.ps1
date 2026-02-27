[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [switch]$NoKillProcesses,
  [switch]$NoStopRunningTasks,
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

function Normalize-Path([string]$PathText) {
  if ([string]::IsNullOrWhiteSpace($PathText)) {
    return ""
  }
  return ([System.IO.Path]::GetFullPath($PathText)).TrimEnd('\').ToLowerInvariant()
}

function Contains-RepoPath([string]$Text, [string]$RepoNorm) {
  if ([string]::IsNullOrWhiteSpace($Text)) {
    return $false
  }
  $normalized = $Text.Replace("/", "\").ToLowerInvariant()
  return $normalized.Contains($RepoNorm)
}

if (-not (Test-Path $RepoRoot)) {
  throw "RepoRoot not found: $RepoRoot"
}

$repoResolved = (Resolve-Path $RepoRoot).Path
$repoNorm = Normalize-Path $repoResolved
$scriptsNeedle = ($repoNorm + "\scripts\")
$selfPid = $PID

$allTasks = Get-ScheduledTask
$repoTasks = @()
foreach ($task in $allTasks) {
  $hasRepoAction = $false
  foreach ($action in @($task.Actions)) {
    if ($null -eq $action) { continue }
    if (Contains-RepoPath -Text ([string]$action.Arguments) -RepoNorm $repoNorm) {
      $hasRepoAction = $true
      break
    }
    if (Contains-RepoPath -Text ([string]$action.WorkingDirectory) -RepoNorm $repoNorm) {
      $hasRepoAction = $true
      break
    }
  }
  if ($hasRepoAction) {
    $repoTasks += $task
  }
}

$changedTasks = @()
$stoppedRunningCount = 0
$disabledCount = 0
foreach ($task in ($repoTasks | Sort-Object TaskName)) {
  $beforeState = [string]$task.State

  if (-not $NoStopRunningTasks -and $beforeState -eq "Running") {
    try {
      Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction Stop
      $stoppedRunningCount += 1
    } catch {
    }
  }

  if ($beforeState -ne "Disabled") {
    try {
      Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
      $disabledCount += 1
    } catch {
    }
  }

  $after = Get-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue
  $afterState = if ($null -eq $after) { "MISSING" } else { [string]$after.State }
  $changedTasks += [pscustomobject]@{
    TaskName = $task.TaskName
    Before = $beforeState
    After = $afterState
  }
}

$stoppedProcesses = @()
if (-not $NoKillProcesses) {
  $procTargets = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $selfPid -and
    $_.Name -in @("python.exe", "pythonw.exe", "pwsh.exe", "powershell.exe", "wscript.exe", "cscript.exe") -and
    (Contains-RepoPath -Text ([string]$_.CommandLine) -RepoNorm $scriptsNeedle) -and
    ($_.CommandLine -notmatch "disable_repo_tasks\.ps1")
  }

  foreach ($p in $procTargets) {
    try {
      Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
      $stoppedProcesses += [pscustomobject]@{
        ProcessId = $p.ProcessId
        Name = $p.Name
      }
    } catch {
    }
  }
}

Write-Host ("repo_root={0}" -f $repoResolved)
Write-Host ("tasks_found={0} disabled_now={1} stopped_running_now={2}" -f $repoTasks.Count, $disabledCount, $stoppedRunningCount)
if ($changedTasks.Count -gt 0) {
  $changedTasks | Sort-Object TaskName | Format-Table -AutoSize
}
Write-Host ("processes_stopped={0}" -f $stoppedProcesses.Count)
if ($stoppedProcesses.Count -gt 0) {
  $stoppedProcesses | Sort-Object Name,ProcessId | Format-Table -AutoSize
}

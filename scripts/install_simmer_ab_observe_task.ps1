[CmdletBinding()]
param(
  [string]$TaskName = "SimmerABObserveSupervisor",
  [string]$FallbackTaskName = "SimmerWeatherAutoTrade",
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PowerShellExe = "powershell.exe",
  [string]$PythonExe = "python",
  [string]$ConfigFile = "configs/bot_supervisor.simmer_ab.observe.json",
  [string]$LogFile = "logs/simmer_ab_supervisor.log",
  [string]$StateFile = "logs/simmer_ab_supervisor_state.json",
  [string]$LockFile = "logs/simmer_ab_observe_supervisor.lock",
  [double]$PollSec = 1,
  [double]$WriteStateSec = 2,
  [int]$RunSeconds = 0,
  [string]$StartupScriptName = "SimmerABObserveSupervisor.cmd",
  [switch]$NoStartupFolderFallback,
  [ValidateSet("Logon", "Startup")]
  [string]$StartMode = "Logon",
  [ValidateSet("auto", "default", "s4u", "interactive")]
  [string]$PrincipalMode = "auto",
  [switch]$RunNow,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_simmer_ab_observe_task.ps1 -NoBackground [-TaskName SimmerABObserveSupervisor] [-FallbackTaskName SimmerWeatherAutoTrade] [-StartupScriptName SimmerABObserveSupervisor.cmd] [-NoStartupFolderFallback] [-StartMode Logon|Startup] [-PrincipalMode auto|default|s4u|interactive] [-LockFile logs/simmer_ab_observe_supervisor.lock] [-RunNow]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_simmer_ab_observe_task.ps1 -NoBackground -?"
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
  } else {
    throw "PowerShell executable not found: $PowerShellExe"
  }
}

$runnerPath = Join-Path $RepoRoot "scripts\run_simmer_ab_observe_supervisor.ps1"
if (-not (Test-Path $runnerPath)) {
  throw "Runner not found: $runnerPath"
}

$configPath = if ([System.IO.Path]::IsPathRooted($ConfigFile)) { $ConfigFile } else { Join-Path $RepoRoot $ConfigFile }
if (-not (Test-Path $configPath)) {
  throw "Config not found: $configPath"
}

if ($PollSec -le 0) { throw "PollSec must be > 0" }
if ($WriteStateSec -le 0) { throw "WriteStateSec must be > 0" }
if ($RunSeconds -lt 0) { throw "RunSeconds must be >= 0" }
if ([string]::IsNullOrWhiteSpace($StartupScriptName)) { throw "StartupScriptName must not be empty" }

$actionArgs = @(
  "-NoLogo",
  "-NoProfile",
  "-NonInteractive",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runnerPath),
  "-NoBackground",
  "-RepoRoot", ('"{0}"' -f $RepoRoot),
  "-PythonExe", ('"{0}"' -f $PythonExe),
  "-ConfigFile", ('"{0}"' -f $ConfigFile),
  "-LogFile", ('"{0}"' -f $LogFile),
  "-StateFile", ('"{0}"' -f $StateFile),
  "-LockFile", ('"{0}"' -f $LockFile),
  "-PollSec", ([string]$PollSec),
  "-WriteStateSec", ([string]$WriteStateSec)
)
if ($RunSeconds -gt 0) {
  $actionArgs += @("-RunSeconds", ([string]$RunSeconds))
}
$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument ($actionArgs -join " ")

if ($StartMode -eq "Startup") {
  $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
  $trigger = New-ScheduledTaskTrigger -AtLogOn
}

$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$desc = "Run Simmer A/B observe collectors via bot supervisor (observe-only)"

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
$registerError = ""
$principalModeUsed = "default"
$mode = ($PrincipalMode | ForEach-Object { [string]$_ }).Trim().ToLowerInvariant()

if ($mode -eq "default") {
  try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
    $registered = $true
    $principalModeUsed = "default"
  } catch {
    $registerError = $_.Exception.Message
  }
} elseif ($mode -in @("s4u", "interactive")) {
  if ([string]::IsNullOrWhiteSpace($currentUser)) {
    throw "Cannot use PrincipalMode=$mode because current user resolution failed."
  }
  $logonType = if ($mode -eq "s4u") { "S4U" } else { "Interactive" }
  try {
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType $logonType -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
    $registered = $true
    $principalModeUsed = "${logonType}:$currentUser"
  } catch {
    $registerError = $_.Exception.Message
  }
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
        $registerError = $_.Exception.Message
      }
    }
  }
  if (-not $registered) {
    try {
      Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $desc -Force | Out-Null
      $registered = $true
      $principalModeUsed = "default"
    } catch {
      $registerError = $_.Exception.Message
    }
  }
}

if (-not $registered) {
  $fallbackError = ""
  if (-not [string]::IsNullOrWhiteSpace($FallbackTaskName)) {
    try {
      $existing = Get-ScheduledTask -TaskName $FallbackTaskName -ErrorAction Stop
      Stop-ScheduledTask -TaskName $FallbackTaskName -ErrorAction SilentlyContinue
      Set-ScheduledTask -TaskName $FallbackTaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
      try { Enable-ScheduledTask -TaskName $FallbackTaskName | Out-Null } catch {}
      if ($RunNow.IsPresent) {
        Start-ScheduledTask -TaskName $FallbackTaskName
      }
      Write-Host ("Registered principal mode: fallback_reuse:{0}" -f $FallbackTaskName)
      Get-ScheduledTask -TaskName $FallbackTaskName | Select-Object TaskName,State
      Get-ScheduledTask -TaskName $FallbackTaskName | Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult,NextRunTime
      exit 0
    } catch {
      $fallbackError = $_.Exception.Message
    }
  }

  if (-not $NoStartupFolderFallback.IsPresent) {
    try {
      $startupDir = [Environment]::GetFolderPath("Startup")
      if ([string]::IsNullOrWhiteSpace($startupDir)) {
        throw "Startup folder path is empty"
      }
      if (-not (Test-Path $startupDir)) {
        New-Item -Path $startupDir -ItemType Directory -Force | Out-Null
      }
      $startupScriptPath = Join-Path $startupDir $StartupScriptName
      $runnerCmd = @(
        "@echo off",
        "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""$runnerPath"" -RepoRoot ""$RepoRoot"" -PythonExe ""$PythonExe"" -ConfigFile ""$ConfigFile"" -LogFile ""$LogFile"" -StateFile ""$StateFile"" -LockFile ""$LockFile"" -PollSec $PollSec -WriteStateSec $WriteStateSec" + $(if ($RunSeconds -gt 0) { " -RunSeconds $RunSeconds" } else { "" }),
        "exit /b 0"
      )
      Set-Content -Path $startupScriptPath -Value $runnerCmd -Encoding ascii -Force

      if ($RunNow.IsPresent) {
        $runArgs = @(
          "-NoLogo",
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy", "Bypass",
          "-File", $runnerPath,
          "-RepoRoot", $RepoRoot,
          "-PythonExe", $PythonExe,
          "-ConfigFile", $ConfigFile,
          "-LogFile", $LogFile,
          "-StateFile", $StateFile,
          "-LockFile", $LockFile,
          "-PollSec", ([string]$PollSec),
          "-WriteStateSec", ([string]$WriteStateSec)
        )
        if ($RunSeconds -gt 0) {
          $runArgs += @("-RunSeconds", ([string]$RunSeconds))
        }
        & $PowerShellExe @runArgs
      }

      Write-Host ("Registered principal mode: startup_folder_fallback:{0}" -f $startupScriptPath)
      exit 0
    } catch {
      throw ("Failed to register task '{0}', fallback task '{1}', and startup-folder fallback. register_error={2}; fallback_error={3}; startup_error={4}" -f $TaskName, $FallbackTaskName, $registerError, $fallbackError, $_.Exception.Message)
    }
  }

  throw ("Failed to register task '{0}'. register_error={1}; fallback_error={2}" -f $TaskName, $registerError, $fallbackError)
}

Write-Host ("Registered principal mode: {0}" -f $principalModeUsed)

try {
  Enable-ScheduledTask -TaskName $TaskName | Out-Null
} catch {
}

if ($RunNow.IsPresent) {
  Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult,NextRunTime

param(
  [switch]$Background,
  [switch]$NoBackground,
  [string]$ConfigFile = "",
  [int]$RunSeconds = 0
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
$supervisorPy = Join-Path $baseDir "scripts\bot_supervisor.py"
$configDefault = Join-Path $baseDir "configs\bot_supervisor.observe.json"
$logDir = Join-Path $baseDir "logs"
$logFile = Join-Path $logDir "bot-supervisor.log"
$stateFile = Join-Path $logDir "bot_supervisor_state.json"

if (-not (Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}

if (-not (Test-Path $supervisorPy)) {
  Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] error: supervisor script not found: $supervisorPy"
  exit 1
}

$config = $ConfigFile
if ([string]::IsNullOrWhiteSpace($config)) {
  $envConfig = [Environment]::GetEnvironmentVariable("BOTSUP_CONFIG", "Process")
  if ([string]::IsNullOrWhiteSpace($envConfig)) {
    $envConfig = [Environment]::GetEnvironmentVariable("BOTSUP_CONFIG", "User")
  }
  if ([string]::IsNullOrWhiteSpace($envConfig)) {
    $envConfig = [Environment]::GetEnvironmentVariable("BOTSUP_CONFIG", "Machine")
  }
  if (-not [string]::IsNullOrWhiteSpace($envConfig)) {
    $config = $envConfig
  } else {
    $config = $configDefault
  }
}

$runSec = $RunSeconds
if ($runSec -le 0) {
  $envRun = [Environment]::GetEnvironmentVariable("BOTSUP_RUN_SECONDS", "Process")
  if ([string]::IsNullOrWhiteSpace($envRun)) {
    $envRun = [Environment]::GetEnvironmentVariable("BOTSUP_RUN_SECONDS", "User")
  }
  if ([string]::IsNullOrWhiteSpace($envRun)) {
    $envRun = [Environment]::GetEnvironmentVariable("BOTSUP_RUN_SECONDS", "Machine")
  }
  if (-not [string]::IsNullOrWhiteSpace($envRun)) {
    [int]$parsed = 0
    if ([int]::TryParse($envRun, [ref]$parsed) -and $parsed -gt 0) {
      $runSec = $parsed
    }
  }
}

$args = @(
  $supervisorPy,
  "run",
  "--config", $config,
  "--log-file", $logFile,
  "--state-file", $stateFile,
  "--poll-sec", "1",
  "--write-state-sec", "2",
  "--halt-when-all-stopped"
)

if ($runSec -gt 0) {
  $args += @("--run-seconds", "$runSec")
}

Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] supervisor run start config=$config run_seconds=$runSec"
& python @args
$code = $LASTEXITCODE
Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] supervisor run end code=$code"
exit $code

[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [switch]$LiveExecute,
  [string]$LiveConfirm = "",
  [string]$SignalsFile = "logs/event-driven-observe-signals.jsonl",
  [string]$StateFile = "logs/event_driven_live_state.json",
  [string]$ExecLogFile = "logs/event_driven_live_executions.jsonl",
  [string]$LiveLogFile = "logs/event_driven_live.log",
  [string]$RunLogFile = "logs/event_driven_live_exit_check.log",
  [double]$WinThreshold = 0.99,
  [double]$LoseThreshold = 0.01,
  [double]$ApiTimeoutSec = 20,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_live_exit_check.ps1 -NoBackground [-LiveExecute -LiveConfirm YES] [-WinThreshold 0.99] [-LoseThreshold 0.01]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_live_exit_check.ps1 -NoBackground -?"
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

function Resolve-PathLike([string]$base, [string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  if ([System.IO.Path]::IsPathRooted($raw)) { return $raw }
  return (Join-Path $base $raw)
}

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLogAbs -Append -Encoding utf8
  Write-Host $line
}

if ($LiveExecute.IsPresent -and [string]$LiveConfirm -ne "YES") {
  throw "Refusing live exit-check mode: specify -LiveConfirm YES with -LiveExecute"
}

$scriptPath = Join-Path $RepoRoot "scripts\execute_event_driven_live.py"
if (-not (Test-Path $scriptPath)) {
  throw "Live helper not found: $scriptPath"
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}

$signalsAbs = Resolve-PathLike $RepoRoot $SignalsFile
$stateAbs = Resolve-PathLike $RepoRoot $StateFile
$execLogAbs = Resolve-PathLike $RepoRoot $ExecLogFile
$liveLogAbs = Resolve-PathLike $RepoRoot $LiveLogFile
$runLogAbs = Resolve-PathLike $RepoRoot $RunLogFile

$cmdArgs = @(
  $scriptPath,
  "--signals-file", $signalsAbs,
  "--state-file", $stateAbs,
  "--exec-log-file", $execLogAbs,
  "--log-file", $liveLogAbs,
  "--win-threshold", "$WinThreshold",
  "--lose-threshold", "$LoseThreshold",
  "--api-timeout-sec", "$ApiTimeoutSec",
  "--exit-only",
  "--pretty"
)
if ($LiveExecute.IsPresent) {
  $cmdArgs += @("--execute", "--confirm-live", "$LiveConfirm")
}

Log ("start live_execute={0} win_threshold={1} lose_threshold={2} state={3}" -f [bool]$LiveExecute.IsPresent, $WinThreshold, $LoseThreshold, $stateAbs)
$output = & $PythonExe @cmdArgs 2>&1
if ($output) {
  ($output -join [Environment]::NewLine) | Out-File -FilePath $runLogAbs -Append -Encoding utf8
  Write-Host ($output -join [Environment]::NewLine)
}
if ($LASTEXITCODE -ne 0) {
  throw "event-driven live exit-check failed with code ${LASTEXITCODE}"
}
Log "done"

param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [double]$Hours = 24.0,
  [ValidateSet("since_baseline", "since_supervisor_start")]
  [string]$MetricScope = "since_baseline",
  [int]$TailBytes = 167772160,
  [string]$SupervisorStateFile = "logs/fade_observe_supervisor_state.json",
  [string]$ControlArmId = "regime_both_core",
  [switch]$NoControlSelfForControlArm,
  [switch]$RefreshStrategySnapshot,
  [Alias("h")]
  [switch]$Help,
  [switch]$Background,
  [switch]$NoBackground
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_fade_regime_staged_checks.ps1 -NoBackground [-Hours 24] [-MetricScope since_baseline] [-ControlArmId regime_both_core] [-RefreshStrategySnapshot]"
  Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_fade_regime_staged_checks.ps1 -NoBackground -?"
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

function Resolve-ToolPath([string]$Root, [string]$RelativePath) {
  $p = Join-Path $Root $RelativePath
  if (-not (Test-Path $p)) {
    throw "tool not found: $p"
  }
  return $p
}

function Resolve-PowerShellPath([string]$Raw) {
  if (Test-Path $Raw) { return $Raw }
  if ($Raw -match "^[A-Za-z]:\\") {
    throw "PowerShell executable not found: $Raw"
  }
  $resolved = Get-Command $Raw -ErrorAction SilentlyContinue
  if ($null -eq $resolved) {
    throw "PowerShell executable not found: $Raw"
  }
  return $resolved.Source
}

function Run-Python([string[]]$CmdArgs) {
  & $PythonExe @CmdArgs
  if ($LASTEXITCODE -ne 0) {
    throw "python failed with code ${LASTEXITCODE}: $($CmdArgs -join ' ')"
  }
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) {
  New-Item -Path $logsDir -ItemType Directory -Force | Out-Null
}
$runLog = Join-Path $logsDir "fade_regime_staged_checks_run.log"

function Log([string]$msg) {
  $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $msg"
  $line | Out-File -FilePath $runLog -Append -Encoding utf8
  Write-Host $line
}

$runTool = Resolve-ToolPath -Root $RepoRoot -RelativePath "scripts\run_fade_regime_staged_checks.py"
$snapshotTool = Join-Path $RepoRoot "scripts\render_strategy_register_snapshot.py"

$outJson = Join-Path $logsDir "fade_regime_staged_decision_latest.json"
$outTxt = Join-Path $logsDir "fade_regime_staged_decision_latest.txt"
$supervisorStateAbs = if ([System.IO.Path]::IsPathRooted($SupervisorStateFile)) { $SupervisorStateFile } else { Join-Path $RepoRoot $SupervisorStateFile }

$runArgs = @(
  $runTool,
  "--hours", ([string]$Hours),
  "--metric-scope", $MetricScope,
  "--tail-bytes", ([string]$TailBytes),
  "--supervisor-state-file", $supervisorStateAbs,
  "--control-arm-id", $ControlArmId,
  "--out-json", $outJson,
  "--out-txt", $outTxt
)
if ($NoControlSelfForControlArm.IsPresent) {
  $runArgs += "--no-control-self-for-control-arm"
} else {
  $runArgs += "--control-self-for-control-arm"
}

Log ("start hours={0} metric_scope={1} control_arm={2} control_self={3}" -f $Hours, $MetricScope, $ControlArmId, (-not $NoControlSelfForControlArm.IsPresent))
Run-Python $runArgs
Log ("batch_check done -> {0}" -f $outJson)

if ($RefreshStrategySnapshot.IsPresent) {
  if (-not (Test-Path $snapshotTool)) {
    Log "strategy_snapshot skip: renderer not found"
  } else {
    Run-Python @(
      $snapshotTool,
      "--pretty"
    )
    Log "strategy_snapshot done -> logs/strategy_register_latest.json"
  }
}

if (Test-Path $outTxt) {
  try {
    $summary = Get-Content -Path $outTxt -Raw
    foreach ($line in ($summary -split "`r?`n")) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      Log $line
    }
  } catch {
    Log "summary read failed (non-fatal): $($_.Exception.Message)"
  }
}

Log "done"

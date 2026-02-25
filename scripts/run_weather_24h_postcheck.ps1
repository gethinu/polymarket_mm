[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Repos\polymarket_mm",
  [string]$PythonExe = "python",
  [string]$ObserveLogFile = "logs/clob-arb-weather-observe-24h.log",
  [double]$Hours = 24.0,
  [string]$ThresholdsCents = "0,1,2,3,5,10",
  [double]$MinUsefulPct = 20.0,
  [string]$ReportFile = "logs/weather24h_postcheck_latest.txt",
  [string]$SummaryFile = "logs/weather24h_postcheck_latest.json",
  [string]$AlarmLogFile = "logs/alarm_weather24h.log",
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
    "-File", ('"{0}"' -f $ScriptPath.Replace('"', '\"')),
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
    $text = [string]$value
    if ($text -match '[\s"]') {
      $text = ('"{0}"' -f $text.Replace('"', '\"'))
    }
    $argList += $text
  }

  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden -PassThru
  Write-Host ("Started in background: pid={0} script={1}" -f $proc.Id, $ScriptPath)
  exit 0
}

function Resolve-RepoPath {
  param([Parameter(Mandatory = $true)][string]$PathText)
  if ([System.IO.Path]::IsPathRooted($PathText)) {
    return $PathText
  }
  $repoRoot = Split-Path -Parent $PSScriptRoot
  return (Join-Path $repoRoot $PathText)
}

function Ensure-ParentDir {
  param([Parameter(Mandatory = $true)][string]$PathText)
  $parent = Split-Path -Parent $PathText
  if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

$repoPath = (Resolve-Path $RepoRoot).Path
$reportScriptPath = Join-Path $repoPath "scripts\report_clob_observation.py"
if (-not (Test-Path $reportScriptPath)) {
  throw "Observation report script not found: $reportScriptPath"
}

$observeLogPath = Resolve-RepoPath -PathText $ObserveLogFile
$reportOutPath = Resolve-RepoPath -PathText $ReportFile
$summaryPath = Resolve-RepoPath -PathText $SummaryFile
$alarmLogPath = Resolve-RepoPath -PathText $AlarmLogFile

Ensure-ParentDir -PathText $reportOutPath
Ensure-ParentDir -PathText $summaryPath
Ensure-ParentDir -PathText $alarmLogPath

$hoursArg = [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0}", $Hours)
$cmdArgs = @(
  $reportScriptPath,
  "--log-file", $observeLogPath,
  "--hours", $hoursArg,
  "--thresholds-cents", $ThresholdsCents
)

$cmdRendered = $PythonExe + " " + (($cmdArgs | ForEach-Object {
  $s = [string]$_
  if ($s -match '\s') { '"' + $s + '"' } else { $s }
}) -join " ")

$outputLines = & $PythonExe @cmdArgs 2>&1
$exitCode = $LASTEXITCODE
$outputText = (($outputLines | ForEach-Object { [string]$_ }) -join [Environment]::NewLine)
if ([string]::IsNullOrWhiteSpace($outputText)) {
  $outputText = "(no output)"
}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$reportBody = @(
  "[$ts] weather24h postcheck",
  "command: $cmdRendered",
  "exit_code: $exitCode",
  "---",
  $outputText
) -join [Environment]::NewLine
Set-Content -Path $reportOutPath -Value $reportBody -Encoding UTF8

$match0 = [regex]::Match($outputText, '>=\s+\$0\.0000\s*:\s*(?<hit>\d+)\s*/\s*(?<total>\d+)\s*\((?<pct>\d+(?:\.\d+)?)%\)')
$match5 = [regex]::Match($outputText, '>=\s+\$0\.0500\s*:\s*(?<hit>\d+)\s*/\s*(?<total>\d+)\s*\((?<pct>\d+(?:\.\d+)?)%\)')

$pct0 = $null
$hit0 = $null
$total0 = $null
if ($match0.Success) {
  $pct0 = [double]$match0.Groups["pct"].Value
  $hit0 = [int]$match0.Groups["hit"].Value
  $total0 = [int]$match0.Groups["total"].Value
}

$pct5 = $null
$hit5 = $null
$total5 = $null
if ($match5.Success) {
  $pct5 = [double]$match5.Groups["pct"].Value
  $hit5 = [int]$match5.Groups["hit"].Value
  $total5 = [int]$match5.Groups["total"].Value
}

$decision = "REVIEW"
if ($exitCode -ne 0) {
  $decision = "ERROR"
}
elseif ($null -ne $pct0 -and $pct0 -ge $MinUsefulPct) {
  $decision = "ADOPT"
}

$summary = [ordered]@{
  ts_local = $ts
  observe_log_file = $observeLogPath
  hours = $Hours
  thresholds_cents = $ThresholdsCents
  min_useful_pct = $MinUsefulPct
  report_exit_code = $exitCode
  positive_0c_hits = $hit0
  positive_0c_total = $total0
  positive_0c_pct = $pct0
  positive_5c_hits = $hit5
  positive_5c_total = $total5
  positive_5c_pct = $pct5
  decision = $decision
  report_file = $reportOutPath
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $summaryPath -Encoding UTF8

$alarmLine = "[$ts] POSTCHECK decision=$decision exit=$exitCode"
if ($null -ne $pct0) {
  $alarmLine += (" positive0c={0}% threshold={1}%" -f $pct0.ToString("0.0", [System.Globalization.CultureInfo]::InvariantCulture), $MinUsefulPct.ToString("0.0", [System.Globalization.CultureInfo]::InvariantCulture))
}
Add-Content -Path $alarmLogPath -Value $alarmLine

Write-Host $alarmLine
if ($exitCode -ne 0) {
  exit $exitCode
}
exit 0

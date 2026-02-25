[CmdletBinding()]
param(
  [string]$WaiterStateFile = "logs/weather24h_alarm_waiter_state.json"
)

$ErrorActionPreference = "Stop"

function Resolve-RepoPath {
  param([Parameter(Mandatory = $true)][string]$PathText)
  if ([System.IO.Path]::IsPathRooted($PathText)) {
    return $PathText
  }
  $repoRoot = Split-Path -Parent $PSScriptRoot
  return (Join-Path $repoRoot $PathText)
}

$statePath = Resolve-RepoPath -PathText $WaiterStateFile
if (-not (Test-Path $statePath)) {
  Write-Host "No waiter state file found."
  exit 0
}

$stateObj = $null
try {
  $stateObj = Get-Content -Path $statePath -Raw | ConvertFrom-Json
} catch {
  throw "Failed to parse waiter state file: $statePath"
}

$state = [ordered]@{}
foreach ($prop in $stateObj.PSObject.Properties) {
  $state[$prop.Name] = $prop.Value
}

$pidToStop = 0
if ($null -ne $state.waiter_pid) {
  $pidToStop = [int]$state.waiter_pid
}

if ($pidToStop -gt 0) {
  $proc = Get-Process -Id $pidToStop -ErrorAction SilentlyContinue
  if ($null -ne $proc) {
    Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
  }
}

$state.status = "canceled"
$state.canceled_ts_local = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
$state | ConvertTo-Json -Depth 5 | Set-Content -Path $statePath -Encoding UTF8

Write-Host "Alarm waiter canceled."

param(
  [Parameter(Mandatory = $false)]
  [string]$FunderAddress,
  [Parameter(Mandatory = $false)]
  [ValidateSet("buckets", "yes-no", "both")]
  [string]$Strategy = "buckets",
  [Parameter(Mandatory = $false)]
  [int]$MaxLegs = 0,
  [Parameter(Mandatory = $false)]
  [int]$RunSeconds = 115,
  [Parameter(Mandatory = $false)]
  [switch]$RestartTask,
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

  # setup_clob_backend.ps1 is interactive, so keep the spawned console visible.
  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Normal -PassThru
  Write-Host ("Started in background (interactive window): pid={0} script={1}" -f $proc.Id, $ScriptPath)
  exit 0
}

if (-not $Background -and -not $NoBackground) {
  Start-BackgroundSelf -ScriptPath $PSCommandPath -BoundParameters $PSBoundParameters
}

function Read-SecretSecureString {
  param([string]$Prompt)
  return (Read-Host -Prompt $Prompt -AsSecureString)
}

function SecureStringToText {
  param([Security.SecureString]$Secure)
  if (-not $Secure) { return "" }
  $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { if ($ptr -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) } }
}

function Save-DpapiSecretFile {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][Security.SecureString]$Secure
  )

  $dir = Split-Path -Parent $Path
  if (-not (Test-Path $dir)) {
    New-Item -Path $dir -ItemType Directory -Force | Out-Null
  }

  # DPAPI-encrypted string bound to this Windows user.
  $enc = $Secure | ConvertFrom-SecureString
  Set-Content -Path $Path -Value $enc -Encoding ASCII -NoNewline

  # Lock down permissions to current user only.
  try {
    # Use ${env:...} to avoid PowerShell parsing "$env:USERNAME:..." as another env var name.
    & icacls $dir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" /T | Out-Null
    & icacls $Path /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  }
  catch {
    Write-Warning "Could not harden secret file ACLs automatically: $($_.Exception.Message)"
  }
}

Write-Host "=== CLOB Backend Setup (Secure) ==="
Write-Host "This stores secrets as DPAPI-encrypted files (not in env vars)."
Write-Host ""

$openclawHome = Join-Path $env:USERPROFILE ".openclaw"
$secretsDir = Join-Path $openclawHome "secrets"
$pkFile = Join-Path $secretsDir "pm_private_key.dpapi.txt"
$apiSecretFile = Join-Path $secretsDir "pm_api_secret.dpapi.txt"
$apiPassFile = Join-Path $secretsDir "pm_api_passphrase.dpapi.txt"

$pkSecure = Read-SecretSecureString "Enter PM private key (64 hex chars; may start with 0x or not)"
$pk = (SecureStringToText $pkSecure).Trim()
if ([string]::IsNullOrWhiteSpace($pk)) {
  throw "Private key is required."
}

# Normalize / validate: accept 64-hex (with or without 0x prefix).
if ($pk -match '^[0-9a-fA-F]{64}$') {
  $pk = "0x$pk"
}
elseif ($pk -notmatch '^0x[0-9a-fA-F]{64}$') {
  throw "Invalid EVM private key format. Expected 64 hex characters (optionally prefixed with 0x). Do NOT paste your seed phrase."
}

$pkSecureNorm = ConvertTo-SecureString -String $pk -AsPlainText -Force
Save-DpapiSecretFile -Path $pkFile -Secure $pkSecureNorm

if ([string]::IsNullOrWhiteSpace($FunderAddress)) {
  $FunderAddress = Read-Host "Enter PM_FUNDER address (wallet/funder address, 0x...)"
}
if ([string]::IsNullOrWhiteSpace($FunderAddress)) {
  throw "PM_FUNDER is required."
}

# Optional API creds. If omitted, runtime derives creds with signer.
$apiKey = Read-Host "Optional PM_API_KEY (press Enter to skip)"
$apiSecretSecure = Read-SecretSecureString "Optional PM_API_SECRET (press Enter to skip)"
$apiPassSecure = Read-SecretSecureString "Optional PM_API_PASSPHRASE (press Enter to skip)"

[Environment]::SetEnvironmentVariable("PM_PRIVATE_KEY", $null, "User") # avoid plaintext in user env
[Environment]::SetEnvironmentVariable("PM_PRIVATE_KEY_DPAPI_FILE", $pkFile, "User")
[Environment]::SetEnvironmentVariable("PM_FUNDER", $FunderAddress, "User")
[Environment]::SetEnvironmentVariable("CLOBBOT_EXEC_BACKEND", "clob", "User")
[Environment]::SetEnvironmentVariable("CLOBBOT_EXECUTE", "1", "User")
[Environment]::SetEnvironmentVariable("CLOBBOT_STRATEGY", $Strategy, "User")
[Environment]::SetEnvironmentVariable("CLOBBOT_MAX_LEGS", $MaxLegs.ToString(), "User")
[Environment]::SetEnvironmentVariable("CLOBBOT_RUN_SECONDS", $RunSeconds.ToString(), "User")

if (-not [string]::IsNullOrWhiteSpace($apiKey)) {
  [Environment]::SetEnvironmentVariable("PM_API_KEY", $apiKey, "User")
}

$apiSecret = SecureStringToText $apiSecretSecure
$apiPass = SecureStringToText $apiPassSecure
if (-not [string]::IsNullOrWhiteSpace($apiSecret) -and -not [string]::IsNullOrWhiteSpace($apiPass)) {
  Save-DpapiSecretFile -Path $apiSecretFile -Secure $apiSecretSecure
  Save-DpapiSecretFile -Path $apiPassFile -Secure $apiPassSecure
  [Environment]::SetEnvironmentVariable("PM_API_SECRET_DPAPI_FILE", $apiSecretFile, "User")
  [Environment]::SetEnvironmentVariable("PM_API_PASSPHRASE_DPAPI_FILE", $apiPassFile, "User")
  [Environment]::SetEnvironmentVariable("PM_API_SECRET", $null, "User")
  [Environment]::SetEnvironmentVariable("PM_API_PASSPHRASE", $null, "User")
  Write-Host "Saved PM API secret/passphrase as DPAPI-encrypted files."
} else {
  Write-Host "Skipped PM API secret/passphrase (runtime will derive creds if needed)."
}

Write-Host ""
Write-Host "Saved:"
Write-Host " - PM_PRIVATE_KEY_DPAPI_FILE = $pkFile"
Write-Host " - PM_FUNDER = $FunderAddress"
Write-Host " - CLOBBOT_EXEC_BACKEND = clob"
Write-Host " - CLOBBOT_EXECUTE = 1"
Write-Host " - CLOBBOT_STRATEGY = $Strategy"
Write-Host " - CLOBBOT_MAX_LEGS = $MaxLegs"
Write-Host " - CLOBBOT_RUN_SECONDS = $RunSeconds"

if ($RestartTask) {
  try {
    Stop-ScheduledTask -TaskName PolymarketClobArbMonitor -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName PolymarketClobArbMonitor
    Write-Host "Task restarted: PolymarketClobArbMonitor"
  }
  catch {
    Write-Warning "Failed to restart task automatically: $($_.Exception.Message)"
  }
}

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "  Get-ScheduledTaskInfo -TaskName PolymarketClobArbMonitor"
Write-Host "  Get-Content C:\Repos\polymarket_mm\logs\clob-arb-monitor.log -Tail 80"

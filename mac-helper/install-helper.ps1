# Windows enrollment for the KVM AI Monitor push helper.
# Usage: powershell -ExecutionPolicy Bypass -File install-helper.ps1 -Kvm <host> -Device <device-id> [-Update]
# Requires Python 3 on PATH. The secret is encrypted with user-scoped Windows DPAPI.
param(
    [string]$Kvm = "",
    [string]$Device = "",
    [switch]$Update,
    [switch]$SecretStdin
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigDir = Join-Path $env:USERPROFILE ".kvm-ai-monitor"
$AppDir = Join-Path $env:LOCALAPPDATA "kvm-ai-monitor"
$TaskName = "kvm-ai-monitor-helper"

if ($Update) {
    if (-not (Test-Path (Join-Path $ConfigDir "helper.json"))) {
        Write-Error "-Update requires an existing installation (helper.json not found)."
    }
} elseif (-not $Kvm -or -not $Device) {
    Write-Error "Usage: install-helper.ps1 -Kvm <host> -Device <device-id> [-SecretStdin] | -Update"
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Error "Python 3 is required (https://www.python.org/downloads/)." }

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item (Join-Path $ProjectDir "mac-helper\kvm_ai_push.py") (Join-Path $AppDir "kvm_ai_push.py") -Force
Copy-Item (Join-Path $ProjectDir "mac-helper\kvm-ai-claude-hook.cmd") (Join-Path $AppDir "kvm-ai-claude-hook.cmd") -Force
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

if (-not $Update) {
    if ($SecretStdin) {
        $Secret = [Console]::In.ReadLine()
    } else {
        $SecureSecret = Read-Host "One-time device secret from the KVM AI Usage page" -AsSecureString
        $Secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureSecret))
    }
    if (-not $Secret) { Write-Error "Secret cannot be empty." }
    $Secret | & $python (Join-Path $AppDir "kvm_ai_push.py") store-secret --kvm $Kvm
    if ($LASTEXITCODE -ne 0) { Write-Error "Storing the secret failed." }
    Remove-Variable Secret

    $mergeScript = @'
import json, pathlib, sys
path, host, device = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
config = {}
if path.is_file():
    try:
        config = json.load(path.open())
    except ValueError:
        config = {}
targets = config.get("targets")
if not isinstance(targets, list):
    targets = []
    if config.get("kvmHost") and config.get("deviceId"):
        targets.append({"kvmHost": config["kvmHost"], "deviceId": config["deviceId"]})
targets = [t for t in targets if isinstance(t, dict) and t.get("kvmHost") != host]
targets.append({"kvmHost": host, "deviceId": device})
path.write_text(json.dumps({"targets": targets}, indent=2) + "\n")
'@
    $mergePath = Join-Path $env:TEMP "kvm-ai-merge.py"
    Set-Content -Path $mergePath -Value $mergeScript
    & $python $mergePath (Join-Path $ConfigDir "helper.json") $Kvm $Device
    Remove-Item $mergePath
}

$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$action = "`"$pythonw`" `"$(Join-Path $AppDir 'kvm_ai_push.py')`" send-usage"
schtasks /Create /F /SC MINUTE /MO 1 /TN $TaskName /TR $action | Out-Null
Write-Host "Scheduled task '$TaskName' created (every minute)."

Write-Host "Running an initial usage push..."
& $python (Join-Path $AppDir "kvm_ai_push.py") send-usage
if ($LASTEXITCODE -eq 0) { Write-Host "Initial push succeeded." }
else { Write-Warning "Initial push failed; check the KVM address and secret." }

Write-Host ""
Write-Host "To also send exact working/idle events from Claude Code on this device, run:"
Write-Host "  python `"$(Join-Path $ProjectDir 'mac-helper\claude_hooks.py')`" install `"$(Join-Path $AppDir 'kvm-ai-claude-hook.cmd')`""

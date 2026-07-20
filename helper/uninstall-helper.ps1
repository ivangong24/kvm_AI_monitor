# Windows counterpart to uninstall-helper.sh.
# Usage: powershell -ExecutionPolicy Bypass -File uninstall-helper.ps1 [-Purge]
param([switch]$Purge)

$ErrorActionPreference = "Stop"

$ConfigDir = Join-Path $env:USERPROFILE ".kvm-ai-monitor"
$AppDir = Join-Path $env:LOCALAPPDATA "kvm-ai-monitor"
$TaskName = "kvm-ai-monitor-helper"

# schtasks.exe for symmetry with the installer: Unregister-ScheduledTask needs elevation.
$existing = & schtasks.exe /Query /TN $TaskName 2>&1
if ($LASTEXITCODE -eq 0) {
    & schtasks.exe /Delete /TN $TaskName /F | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "Removing the scheduled task failed." }
    Write-Host "Scheduled task removed."
} else {
    Write-Host "No scheduled task was registered."
}

if (Test-Path $AppDir) {
    Remove-Item $AppDir -Recurse -Force
    Write-Host "Helper files removed."
}
$marker = Join-Path $ConfigDir "last-activity"
if (Test-Path $marker) { Remove-Item $marker -Force }

if ($Purge) {
    # Push secrets are DPAPI-encrypted files under the config directory.
    $secrets = Join-Path $ConfigDir "secrets"
    if (Test-Path $secrets) { Remove-Item $secrets -Recurse -Force }
    foreach ($name in "helper.json", "limits-cache.json") {
        $path = Join-Path $ConfigDir $name
        if (Test-Path $path) { Remove-Item $path -Force }
    }
    Write-Host "Purged helper config and the DPAPI push secrets."
} else {
    Write-Host "Left $ConfigDir\helper.json and the push secret in place (use -Purge to remove)."
}

Write-Host "KVM AI Monitor helper uninstalled."

# Windows counterpart to helper-status.sh: audit what the push helper has configured, whether
# the scheduled task is actually firing, and exactly what would be sent.
# Usage: powershell -ExecutionPolicy Bypass -File helper-status.ps1  (or: npm run helper:status)

$ErrorActionPreference = "Continue"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$ConfigDir = Join-Path $env:USERPROFILE ".kvm-ai-monitor"
$ConfigPath = Join-Path $ConfigDir "helper.json"
$AppDir = Join-Path $env:LOCALAPPDATA "kvm-ai-monitor"
$TaskName = "kvm-ai-monitor-helper"

Write-Host "== Config =="
$kvmHosts = @()
if (Test-Path $ConfigPath) {
    try {
        $config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        $targets = if ($config.targets) { @($config.targets) }
                   elseif ($config.kvmHost -and $config.deviceId) { @($config) }
                   else { @() }
        $kvmHosts = @($targets | Where-Object { $_.kvmHost -and $_.deviceId } | ForEach-Object { $_.kvmHost })
    } catch {
        $kvmHosts = @()
    }
    if ($kvmHosts.Count -gt 0) {
        Write-Host "present and valid: $ConfigPath (KVMs: $($kvmHosts -join ' '))"
    } else {
        Write-Host "present but invalid (no usable targets): $ConfigPath"
    }
} else {
    Write-Host "missing: $ConfigPath"
}

Write-Host ""
Write-Host "== DPAPI push secrets =="
# kvm_ai_push.py stores each secret DPAPI-encrypted at secrets\push-<host> (non [A-Za-z0-9._-]
# characters in the host replaced by "_").
foreach ($kvmHost in $kvmHosts) {
    $safe = $kvmHost -replace "[^A-Za-z0-9._-]", "_"
    if (Test-Path (Join-Path $ConfigDir (Join-Path "secrets" "push-$safe"))) {
        Write-Host "${kvmHost}: present"
    } else {
        Write-Host "${kvmHost}: not found"
    }
}
if ($kvmHosts.Count -eq 0) { Write-Host "not found" }

Write-Host ""
Write-Host "== Scheduled task ($TaskName) =="
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
if ($info) {
    Write-Host "last run:      $($info.LastRunTime)"
    Write-Host "next run:      $($info.NextRunTime)"
    switch ($info.LastTaskResult) {
        0        { Write-Host "last result:   0 (last push succeeded)" }
        267011   { Write-Host "last result:   267011 (task has not run yet)" }
        default  { Write-Host ("last result:   {0} (0x{0:X} - last push FAILED; check the KVM address and secret)" -f $info.LastTaskResult) }
    }
    Write-Host "note:          the task runs in the interactive session, so pushes pause while"
    Write-Host "               this user is signed out and resume at the next sign-in."
} else {
    Write-Host "not registered (run: npm run helper:install)"
}

Write-Host ""
Write-Host "== Installed files ($AppDir) =="
foreach ($name in "kvm_ai_push.py", "kvm-ai-claude-hook.cmd") {
    if (Test-Path (Join-Path $AppDir $name)) { Write-Host "${name}: present" }
    else { Write-Host "${name}: missing" }
}

Write-Host ""
Write-Host "== Payload that would be sent (print-payload) =="
. (Join-Path $PSScriptRoot "find-python.ps1")
$python = Find-Python
if (-not $python) {
    Write-Host "no usable Python 3 found (set KVM_PYTHON to a python.exe)"
    exit 1
}
$helperScript = Join-Path $AppDir "kvm_ai_push.py"
if (-not (Test-Path $helperScript)) { $helperScript = Join-Path $ProjectDir "helper\kvm_ai_push.py" }
& $python $helperScript print-payload
exit $LASTEXITCODE

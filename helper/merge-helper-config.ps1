# Merges one KVM target into helper.json so a single PC can push to several KVMs.
# Invoked by install-helper.ps1 and exercised directly by test/helper-config-merge.test.js
# (which is how the merge gets covered on Windows CI).
#
# Kept in PowerShell rather than a generated .py file: Set-Content defaults to UTF-16 under
# Windows PowerShell 5.1, and Python cannot parse UTF-16 source.
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$Kvm,
    [Parameter(Mandatory = $true)][string]$Device
)

$ErrorActionPreference = "Stop"

$targets = @()
if (Test-Path $ConfigPath) {
    try {
        $existing = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        if ($existing.targets) {
            $targets = @($existing.targets)
        } elseif ($existing.kvmHost -and $existing.deviceId) {
            # Pre-multi-KVM installs stored a single {kvmHost, deviceId} at the top level.
            $targets = @([pscustomobject]@{ kvmHost = $existing.kvmHost; deviceId = $existing.deviceId })
        }
    } catch {
        $targets = @()
    }
}
# Re-enrolling an already-known KVM replaces its entry (the device id may have changed).
$targets = @($targets | Where-Object { $_.kvmHost -ne $Kvm })
$targets += [pscustomobject]@{ kvmHost = $Kvm; deviceId = $Device }

# -Depth: PS 5.1 defaults to 2 and would stringify the target objects.
$json = ConvertTo-Json @{ targets = @($targets) } -Depth 5
# Must be BOM-less: both Python's json.load and Node's JSON.parse reject a leading BOM.
[IO.File]::WriteAllText($ConfigPath, $json + "`n", (New-Object Text.UTF8Encoding($false)))

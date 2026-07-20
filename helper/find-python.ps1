# Dot-sourced by install-helper.ps1 and helper-status.ps1.
#
# `python`/`python3` on PATH are usually the Microsoft Store alias stubs: they exist, so a
# plain Get-Command finds them, but they exit 9009 without running anything. Probe candidates
# and keep the first that actually executes. uv-managed interpreters are never on PATH.
# KVM_PYTHON overrides. (src/platform.js mirrors this logic for the Node entry points.)
function Find-Python {
    param([switch]$Windowed)

    $exe = if ($Windowed) { "pythonw.exe" } else { "python.exe" }
    $candidates = @()

    if ($env:KVM_PYTHON) {
        $candidates += if ($Windowed) { $env:KVM_PYTHON -replace "python\.exe$", "pythonw.exe" } else { $env:KVM_PYTHON }
    }

    $uvRoot = Join-Path $env:APPDATA "uv\python"
    if (Test-Path $uvRoot) {
        # Sort by parsed version, not by name: a string sort ranks "cpython-3.9" above
        # "cpython-3.14" and would pick the oldest interpreter installed.
        $candidates += Get-ChildItem $uvRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $version = New-Object Version(0, 0, 0)
                if ($_.Name -match "cpython-(\d+)\.(\d+)(?:\.(\d+))?") {
                    $patch = 0
                    if ($Matches[3]) { $patch = [int]$Matches[3] }
                    $version = New-Object Version([int]$Matches[1], [int]$Matches[2], $patch)
                }
                [pscustomobject]@{ Path = $_.FullName; Version = $version }
            } |
            Sort-Object Version -Descending |
            ForEach-Object { Join-Path $_.Path $exe }
    }

    $candidates += (Get-Command $exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    if (-not $Windowed) {
        $candidates += (Get-Command "python3.exe", "py.exe" -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if ($candidate -like "*\WindowsApps\*") { continue }   # Store alias stub
        if (-not (Test-Path $candidate)) { continue }
        # pythonw has no console to print to, so always probe with the console build.
        $probeExe = $candidate -replace "pythonw\.exe$", "python.exe"
        if (-not (Test-Path $probeExe)) { continue }
        try {
            $probe = & $probeExe -c "print(1)" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$probe".Trim() -eq "1") { return $candidate }
        } catch {
            continue
        }
    }
    return $null
}

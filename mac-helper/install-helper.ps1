# Windows enrollment for the KVM AI Monitor push helper.
# Usage: powershell -ExecutionPolicy Bypass -File install-helper.ps1 -Kvm <host> -Device <device-id> [-Update]
# The secret is encrypted with user-scoped Windows DPAPI by kvm_ai_push.py itself.
param(
    [string]$Kvm = "",
    [string]$Device = "",
    [switch]$Update,
    [switch]$SecretStdin
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
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

# --- locate a usable Python --------------------------------------------------------------
#
# `python`/`python3` on PATH are usually the Microsoft Store alias stubs: they exist, so a
# plain Get-Command finds them, but they exit 9009 without running anything. Probe candidates
# and keep the first that actually executes. uv-managed interpreters are never on PATH.
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

$python = Find-Python
if (-not $python) {
    Write-Error ("No usable Python 3 found. `python`/`python3` on PATH are usually Microsoft Store " +
                 "stubs that do not run. Install one (https://www.python.org/downloads/ or " +
                 "``uv python install``), or set KVM_PYTHON to a python.exe.")
}
$pythonw = Find-Python -Windowed
if (-not $pythonw) { $pythonw = $python }

# --- install the helper ------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item (Join-Path $ProjectDir "mac-helper\kvm_ai_push.py") (Join-Path $AppDir "kvm_ai_push.py") -Force
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$helperPath = Join-Path $AppDir "kvm_ai_push.py"

# The shipped hook calls a bare `pythonw`, which on most machines is the Store stub or absent;
# the hook swallows all output and exits 0, so that failure would be silent. Bake in the
# interpreter we just resolved.
$hookSource = Get-Content (Join-Path $ProjectDir "mac-helper\kvm-ai-claude-hook.cmd") -Raw
$hookSource = $hookSource -replace 'start /b "" pythonw ', ('start /b "" "' + $pythonw + '" ')
[IO.File]::WriteAllText((Join-Path $AppDir "kvm-ai-claude-hook.cmd"), $hookSource,
                        (New-Object Text.UTF8Encoding($false)))

if (-not $Update) {
    if ($SecretStdin) {
        $Secret = [Console]::In.ReadLine()
    } else {
        $SecureSecret = Read-Host "One-time device secret from the KVM AI Usage page" -AsSecureString
        $Secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureSecret))
    }
    if (-not $Secret) { Write-Error "Secret cannot be empty." }

    # Written through .NET rather than `$Secret | & $python`: a PowerShell pipe into a native
    # process prefixes a UTF-8 BOM (regardless of $OutputEncoding), which corrupts the secret.
    # ArgumentList/StandardInputEncoding are .NET Core APIs and absent from Windows PowerShell,
    # so quote manually and write bytes to the raw stream.
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = '"{0}" store-secret --kvm "{1}"' -f $helperPath, $Kvm
    $psi.RedirectStandardInput = $true
    $psi.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($psi)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Secret + "`n")
    $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $process.StandardInput.BaseStream.Flush()
    $process.StandardInput.Close()
    $process.WaitForExit()
    Remove-Variable Secret, bytes
    if ($process.ExitCode -ne 0) { Write-Error "Storing the secret failed." }

    # Merge this KVM into the target list so one PC can push to several KVMs. Done in
    # PowerShell rather than a generated .py file: Set-Content defaults to UTF-16 here, and
    # Python cannot parse UTF-16 source.
    $configPath = Join-Path $ConfigDir "helper.json"
    $targets = @()
    if (Test-Path $configPath) {
        try {
            $existing = Get-Content $configPath -Raw | ConvertFrom-Json
            if ($existing.targets) {
                $targets = @($existing.targets | Where-Object { $_.kvmHost -ne $Kvm })
            } elseif ($existing.kvmHost -and $existing.deviceId) {
                $targets = @([pscustomobject]@{ kvmHost = $existing.kvmHost; deviceId = $existing.deviceId })
            }
        } catch {
            $targets = @()
        }
    }
    $targets += [pscustomobject]@{ kvmHost = $Kvm; deviceId = $Device }
    # -Depth: PS 5.1 defaults to 2 and would stringify the target objects.
    $json = ConvertTo-Json @{ targets = @($targets) } -Depth 5
    # Must be BOM-less: both Python's json.load and Node's JSON.parse reject a leading BOM.
    [IO.File]::WriteAllText($configPath, $json + "`n", (New-Object Text.UTF8Encoding($false)))
}

# --- schedule the per-minute push ----------------------------------------------------------
#
# Registered from XML rather than `schtasks /TR "..."`: the XML keeps the command and its
# arguments as separate elements, so paths containing spaces need no shell quoting, and a
# <Repetition> with no <Duration> means "forever". schtasks.exe is used instead of
# Register-ScheduledTask because the cmdlet needs elevation (0x80070005) even for a task that
# only runs as the current user.

function ConvertTo-XmlText([string]$value) {
    $value.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
}

$user = ConvertTo-XmlText "$env:USERDOMAIN\$env:USERNAME"
# One minute in the past, so the first repetition is due immediately rather than in a minute.
$startBoundary = (Get-Date).AddMinutes(-1).ToString("yyyy-MM-ddTHH:mm:ss")
$commandXml = ConvertTo-XmlText $pythonw
$argsXml = ConvertTo-XmlText ('"{0}" send-usage' -f $helperPath)
$workDirXml = ConvertTo-XmlText $AppDir

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Pushes this PC's Claude usage to the GL.iNet Comet KVM.</Description>
  </RegistrationInfo>
  <Triggers>
    <!-- A LogonTrigger alone is not enough: it fires only at the moment of logon, so a task
         installed during an existing session never starts (NextRunTime stays empty until the
         next sign-in). The TimeTrigger with a StartBoundary in the past begins repeating
         immediately; the LogonTrigger then re-arms it after a reboot. -->
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>$startBoundary</StartBoundary>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$commandXml</Command>
      <Arguments>$argsXml</Arguments>
      <WorkingDirectory>$workDirXml</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path $env:TEMP "kvm-ai-monitor-task.xml"
try {
    # schtasks /xml requires UTF-16LE.
    [IO.File]::WriteAllText($xmlPath, $taskXml, [Text.Encoding]::Unicode)
    $output = & schtasks.exe /Create /F /TN $TaskName /XML $xmlPath 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Error "Registering the scheduled task failed: $output" }
} finally {
    Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue
}
Write-Host "Scheduled task '$TaskName' created (every minute)."

Write-Host "Running an initial usage push..."
& $python $helperPath send-usage
if ($LASTEXITCODE -eq 0) {
    Write-Host "Initial push succeeded."
} else {
    Write-Warning "Initial push failed; check the KVM address and secret."
}

Write-Host ""
Write-Host "To also send exact working/idle events from Claude Code on this device, run:"
Write-Host "  & `"$python`" `"$(Join-Path $ProjectDir 'mac-helper\claude_hooks.py')`" install `"$(Join-Path $AppDir 'kvm-ai-claude-hook.cmd')`""

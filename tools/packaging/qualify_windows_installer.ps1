<#
.SYNOPSIS
    Automated Windows NSIS installer and installed-app qualification script for PICO.
.DESCRIPTION
    Executes end-to-end packaging qualification against a built PICO NSIS setup executable:
    1. Silent current-user installation (`<setup.exe> /S`).
    2. Installed payload & Add/Remove Programs metadata verification.
    3. Runtime launch from installed path, verifying bundled Python Core, `/health`,
       per-user `%LOCALAPPDATA%\PICO\data\pico.db` creation, zero idle WebView2 processes,
       and ADR-0001 companion idle RAM `< 100 MiB`.
    4. Clean companion termination & Job Object child Core exit check.
    5. Narrow uninstall HKCU Run `"PICO"` cleanup without touching unrelated Run entries
       or `%LOCALAPPDATA%\PICO\data` user database persistence.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$InstallerPath,

    [Parameter(Mandatory = $false)]
    [string]$ReportPath = "frontend/src-tauri/staged/installer-qualification-report.json",

    [Parameter(Mandatory = $false)]
    [switch]$PreserveUserData = $true
)

$ErrorActionPreference = "Stop"
$currentStep = "init"

function Resolve-InstallerExe {
    param([string]$ExplicitPath)
    if ($ExplicitPath -and (Test-Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }
    $found = Get-ChildItem -Path "frontend/src-tauri/target" -Recurse -Filter "*-setup.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $found) {
        throw "No NSIS *-setup.exe installer found under frontend/src-tauri/target"
    }
    return $found.FullName
}

function Get-PicoUninstallEntry {
    $regRoots = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($root in $regRoots) {
        $entry = Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -eq "PICO" } |
            Select-Object -First 1
        if ($entry) {
            return $entry
        }
    }
    return $null
}

try {

$currentStep = "resolve_installer"
$resolvedInstaller = Resolve-InstallerExe -ExplicitPath $InstallerPath
Write-Host "Qualifying NSIS installer: $resolvedInstaller"

$installerInfo = Get-Item $resolvedInstaller
$installerSha256 = (Get-FileHash -Path $resolvedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
$signature = Get-AuthenticodeSignature -FilePath $resolvedInstaller

# 1. Silent current-user installation
$currentStep = "silent_install"
Write-Host "[1/6] Running silent NSIS installation (/S)..."
$installProc = Start-Process -FilePath $resolvedInstaller -ArgumentList "/S" -PassThru -Wait
if ($installProc.ExitCode -ne 0) {
    throw "Silent NSIS installer failed with exit code $($installProc.ExitCode)"
}

Start-Sleep -Seconds 1
$uninstallEntry = Get-PicoUninstallEntry
$defaultInstallDir = Join-Path $env:LOCALAPPDATA "PICO"
$installDir = if ($uninstallEntry -and $uninstallEntry.InstallLocation) {
    $uninstallEntry.InstallLocation.TrimEnd('\')
} elseif (Test-Path $defaultInstallDir) {
    $defaultInstallDir
} else {
    throw "Could not determine PICO InstallLocation after silent install (checked uninstall registry and $defaultInstallDir)"
}

Write-Host "Installed location: $installDir"

# Locate main executable and uninstaller
$picoExe = Get-ChildItem -Path $installDir -Filter "*.exe" -File |
    Where-Object { $_.Name -notlike "uninstall*" } |
    Select-Object -First 1
if (-not $picoExe) {
    $topEntries = (Get-ChildItem -Path $installDir -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name) -join ", "
    throw "Installed desktop executable not found in $installDir (entries: $topEntries)"
}

$uninstallExe = Join-Path $installDir "uninstall.exe"
if (-not (Test-Path $uninstallExe)) {
    throw "NSIS uninstaller not found at $uninstallExe"
}

# 2. Verify installed payload & hygiene
$currentStep = "verify_payload_hygiene"
Write-Host "[2/6] Verifying installed resources payload and hygiene..."
$coreMain = Join-Path $installDir "resources\core\pico\main.py"
$bundledPython = Join-Path $installDir "resources\python\python.exe"
if (-not (Test-Path $coreMain)) {
    $treeSample = (Get-ChildItem -Path $installDir -Depth 2 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName | Select-Object -First 25) -join "; "
    throw "Missing bundled Core entrypoint: $coreMain (tree sample: $treeSample)"
}
if (-not (Test-Path $bundledPython)) {
    throw "Missing bundled Python runtime: $bundledPython"
}

$forbiddenHits = @()
Get-ChildItem -Path (Join-Path $installDir "resources") -Recurse -Force | ForEach-Object {
    $nameLower = $_.Name.ToLowerInvariant()
    if ($nameLower -in @(".env", ".venv", "venv", "pico.db", "pytest") -or
        $nameLower -like ".env.*" -or
        $nameLower -like "*.db" -or
        $nameLower -like "*.sqlite*") {
        $forbiddenHits += $_.FullName
    }
}
if ($forbiddenHits.Count -gt 0) {
    throw "Forbidden dev/secret/database artifacts found in installed resources:`n$($forbiddenHits -join "`n")"
}

# 3. Launch installed companion app and verify Core + bundled Python + DB + ADR-0001 idle RAM
$currentStep = "launch_and_health"
Write-Host "[3/6] Launching installed PICO executable from $installDir..."
$envBackupDbUrl = $env:DATABASE_URL
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

$appProc = Start-Process -FilePath $picoExe.FullName -WorkingDirectory $installDir -PassThru
$corePid = $null
$healthOk = $false
$bundledPythonUsed = $false
$idleWv2Count = -1
$companionWorkingSetBytes = 0L
$companionPrivateBytes = 0L
$coreWorkingSetBytes = 0L

try {
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($appProc.HasExited) {
            throw "Installed PICO companion exited prematurely with code $($appProc.ExitCode)"
        }
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) {
                $healthOk = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }

    if (-not $healthOk) {
        throw "Installed PICO Core failed to respond HTTP 200 on http://127.0.0.1:8000/health within 30s"
    }

    # Allow idle stabilization briefly
    Start-Sleep -Seconds 2

    $allProcs = Get-CimInstance Win32_Process
    $appTreeIds = [System.Collections.Generic.HashSet[int]]::new()
    [void]$appTreeIds.Add([int]$appProc.Id)

    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($p in $allProcs) {
            $pidInt = [int]$p.ProcessId
            $ppidInt = [int]$p.ParentProcessId
            if ($appTreeIds.Contains($ppidInt) -and -not $appTreeIds.Contains($pidInt)) {
                [void]$appTreeIds.Add($pidInt)
                $changed = $true
            }
        }
    }

    $pythonChildren = $allProcs | Where-Object {
        $appTreeIds.Contains([int]$_.ProcessId) -and $_.Name -ieq "python.exe"
    }
    if (-not $pythonChildren) {
        throw "No supervised python.exe child process found under installed companion PID $($appProc.Id)"
    }
    $coreProcCim = $pythonChildren | Select-Object -First 1
    $corePid = [int]$coreProcCim.ProcessId
    $expectedPythonCanonical = ((Resolve-Path $bundledPython).Path) -replace '^\\\\\?\\', ''
    $actualPythonPath = ($coreProcCim.ExecutablePath) -replace '^\\\\\?\\', ''
    if (-not $actualPythonPath -or ($actualPythonPath -ine $expectedPythonCanonical)) {
        throw "Supervised Core used unexpected Python executable '$actualPythonPath' instead of bundled '$expectedPythonCanonical'"
    }
    $bundledPythonUsed = $true

    $wv2Procs = $allProcs | Where-Object {
        $_.Name -ieq "msedgewebview2.exe" -and (
            $appTreeIds.Contains([int]$_.ProcessId) -or
            ($_.CommandLine -and $_.CommandLine -match "com\.pico\.desktop")
        )
    }
    $idleWv2Count = @($wv2Procs).Count
    if ($idleWv2Count -ne 0) {
        throw "Expected 0 PICO-owned msedgewebview2.exe processes at idle, found $idleWv2Count"
    }

    # Measure companion process tree excluding separate Core python.exe (per ADR 0001)
    foreach ($pidInt in $appTreeIds) {
        $liveProc = Get-Process -Id $pidInt -ErrorAction SilentlyContinue
        if (-not $liveProc) { continue }
        if ($pidInt -eq $corePid) {
            $coreWorkingSetBytes += [int64]$liveProc.WorkingSet64
        } else {
            $companionWorkingSetBytes += [int64]$liveProc.WorkingSet64
            $companionPrivateBytes += [int64]$liveProc.PrivateMemorySize64
        }
    }

    $companionWsMiB = [Math]::Round($companionWorkingSetBytes / 1MB, 2)
    Write-Host "Companion idle Working Set: $companionWsMiB MiB (Private: $([Math]::Round($companionPrivateBytes / 1MB, 2)) MiB)"
    if ($companionWsMiB -ge 100.0) {
        throw "ADR-0001 idle RAM budget exceeded: $companionWsMiB MiB >= 100 MiB"
    }

    # 4. Verify per-user DB path creation & zero DB inside resources/core
    Write-Host "[4/6] Verifying per-user database location in %LOCALAPPDATA%\PICO\data..."
    $userDataDir = Join-Path $env:LOCALAPPDATA "PICO\data"
    $userDbPath = Join-Path $userDataDir "pico.db"
    if (-not (Test-Path $userDbPath)) {
        throw "Expected per-user SQLite DB not created at $userDbPath"
    }
    if (Test-Path (Join-Path $installDir "resources\core\pico.db")) {
        throw "SQLite database unexpectedly written inside packaged resources/core directory"
    }
    $sentinelPath = Join-Path $userDataDir "qualification_sentinel.txt"
    "persist-across-uninstall" | Set-Content -Path $sentinelPath -Encoding UTF8
} finally {
    if ($envBackupDbUrl) {
        $env:DATABASE_URL = $envBackupDbUrl
    }
    Write-Host "[5/6] Terminating installed companion and verifying Job Object Core cleanup..."
    if ($appProc -and -not $appProc.HasExited) {
        Stop-Process -Id $appProc.Id -Force -ErrorAction SilentlyContinue
        $appProc.WaitForExit(10000) | Out-Null
    }
    if ($corePid) {
        Start-Sleep -Seconds 2
        $lingeringCore = Get-Process -Id $corePid -ErrorAction SilentlyContinue
        if ($lingeringCore) {
            Stop-Process -Id $corePid -Force -ErrorAction SilentlyContinue
            throw "Supervised Core PID $corePid lingered after companion process termination"
        }
    }
}

# 6. Auto-start Run hook & silent uninstall verification
$currentStep = "uninstall_and_cleanup"
Write-Host "[6/6] Verifying narrow HKCU Run cleanup and user data preservation on uninstall..."
$runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (-not (Test-Path $runKeyPath)) {
    New-Item -Path $runKeyPath -Force | Out-Null
}
$unrelatedValueName = "PicoQualificationKeepUnrelated"
$unrelatedValueData = '"C:\Windows\System32\cmd.exe" /c exit 0'
New-ItemProperty -Path $runKeyPath -Name "PICO" -Value "`"$($picoExe.FullName)`"" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $runKeyPath -Name $unrelatedValueName -Value $unrelatedValueData -PropertyType String -Force | Out-Null

# Silent NSIS uninstallation. Copy uninstall.exe or invoke with _?= when waiting synchronously on NSIS uninstaller.
$uninstProc = Start-Process -FilePath $uninstallExe -ArgumentList "/S", "_?=$installDir" -PassThru -Wait
if ($uninstProc.ExitCode -ne 0) {
    throw "Silent NSIS uninstaller failed with exit code $($uninstProc.ExitCode)"
}
Start-Sleep -Seconds 1

$picoRunStillExists = $null -ne (Get-ItemPropertyValue -Path $runKeyPath -Name "PICO" -ErrorAction SilentlyContinue)
$unrelatedActual = Get-ItemPropertyValue -Path $runKeyPath -Name $unrelatedValueName -ErrorAction SilentlyContinue
$unrelatedStillExists = $unrelatedActual -eq $unrelatedValueData
Remove-ItemProperty -Path $runKeyPath -Name $unrelatedValueName -ErrorAction SilentlyContinue

# Clean up uninstaller exe itself if _?= prevented self-deletion
Remove-Item -Path $uninstallExe -Force -ErrorAction SilentlyContinue

$exeRemovedAfterUninstall = -not (Test-Path $picoExe.FullName)
$userDbPreservedAfterUninstall = Test-Path (Join-Path $env:LOCALAPPDATA "PICO\data\pico.db")
$sentinelPreservedAfterUninstall = Test-Path (Join-Path $env:LOCALAPPDATA "PICO\data\qualification_sentinel.txt")
Remove-Item -Path (Join-Path $env:LOCALAPPDATA "PICO\data\qualification_sentinel.txt") -Force -ErrorAction SilentlyContinue

if ($picoRunStillExists) { throw "Uninstall failed to remove HKCU Run entry 'PICO'" }
if (-not $unrelatedStillExists) { throw "Uninstall modified or removed unrelated HKCU Run entry" }
if (-not $exeRemovedAfterUninstall) { throw "Installed executable still present after uninstall: $($picoExe.FullName)" }
if (-not $userDbPreservedAfterUninstall -or -not $sentinelPreservedAfterUninstall) {
    throw "Uninstall unexpectedly deleted user data under %LOCALAPPDATA%\PICO\data"
}

$report = [ordered]@{
    installer_path = $resolvedInstaller
    installer_size_bytes = $installerInfo.Length
    installer_sha256 = $installerSha256
    authenticode_status = $signature.Status.ToString()
    install_dir = $installDir
    uninstall_display_name = if ($uninstallEntry) { $uninstallEntry.DisplayName } else { $null }
    uninstall_publisher = if ($uninstallEntry) { $uninstallEntry.Publisher } else { $null }
    uninstall_version = if ($uninstallEntry) { $uninstallEntry.DisplayVersion } else { $null }
    health_check_passed = $healthOk
    bundled_python_verified = $bundledPythonUsed
    idle_webview2_process_count = $idleWv2Count
    companion_idle_working_set_mib = [Math]::Round($companionWorkingSetBytes / 1MB, 2)
    companion_idle_private_mib = [Math]::Round($companionPrivateBytes / 1MB, 2)
    core_working_set_mib = [Math]::Round($coreWorkingSetBytes / 1MB, 2)
    autostart_pico_removed_on_uninstall = (-not $picoRunStillExists)
    autostart_unrelated_preserved = $unrelatedStillExists
    executable_removed_on_uninstall = $exeRemovedAfterUninstall
    user_db_preserved_on_uninstall = ($userDbPreservedAfterUninstall -and $sentinelPreservedAfterUninstall)
}

$reportDir = Split-Path -Parent $ReportPath
if ($reportDir -and -not (Test-Path $reportDir)) {
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
}
$report | ConvertTo-Json -Depth 4 | Set-Content -Path $ReportPath -Encoding UTF8
Write-Host "Qualification report written to $ReportPath"
$report | ConvertTo-Json -Depth 4

} catch {
    $errMsg = $_.Exception.Message
    $errLine = $_.InvocationInfo.ScriptLineNumber
    Write-Host "::error title=Installer Qualification Failed ($currentStep at line $errLine)::$errMsg"
    throw
}

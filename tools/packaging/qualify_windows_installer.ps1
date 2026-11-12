<#
.SYNOPSIS
    Automated Windows NSIS installer, upgrade, and 60s+ idle qualification script for PICO.
.DESCRIPTION
    Executes end-to-end packaging qualification against built PICO NSIS setup executables:
    1. Silent current-user installation (`<setup.exe> /S`).
    2. Installed payload hygiene & Add/Remove Programs metadata verification.
    3. Runtime launch from installed path, verifying bundled CPython 3.13 Core, `/health`,
       per-user `%LOCALAPPDATA%\PICO\data\pico.db` creation + SQLite integrity,
       and multi-snapshot synchronized idle RAM (< 100 MiB ADR-0001 budget, 0 idle WebView2).
    4. Optional 2-version upgrade qualification (`0.1.0 -> 0.1.1`), verifying DB & sentinel
       preservation, HKCU Run `"PICO"` auto-start preservation across upgrade AND `/UPDATE`
       uninstaller execution, plus full 60s+ idle RAM qualification on the upgraded binary.
    5. Clean companion termination & Job Object child Core exit check.
    6. Genuine uninstall (`uninstall.exe /S _?=...`) verifying narrow HKCU Run `"PICO"` removal
       without touching unrelated Run entries or `%LOCALAPPDATA%\PICO\data`.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$InstallerPath,

    [Parameter(Mandatory = $false)]
    [string]$UpgradeInstallerPath,

    [Parameter(Mandatory = $false)]
    [string]$ReportPath = "frontend/src-tauri/staged/installer-qualification-report.json",

    [Parameter(Mandatory = $false)]
    [int]$IdleWindowSeconds = 60,

    [Parameter(Mandatory = $false)]
    [int]$IdleSampleIntervalSeconds = 10,

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

function Resolve-InstalledLayout {
    $uninstallEntry = Get-PicoUninstallEntry
    $defaultInstallDir = Join-Path $env:LOCALAPPDATA "PICO"
    $installDir = if ($uninstallEntry -and $uninstallEntry.InstallLocation) {
        ($uninstallEntry.InstallLocation.ToString()).Trim().Trim('"').TrimEnd('\')
    } elseif (Test-Path $defaultInstallDir) {
        $defaultInstallDir
    } else {
        throw "Could not determine PICO InstallLocation after silent install (checked uninstall registry and $defaultInstallDir)"
    }

    if (-not (Test-Path -LiteralPath $installDir)) {
        throw "Resolved InstallLocation '$installDir' does not exist on disk"
    }

    $picoExe = Get-ChildItem -LiteralPath $installDir -Filter "*.exe" |
        Where-Object { -not $_.PSIsContainer -and $_.Name -notlike "uninstall*" } |
        Select-Object -First 1
    if (-not $picoExe) {
        $topEntries = (Get-ChildItem -Path $installDir -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name) -join ", "
        throw "Installed desktop executable not found in $installDir (entries: $topEntries)"
    }

    $uninstallExe = Join-Path $installDir "uninstall.exe"
    if (-not (Test-Path $uninstallExe)) {
        throw "NSIS uninstaller not found at $uninstallExe"
    }

    return [pscustomobject]@{
        UninstallEntry = $uninstallEntry
        InstallDir     = $installDir
        PicoExe        = $picoExe
        UninstallExe   = $uninstallExe
    }
}

function Assert-InstalledPayloadHygiene {
    param([string]$InstallDir)

    $coreMain = Join-Path $InstallDir "resources\core\pico\main.py"
    $bundledPython = Join-Path $InstallDir "resources\python\python.exe"
    $pythonDll = Join-Path $InstallDir "resources\python\python314.dll"
    $pythonPth = Join-Path $InstallDir "resources\python\python314._pth"

    foreach ($reqFile in @($coreMain, $bundledPython, $pythonDll, $pythonPth)) {
        if (-not (Test-Path $reqFile)) {
            $treeSample = (Get-ChildItem -Path $InstallDir -Depth 2 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName | Select-Object -First 25) -join "; "
            throw "Missing required installed payload artifact: $reqFile (tree sample: $treeSample)"
        }
    }

    $forbiddenHits = @()
    Get-ChildItem -Path (Join-Path $InstallDir "resources") -Recurse -Force | ForEach-Object {
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

    return [pscustomobject]@{
        CoreMain      = $coreMain
        BundledPython = $bundledPython
    }
}

function Get-ProcessTreeMemorySnapshot {
    param([int]$RootPid)

    $allProcs = Get-CimInstance Win32_Process
    $appTreeIds = [System.Collections.Generic.HashSet[int]]::new()
    [void]$appTreeIds.Add($RootPid)

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

    $pythonChildren = @($allProcs | Where-Object {
        $appTreeIds.Contains([int]$_.ProcessId) -and $_.Name -ieq "python.exe"
    })
    if ($pythonChildren.Count -eq 0) {
        throw "No supervised python.exe child process found under installed companion PID $RootPid"
    }
    $coreProcCim = $pythonChildren | Select-Object -First 1
    $corePid = [int]$coreProcCim.ProcessId

    $wv2Procs = @($allProcs | Where-Object {
        $_.Name -ieq "msedgewebview2.exe" -and (
            $appTreeIds.Contains([int]$_.ProcessId) -or
            ($_.CommandLine -and $_.CommandLine -match "com\.pico\.desktop")
        )
    })

    $perfByPid = @{}
    try {
        $perfRows = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -ErrorAction Stop
        foreach ($row in $perfRows) {
            if ($null -ne $row.IDProcess) {
                $perfByPid[[int]$row.IDProcess] = $row
            }
        }
    } catch {
        # Fallback cleanly if perf counters are temporarily unavailable
    }

    $companionWs = 0L
    $companionPrivWs = 0L
    $companionPrivBytes = 0L
    $coreWs = 0L
    $companionProcCount = 0

    foreach ($pidInt in $appTreeIds) {
        $liveProc = Get-Process -Id $pidInt -ErrorAction SilentlyContinue
        if (-not $liveProc) { continue }
        $ws = [int64]$liveProc.WorkingSet64
        $privBytes = [int64]$liveProc.PrivateMemorySize64
        $privWs = if ($perfByPid.ContainsKey($pidInt) -and $null -ne $perfByPid[$pidInt].WorkingSetPrivate) {
            [int64]$perfByPid[$pidInt].WorkingSetPrivate
        } else {
            0L
        }

        if ($pidInt -eq $corePid) {
            $coreWs += $ws
        } else {
            $companionProcCount += 1
            $companionWs += $ws
            $companionPrivWs += $privWs
            $companionPrivBytes += $privBytes
        }
    }

    return [pscustomobject]@{
        CorePid                     = $corePid
        CoreExePath                 = $coreProcCim.ExecutablePath
        WebView2Count               = $wv2Procs.Count
        CompanionProcessCount       = $companionProcCount
        CompanionWorkingSetMiB      = [Math]::Round($companionWs / 1MB, 2)
        CompanionPrivateWsMiB       = [Math]::Round($companionPrivWs / 1MB, 2)
        CompanionPrivateBytesMiB    = [Math]::Round($companionPrivBytes / 1MB, 2)
        CoreWorkingSetMiB           = [Math]::Round($coreWs / 1MB, 2)
    }
}

function Stop-CompanionAndVerifyCoreExit {
    param(
        [System.Diagnostics.Process]$AppProcess,
        [Nullable[int]]$CorePid
    )
    if ($AppProcess -and -not $AppProcess.HasExited) {
        Stop-Process -Id $AppProcess.Id -Force -ErrorAction SilentlyContinue
        $AppProcess.WaitForExit(10000) | Out-Null
    }
    if ($CorePid) {
        Start-Sleep -Seconds 2
        $lingeringCore = Get-Process -Id $CorePid -ErrorAction SilentlyContinue
        if ($lingeringCore) {
            Stop-Process -Id $CorePid -Force -ErrorAction SilentlyContinue
            throw "Supervised Core PID $CorePid lingered after companion process termination"
        }
    }
}

function Invoke-InstalledCompanionCheck {
    param(
        [string]$InstallDir,
        [System.IO.FileInfo]$PicoExe,
        [string]$BundledPython,
        [int]$SampleDurationSeconds,
        [int]$SampleIntervalSeconds
    )

    $envBackupDbUrl = $env:DATABASE_URL
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

    $appProc = Start-Process -FilePath $PicoExe.FullName -WorkingDirectory $InstallDir -PassThru
    $corePid = $null
    $healthOk = $false
    $bundledPythonUsed = $false
    $samples = @()

    try {
        $deadline = (Get-Date).AddSeconds(60)
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
            throw "Installed PICO Core failed to respond HTTP 200 on http://127.0.0.1:8000/health within 45s"
        }

        Start-Sleep -Seconds 2
        $expectedPythonCanonical = ((Resolve-Path $BundledPython).Path) -replace '^\\\\\?\\', ''

        $sampleCount = if ($SampleDurationSeconds -le 0) {
            1
        } else {
            [Math]::Floor($SampleDurationSeconds / [Math]::Max(1, $SampleIntervalSeconds)) + 1
        }

        $windowStart = Get-Date
        for ($i = 0; $i -lt $sampleCount; $i++) {
            if ($appProc.HasExited) {
                throw "Installed PICO companion exited unexpectedly during idle sampling (exit code $($appProc.ExitCode))"
            }

            $snap = Get-ProcessTreeMemorySnapshot -RootPid $appProc.Id
            $corePid = $snap.CorePid
            $actualPythonPath = ($snap.CoreExePath) -replace '^\\\\\?\\', ''
            if (-not $actualPythonPath -or ($actualPythonPath -ine $expectedPythonCanonical)) {
                throw "Supervised Core used unexpected Python executable '$actualPythonPath' instead of bundled '$expectedPythonCanonical'"
            }
            $bundledPythonUsed = $true

            if ($snap.WebView2Count -ne 0) {
                throw "Expected 0 PICO-owned msedgewebview2.exe processes at idle (sample $i), found $($snap.WebView2Count)"
            }
            if ($snap.CompanionWorkingSetMiB -ge 100.0) {
                throw "ADR-0001 idle RAM budget exceeded at sample $($i): $($snap.CompanionWorkingSetMiB) MiB >= 100 MiB"
            }

            $elapsedSec = [Math]::Round(((Get-Date) - $windowStart).TotalSeconds, 1)
            $samples += [ordered]@{
                elapsed_seconds              = $elapsedSec
                companion_process_count      = $snap.CompanionProcessCount
                companion_working_set_mib    = $snap.CompanionWorkingSetMiB
                companion_private_ws_mib     = $snap.CompanionPrivateWsMiB
                companion_private_bytes_mib  = $snap.CompanionPrivateBytesMiB
                core_working_set_mib         = $snap.CoreWorkingSetMiB
                idle_webview2_count          = $snap.WebView2Count
            }

            Write-Host ("  [Idle t={0,5}s] Companion WS={1} MiB (PrivWS={2} MiB, PrivBytes={3} MiB) | Core WS={4} MiB | WV2={5}" -f `
                $elapsedSec, $snap.CompanionWorkingSetMiB, $snap.CompanionPrivateWsMiB, $snap.CompanionPrivateBytesMiB, $snap.CoreWorkingSetMiB, $snap.WebView2Count)

            if ($i -lt ($sampleCount - 1)) {
                Start-Sleep -Seconds $SampleIntervalSeconds
            }
        }

        $userDataDir = Join-Path $env:LOCALAPPDATA "PICO\data"
        $userDbPath = Join-Path $userDataDir "pico.db"
        if (-not (Test-Path $userDbPath)) {
            throw "Expected per-user SQLite DB not created at $userDbPath"
        }
        if (Test-Path (Join-Path $InstallDir "resources\core\pico.db")) {
            throw "SQLite database unexpectedly written inside packaged resources/core directory"
        }

        $dbCheckScript = Join-Path ([System.IO.Path]::GetTempPath()) ("pico_db_check_" + [guid]::NewGuid().ToString("N") + ".py")
        $dbPyCode = @(
            "import sqlite3, sys"
            "conn = sqlite3.connect(sys.argv[1])"
            "chk = conn.execute('PRAGMA integrity_check').fetchone()[0]"
            "tables = len(conn.execute('SELECT name FROM sqlite_master WHERE type = ?', ('table',)).fetchall())"
            "conn.close()"
            "print(f'{chk}|{tables}')"
        ) -join "`n"
        [System.IO.File]::WriteAllText($dbCheckScript, $dbPyCode, (New-Object System.Text.UTF8Encoding($false)))
        try {
            $dbCheckOut = (& $BundledPython -B $dbCheckScript $userDbPath 2>&1) -join "`n"
            $dbExit = $LASTEXITCODE
        } finally {
            Remove-Item -Path $dbCheckScript -Force -ErrorAction SilentlyContinue
        }
        if ($dbExit -ne 0 -or -not ($dbCheckOut -match "^ok\|\d+")) {
            throw "SQLite integrity_check failed for $userDbPath (exit=${dbExit}, out='${dbCheckOut}')"
        }

        $bundledPythonVersion = ((& $BundledPython -B -c "import sys; print(sys.version.split()[0])" 2>&1) -join "").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $bundledPythonVersion) {
            throw "Failed querying bundled python version from $BundledPython"
        }

        $wsValues = @($samples | ForEach-Object { [double]$_.companion_working_set_mib })
        $privWsValues = @($samples | ForEach-Object { [double]$_.companion_private_ws_mib })
        $privBytesValues = @($samples | ForEach-Object { [double]$_.companion_private_bytes_mib })
        $coreWsValues = @($samples | ForEach-Object { [double]$_.core_working_set_mib })

        return [ordered]@{
            health_check_passed         = $healthOk
            bundled_python_verified     = $bundledPythonUsed
            bundled_python_version      = $bundledPythonVersion
            db_integrity_check          = ($dbCheckOut -join "")
            sample_count                = $samples.Count
            measured_window_seconds     = $samples[-1].elapsed_seconds
            companion_ws_min_mib        = [Math]::Round(($wsValues | Measure-Object -Minimum).Minimum, 2)
            companion_ws_avg_mib        = [Math]::Round(($wsValues | Measure-Object -Average).Average, 2)
            companion_ws_max_mib        = [Math]::Round(($wsValues | Measure-Object -Maximum).Maximum, 2)
            companion_priv_ws_avg_mib   = [Math]::Round(($privWsValues | Measure-Object -Average).Average, 2)
            companion_priv_ws_max_mib   = [Math]::Round(($privWsValues | Measure-Object -Maximum).Maximum, 2)
            companion_priv_bytes_avg_mib = [Math]::Round(($privBytesValues | Measure-Object -Average).Average, 2)
            companion_priv_bytes_max_mib = [Math]::Round(($privBytesValues | Measure-Object -Maximum).Maximum, 2)
            core_ws_avg_mib             = [Math]::Round(($coreWsValues | Measure-Object -Average).Average, 2)
            core_ws_max_mib             = [Math]::Round(($coreWsValues | Measure-Object -Maximum).Maximum, 2)
            idle_webview2_max_count     = 0
            samples                     = $samples
        }
    } finally {
        if ($envBackupDbUrl) {
            $env:DATABASE_URL = $envBackupDbUrl
        }
        Stop-CompanionAndVerifyCoreExit -AppProcess $appProc -CorePid $corePid
    }
}

try {
    $currentStep = "resolve_installer"
    $resolvedInstaller = Resolve-InstallerExe -ExplicitPath $InstallerPath
    Write-Host "Qualifying baseline NSIS installer: $resolvedInstaller"

    $installerInfo = Get-Item $resolvedInstaller
    $installerSha256 = (Get-FileHash -Path $resolvedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    $signature = Get-AuthenticodeSignature -FilePath $resolvedInstaller

    $resolvedUpgradeInstaller = $null
    if ($UpgradeInstallerPath) {
        if (-not (Test-Path $UpgradeInstallerPath)) {
            throw "Provided UpgradeInstallerPath does not exist: $UpgradeInstallerPath"
        }
        $resolvedUpgradeInstaller = (Resolve-Path $UpgradeInstallerPath).Path
        Write-Host "Qualifying upgrade NSIS installer: $resolvedUpgradeInstaller"
    }

    # 1. Clean baseline installation
    $currentStep = "silent_install_baseline"
    Write-Host "[1] Running silent baseline NSIS installation (/S)..."
    $installProc = Start-Process -FilePath $resolvedInstaller -ArgumentList "/S" -PassThru -Wait
    if ($installProc.ExitCode -ne 0) {
        throw "Silent NSIS installer failed with exit code $($installProc.ExitCode)"
    }
    Start-Sleep -Seconds 1

    $layout = Resolve-InstalledLayout
    $baselineVersion = if ($layout.UninstallEntry) { $layout.UninstallEntry.DisplayVersion } else { $null }
    Write-Host "Installed baseline version: $baselineVersion at $($layout.InstallDir)"

    $currentStep = "verify_baseline_hygiene"
    $payload = Assert-InstalledPayloadHygiene -InstallDir $layout.InstallDir

    # 2. Launch baseline, verify Core 3.14.10, DB creation, idle RAM
    $currentStep = "launch_baseline"
    Write-Host "[2] Verifying baseline installed runtime..."
    $baselineSampleSeconds = if ($resolvedUpgradeInstaller) { 0 } else { $IdleWindowSeconds }
    $baselineRuntimeReport = Invoke-InstalledCompanionCheck `
        -InstallDir $layout.InstallDir `
        -PicoExe $layout.PicoExe `
        -BundledPython $payload.BundledPython `
        -SampleDurationSeconds $baselineSampleSeconds `
        -SampleIntervalSeconds $IdleSampleIntervalSeconds

    $userDataDir = Join-Path $env:LOCALAPPDATA "PICO\data"
    $sentinelPath = Join-Path $userDataDir "qualification_sentinel.txt"
    "persist-across-upgrade-and-uninstall" | Set-Content -Path $sentinelPath -Encoding UTF8

    # Configure HKCU Run PICO + unrelated startup entry
    $runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (-not (Test-Path $runKeyPath)) {
        New-Item -Path $runKeyPath -Force | Out-Null
    }
    $unrelatedValueName = "PicoQualificationKeepUnrelated"
    $unrelatedValueData = '"C:\Windows\System32\cmd.exe" /c exit 0'
    $expectedPicoRunValue = "`"$($layout.PicoExe.FullName)`""
    New-ItemProperty -Path $runKeyPath -Name "PICO" -Value $expectedPicoRunValue -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $runKeyPath -Name $unrelatedValueName -Value $unrelatedValueData -PropertyType String -Force | Out-Null

    $upgradeReport = $null
    $finalRuntimeReport = $baselineRuntimeReport

    if ($resolvedUpgradeInstaller) {
        $currentStep = "upgrade_in_place"
        Write-Host "[3] Testing in-place upgrade ($baselineVersion -> next) and auto-start preservation..."
        $upgradeProc = Start-Process -FilePath $resolvedUpgradeInstaller -ArgumentList "/S" -PassThru -Wait
        if ($upgradeProc.ExitCode -ne 0) {
            throw "Silent NSIS upgrade installer failed with exit code $($upgradeProc.ExitCode)"
        }
        Start-Sleep -Seconds 1

        $layout = Resolve-InstalledLayout
        $upgradedVersion = if ($layout.UninstallEntry) { $layout.UninstallEntry.DisplayVersion } else { $null }
        if ($baselineVersion -and $upgradedVersion -eq $baselineVersion) {
            throw "Expected DisplayVersion to advance beyond '$baselineVersion' after upgrade, stayed '$upgradedVersion'"
        }
        Write-Host "Upgraded version: $upgradedVersion"

        $payload = Assert-InstalledPayloadHygiene -InstallDir $layout.InstallDir

        $runKeyObj = Get-Item -Path $runKeyPath
        $picoRunAfterUpgrade = $runKeyObj.GetValue("PICO", $null)
        $unrelatedAfterUpgrade = $runKeyObj.GetValue($unrelatedValueName, $null)
        $expectedUpgradedPicoRunValue = "`"$($layout.PicoExe.FullName)`""
        if (-not $picoRunAfterUpgrade) {
            throw "In-place upgrade removed HKCU Run entry 'PICO'"
        }
        if ($picoRunAfterUpgrade -ne $expectedUpgradedPicoRunValue) {
            throw "In-place upgrade altered HKCU Run entry 'PICO' to '$picoRunAfterUpgrade' (expected '$expectedUpgradedPicoRunValue')"
        }
        if ($unrelatedAfterUpgrade -ne $unrelatedValueData) {
            throw "In-place upgrade modified unrelated HKCU Run entry"
        }
        if (-not (Test-Path $sentinelPath) -or -not (Test-Path (Join-Path $userDataDir "pico.db"))) {
            throw "In-place upgrade removed user data or database in $userDataDir"
        }

        Write-Host "[4] Verifying /UPDATE uninstaller hook preserves HKCU Run 'PICO'..."
        $updateUninstProc = Start-Process -FilePath $layout.UninstallExe -ArgumentList "/S", "/UPDATE", "_?=$($layout.InstallDir)" -PassThru -Wait
        if ($updateUninstProc.ExitCode -ne 0) {
            throw "NSIS uninstaller /UPDATE invocation failed with exit code $($updateUninstProc.ExitCode)"
        }
        Start-Sleep -Seconds 1
        Remove-Item -Path $layout.UninstallExe -Force -ErrorAction SilentlyContinue

        $runKeyObj = Get-Item -Path $runKeyPath
        $picoRunAfterUpdateHook = $runKeyObj.GetValue("PICO", $null)
        if (-not $picoRunAfterUpdateHook) {
            throw "NSIS_HOOK_POSTUNINSTALL /UPDATE check unexpectedly removed HKCU Run entry 'PICO'"
        }
        if ($picoRunAfterUpdateHook -ne $expectedUpgradedPicoRunValue) {
            throw "NSIS /UPDATE hook altered HKCU Run entry 'PICO' to '$picoRunAfterUpdateHook' (expected '$expectedUpgradedPicoRunValue')"
        }

        # Re-apply upgraded installer so final runtime, 60s+ idle RAM, and genuine uninstall run cleanly on upgraded package
        $reapplyProc = Start-Process -FilePath $resolvedUpgradeInstaller -ArgumentList "/S" -PassThru -Wait
        if ($reapplyProc.ExitCode -ne 0) {
            throw "Re-applying upgraded NSIS installer failed with exit code $($reapplyProc.ExitCode)"
        }
        Start-Sleep -Seconds 1
        $layout = Resolve-InstalledLayout
        $payload = Assert-InstalledPayloadHygiene -InstallDir $layout.InstallDir

        $currentStep = "launch_upgraded_idle_60s"
        Write-Host "[5] Running $IdleWindowSeconds s synchronized idle RAM qualification on upgraded $upgradedVersion..."
        $finalRuntimeReport = Invoke-InstalledCompanionCheck `
            -InstallDir $layout.InstallDir `
            -PicoExe $layout.PicoExe `
            -BundledPython $payload.BundledPython `
            -SampleDurationSeconds $IdleWindowSeconds `
            -SampleIntervalSeconds $IdleSampleIntervalSeconds

        $upgradeReport = [ordered]@{
            upgrade_installer_path              = $resolvedUpgradeInstaller
            upgrade_installer_sha256            = (Get-FileHash -Path $resolvedUpgradeInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
            baseline_version                    = $baselineVersion
            upgraded_version                    = $upgradedVersion
            autostart_preserved_across_upgrade  = $true
            autostart_value_after_upgrade       = $picoRunAfterUpgrade
            autostart_preserved_on_update_hook  = $true
            autostart_value_after_update_hook   = $picoRunAfterUpdateHook
            user_db_preserved_across_upgrade    = $true
        }
    }

    # 6. Genuine uninstall verification
    $currentStep = "genuine_uninstall"
    Write-Host "[6] Verifying genuine uninstall cleanup (HKCU Run 'PICO' removed, unrelated Run + DB preserved)..."
    $uninstProc = Start-Process -FilePath $layout.UninstallExe -ArgumentList "/S", "_?=$($layout.InstallDir)" -PassThru -Wait
    if ($uninstProc.ExitCode -ne 0) {
        throw "Silent NSIS uninstaller failed with exit code $($uninstProc.ExitCode)"
    }
    Start-Sleep -Seconds 1

    $runKeyObj = Get-Item -Path $runKeyPath
    $picoRunStillExists = $null -ne $runKeyObj.GetValue("PICO", $null)
    $unrelatedActual = $runKeyObj.GetValue($unrelatedValueName, $null)
    $unrelatedStillExists = $unrelatedActual -eq $unrelatedValueData
    Remove-ItemProperty -Path $runKeyPath -Name $unrelatedValueName -ErrorAction SilentlyContinue
    Remove-Item -Path $layout.UninstallExe -Force -ErrorAction SilentlyContinue

    $exeRemovedAfterUninstall = -not (Test-Path $layout.PicoExe.FullName)
    $userDbPreservedAfterUninstall = Test-Path (Join-Path $userDataDir "pico.db")
    $sentinelPreservedAfterUninstall = Test-Path $sentinelPath
    Remove-Item -Path $sentinelPath -Force -ErrorAction SilentlyContinue

    if ($picoRunStillExists) { throw "Genuine uninstall failed to remove HKCU Run entry 'PICO'" }
    if (-not $unrelatedStillExists) { throw "Genuine uninstall modified or removed unrelated HKCU Run entry" }
    if (-not $exeRemovedAfterUninstall) { throw "Installed executable still present after uninstall: $($layout.PicoExe.FullName)" }
    if (-not $userDbPreservedAfterUninstall -or -not $sentinelPreservedAfterUninstall) {
        throw "Genuine uninstall unexpectedly deleted user data under $userDataDir"
    }

    $report = [ordered]@{
        installer_path                       = $resolvedInstaller
        installer_size_bytes                 = $installerInfo.Length
        installer_sha256                     = $installerSha256
        authenticode_status                  = $signature.Status.ToString()
        install_dir                          = $layout.InstallDir
        uninstall_display_name               = if ($layout.UninstallEntry) { $layout.UninstallEntry.DisplayName } else { $null }
        uninstall_publisher                  = if ($layout.UninstallEntry) { $layout.UninstallEntry.Publisher } else { $null }
        uninstall_version                    = if ($layout.UninstallEntry) { $layout.UninstallEntry.DisplayVersion } else { $null }
        health_check_passed                  = $finalRuntimeReport.health_check_passed
        bundled_python_verified              = $finalRuntimeReport.bundled_python_verified
        db_integrity_check                   = $finalRuntimeReport.db_integrity_check
        idle_webview2_process_count          = $finalRuntimeReport.idle_webview2_max_count
        idle_sampling_window_seconds         = $finalRuntimeReport.measured_window_seconds
        idle_sample_count                    = $finalRuntimeReport.sample_count
        companion_idle_working_set_mib       = $finalRuntimeReport.companion_ws_avg_mib
        companion_idle_working_set_min_mib   = $finalRuntimeReport.companion_ws_min_mib
        companion_idle_working_set_max_mib   = $finalRuntimeReport.companion_ws_max_mib
        companion_idle_private_ws_avg_mib    = $finalRuntimeReport.companion_priv_ws_avg_mib
        companion_idle_private_ws_max_mib    = $finalRuntimeReport.companion_priv_ws_max_mib
        companion_idle_private_mib           = $finalRuntimeReport.companion_priv_bytes_avg_mib
        companion_idle_private_max_mib       = $finalRuntimeReport.companion_priv_bytes_max_mib
        core_working_set_mib                 = $finalRuntimeReport.core_ws_avg_mib
        core_working_set_max_mib             = $finalRuntimeReport.core_ws_max_mib
        upgrade_qualification                = $upgradeReport
        autostart_pico_removed_on_uninstall  = (-not $picoRunStillExists)
        autostart_unrelated_preserved        = $unrelatedStillExists
        executable_removed_on_uninstall      = $exeRemovedAfterUninstall
        user_db_preserved_on_uninstall       = ($userDbPreservedAfterUninstall -and $sentinelPreservedAfterUninstall)
        idle_samples                         = $finalRuntimeReport.samples
    }

    $reportDir = Split-Path -Parent $ReportPath
    if ($reportDir -and -not (Test-Path $reportDir)) {
        New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -Path $ReportPath -Encoding UTF8
    Write-Host "Qualification report written to $ReportPath"
    $report | ConvertTo-Json -Depth 5
    Write-Host "::notice title=NSIS Qualification Passed::installer=$($report.installer_path) size=$($report.installer_size_bytes) sha256=$($report.installer_sha256) window_s=$($report.idle_sampling_window_seconds) companion_ws_avg_mib=$($report.companion_idle_working_set_mib) companion_ws_max_mib=$($report.companion_idle_working_set_max_mib) companion_priv_ws_max_mib=$($report.companion_idle_private_ws_max_mib) core_ws_max_mib=$($report.core_working_set_max_mib) wv2_idle=$($report.idle_webview2_process_count) bundled_py=$($report.bundled_python_verified) upgraded=$(if ($upgradeReport) { $upgradeReport.upgraded_version } else { 'none' }) db_preserved=$($report.user_db_preserved_on_uninstall)"

} catch {
    $errMsg = $_.Exception.Message
    $errLine = $_.InvocationInfo.ScriptLineNumber
    Write-Host "::error title=Installer Qualification Failed ($currentStep at line $errLine)::$errMsg"
    throw
}

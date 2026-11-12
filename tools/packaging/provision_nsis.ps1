<#
.SYNOPSIS
Reproducible, SHA-256-verified NSIS toolchain provisioning for PICO Windows packaging.

.DESCRIPTION
Locates an existing usable `makensis.exe` (PATH or standard install directories) and verifies
`makensis /VERSION`. If missing, downloads Tauri's pinned `nsis-3.13.zip` release archive with
bounded retries, verifies its exact SHA-256 digest before extraction, extracts cleanly to
$InstallRoot, registers the directory in PATH (and GITHUB_PATH when running in CI), and verifies
`makensis /VERSION`. Chocolatey is only consulted as a secondary fallback if direct archive
download fails after all retries.
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = $(Join-Path $env:LOCALAPPDATA "pico-tools\nsis"),
    [string]$NsisArchiveUrl = "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.13/nsis-3.13.zip",
    [string]$ExpectedSha256 = "ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8",
    [int]$MaxDownloadAttempts = 3,
    [switch]$AllowChocoFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Test-MakeNsisExecutable {
    param([string]$ExePath)

    if (-not $ExePath -or -not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        return $null
    }
    try {
        $ver = (& $ExePath /VERSION 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $ver) {
            return $ver
        }
    } catch {
        return $null
    }
    return $null
}

function Register-NsisPath {
    param([string]$BinDir)

    $resolved = (Resolve-Path -LiteralPath $BinDir).Path
    $pathEntries = ($env:PATH -split ";") | Where-Object { $_ }
    if ($pathEntries -notcontains $resolved) {
        $env:PATH = "$resolved;$env:PATH"
    }
    if ($env:GITHUB_PATH) {
        $resolved | Out-File -FilePath $env:GITHUB_PATH -Encoding utf8 -Append
    }
    return $resolved
}

function Find-ExistingMakeNsis {
    param([string]$SearchRoot)

    $cmd = Get-Command makensis -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = Test-MakeNsisExecutable -ExePath $cmd.Source
        if ($ver) {
            return [pscustomobject]@{
                Source  = "PATH"
                ExePath = $cmd.Source
                BinDir  = Split-Path -Parent $cmd.Source
                Version = $ver
            }
        }
        Write-Warning "Found 'makensis' on PATH at '$($cmd.Source)' but 'makensis /VERSION' failed."
    }

    $candidateDirs = @(
        "C:\Program Files (x86)\NSIS",
        "C:\Program Files\NSIS",
        "C:\ProgramData\chocolatey\lib\nsis\tools",
        $SearchRoot
    ) | Where-Object { $_ }

    foreach ($dir in $candidateDirs) {
        if (-not (Test-Path -LiteralPath $dir)) {
            continue
        }
        $direct = Join-Path $dir "makensis.exe"
        $ver = Test-MakeNsisExecutable -ExePath $direct
        if ($ver) {
            return [pscustomobject]@{
                Source  = "StandardDirectory"
                ExePath = $direct
                BinDir  = $dir
                Version = $ver
            }
        }
        $found = Get-ChildItem -Path $dir -Filter "makensis.exe" -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) {
            $verNested = Test-MakeNsisExecutable -ExePath $found.FullName
            if ($verNested) {
                return [pscustomobject]@{
                    Source  = "DiscoveredTree"
                    ExePath = $found.FullName
                    BinDir  = $found.DirectoryName
                    Version = $verNested
                }
            }
        }
    }
    return $null
}

$existing = Find-ExistingMakeNsis -SearchRoot $InstallRoot
if ($existing) {
    $regDir = Register-NsisPath -BinDir $existing.BinDir
    Write-Host "Using existing NSIS toolchain ($($existing.Source)): $($existing.ExePath) [VERSION=$($existing.Version)]"
    Write-Host "NSIS directory registered on PATH: $regDir"
    exit 0
}

Write-Host "No preinstalled makensis.exe found. Provisioning pinned NSIS archive from $NsisArchiveUrl..."
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

$tempZip = Join-Path ([System.IO.Path]::GetTempPath()) ("pico-nsis-3.13-" + [guid]::NewGuid().ToString("N") + ".zip")
$downloadSucceeded = $false
$downloadErrors = @()

try {
    for ($attempt = 1; $attempt -le $MaxDownloadAttempts; $attempt++) {
        try {
            if (Test-Path -LiteralPath $tempZip) {
                Remove-Item -LiteralPath $tempZip -Force -ErrorAction SilentlyContinue
            }
            Write-Host "Download attempt $attempt/$MaxDownloadAttempts -> $NsisArchiveUrl"
            Invoke-WebRequest -Uri $NsisArchiveUrl -OutFile $tempZip -UseBasicParsing -TimeoutSec 120
            if (-not (Test-Path -LiteralPath $tempZip -PathType Leaf)) {
                throw "Downloaded archive file missing at $tempZip"
            }

            $actualHash = (Get-FileHash -LiteralPath $tempZip -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualHash -ne $ExpectedSha256.ToLowerInvariant()) {
                throw "SHA-256 mismatch for NSIS archive! Expected=$ExpectedSha256 Actual=$actualHash"
            }

            Write-Host "Verified NSIS archive SHA-256: $actualHash"
            $downloadSucceeded = $true
            break
        } catch {
            $msg = $_.Exception.Message
            $downloadErrors += "Attempt ${attempt}: $msg"
            Write-Warning "NSIS download/verification attempt $attempt failed: $msg"
            if ($msg -like "SHA-256 mismatch*") {
                throw
            }
            if ($attempt -lt $MaxDownloadAttempts) {
                Start-Sleep -Seconds ([Math]::Min(15, $attempt * 3))
            }
        }
    }

    if ($downloadSucceeded) {
        $stagingDir = Join-Path $InstallRoot ("staging-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
        Expand-Archive -LiteralPath $tempZip -DestinationPath $stagingDir -Force

        $exeCandidate = Get-ChildItem -Path $stagingDir -Filter "makensis.exe" -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $exeCandidate) {
            throw "Extracted NSIS archive under $stagingDir does not contain makensis.exe"
        }

        $targetVersionDir = Join-Path $InstallRoot "nsis-3.13"
        if (Test-Path -LiteralPath $targetVersionDir) {
            Remove-Item -LiteralPath $targetVersionDir -Recurse -Force
        }
        $extractedRoot = $exeCandidate.Directory
        if ($extractedRoot.Name -ieq "Bin" -and $extractedRoot.Parent -and ($extractedRoot.Parent.FullName -ne $stagingDir)) {
            Move-Item -LiteralPath $extractedRoot.Parent.FullName -Destination $targetVersionDir
        } else {
            Move-Item -LiteralPath $extractedRoot.FullName -Destination $targetVersionDir
        }
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue

        $provisioned = Find-ExistingMakeNsis -SearchRoot $targetVersionDir
        if (-not $provisioned) {
            throw "makensis.exe failed version verification after extraction into $targetVersionDir"
        }
        $regDir = Register-NsisPath -BinDir $provisioned.BinDir
        Write-Host "Provisioned verified NSIS toolchain: $($provisioned.ExePath) [VERSION=$($provisioned.Version)]"
        Write-Host "NSIS directory registered on PATH: $regDir"
        exit 0
    }
} finally {
    Remove-Item -LiteralPath $tempZip -Force -ErrorAction SilentlyContinue
}

if ($AllowChocoFallback -and (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Warning "Primary archive download failed after $MaxDownloadAttempts attempts; trying Chocolatey secondary fallback..."
    & choco install nsis --no-progress -y
    if ($LASTEXITCODE -eq 0) {
        $fallback = Find-ExistingMakeNsis -SearchRoot $InstallRoot
        if ($fallback) {
            $regDir = Register-NsisPath -BinDir $fallback.BinDir
            Write-Host "Provisioned NSIS via Chocolatey fallback: $($fallback.ExePath) [VERSION=$($fallback.Version)]"
            Write-Host "NSIS directory registered on PATH: $regDir"
            exit 0
        }
    }
}

throw "Unable to provision verified NSIS toolchain. Download errors: $($downloadErrors -join ' | ')"

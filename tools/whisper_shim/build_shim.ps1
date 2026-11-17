<#
.SYNOPSIS
    Builds the PICO whisper.cpp native C-ABI shim on Windows x64 with AVX2 (CPU-only).

.DESCRIPTION
    1. Checks out upstream whisper.cpp pinned at tag v1.9.5 (commit d1be6fde11ac6e0407606b4e42fe72d34add8037).
    2. Builds whisper.dll and ggml DLLs with AVX2 CPU acceleration and GPU disabled.
    3. Builds pico_whisper_shim.dll exporting a clean, minimal C-ABI.
    4. Copies runtime DLLs into %LOCALAPPDATA%\PICO\runtime\bin\ (outside Git).
#>

[CmdletBinding()]
param(
    [string]$SourceDir = "$env:TEMP\whisper_cpp_src",
    [string]$BuildDir = "$env:TEMP\whisper_build",
    [string]$OutputDir = "$env:LOCALAPPDATA\PICO\runtime\bin",
    [string]$PinnedTag = "v1.9.5",
    [string]$PinnedCommit = "d1be6fde11ac6e0407606b4e42fe72d34add8037"
)

$ErrorActionPreference = "Stop"

Write-Host "=== PICO Whisper C-ABI Shim Builder ===" -ForegroundColor Cyan
Write-Host "Target: Windows x64 CPU-only (AVX2 enabled, CUDA/Vulkan disabled)"
Write-Host "Pinned upstream: tag $PinnedTag (commit $PinnedCommit)"

# 1. Ensure whisper.cpp source is present and pinned
if (-not (Test-Path $SourceDir)) {
    Write-Host "Cloning whisper.cpp from official upstream (ggml-org/whisper.cpp)..."
    git clone --depth 1 --branch $PinnedTag https://github.com/ggml-org/whisper.cpp.git $SourceDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE." }
}

Push-Location $SourceDir
try {
    $currentHead = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "git rev-parse HEAD failed with exit code $LASTEXITCODE." }
    if ($currentHead -ne $PinnedCommit) {
        Write-Warning "Source HEAD ($currentHead) differs from expected pinned commit ($PinnedCommit). Checking out $PinnedCommit..."
        git fetch --depth 1 origin $PinnedCommit
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed with exit code $LASTEXITCODE." }
        git checkout $PinnedCommit
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed with exit code $LASTEXITCODE." }
    }
    $verifiedHead = (git rev-parse HEAD).Trim()
    if ($verifiedHead -ne $PinnedCommit) {
        throw "Failed to verify pinned commit: HEAD is $verifiedHead, expected $PinnedCommit."
    }
    Write-Host "Source verified at pinned commit $PinnedCommit." -ForegroundColor Green
} finally {
    Pop-Location
}

# 2. Locate CMake
$cmakePath = "cmake"
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    $venvCmake = Join-Path $PSScriptRoot "..\..\.venv\Scripts\cmake.exe"
    if (Test-Path $venvCmake) {
        $cmakePath = (Resolve-Path $venvCmake).Path
    } else {
        throw "CMake not found. Please install cmake or install into .venv via 'pip install cmake'."
    }
}
Write-Host "Using CMake: $cmakePath"

# 3. Configure and build pico_whisper_shim via unified CMake project
Write-Host "Configuring pico_whisper_shim build..." -ForegroundColor Cyan
& $cmakePath -B $BuildDir -S $PSScriptRoot `
    -G "Visual Studio 17 2022" -A x64 `
    -DWHISPER_DIR="$SourceDir"
if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed with exit code $LASTEXITCODE." }

# 4. Build pico_whisper_shim (Release) - automatically builds whisper and all ggml dependencies
Write-Host "Building pico_whisper_shim and upstream runtime (Release)..." -ForegroundColor Cyan
& $cmakePath --build $BuildDir --config Release --target pico_whisper_shim
if ($LASTEXITCODE -ne 0) { throw "CMake build failed with exit code $LASTEXITCODE." }

# 5. Stage DLLs into OutputDir
Write-Host "Staging runtime binaries into $OutputDir..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$dllDirs = @(
    (Join-Path $BuildDir "bin\Release"),
    (Join-Path $BuildDir "whisper_build\bin\Release")
)
$stagedCount = 0
foreach ($dir in $dllDirs) {
    if (Test-Path $dir) {
        $dlls = Get-ChildItem -Path $dir -Filter "*.dll"
        foreach ($dll in $dlls) {
            Copy-Item $dll.FullName -Destination $OutputDir -Force
            Write-Host "  Staged: $($dll.Name) ($([math]::Round($dll.Length/1KB, 1)) KB)"
            $stagedCount++
        }
    }
}

if ($stagedCount -eq 0) {
    throw "No runtime DLLs found to stage from $BuildDir."
}

Write-Host "=== Build and Staging Complete ===" -ForegroundColor Green

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
}

Push-Location $SourceDir
try {
    $currentHead = (git rev-parse HEAD).Trim()
    if ($currentHead -ne $PinnedCommit) {
        Write-Warning "Source HEAD ($currentHead) differs from expected pinned commit ($PinnedCommit). Checking out $PinnedCommit..."
        git fetch --depth 1 origin $PinnedCommit
        git checkout $PinnedCommit
    } else {
        Write-Host "Source verified at pinned commit $PinnedCommit." -ForegroundColor Green
    }
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

# 3. Configure whisper.cpp with CMake
Write-Host "Configuring whisper.cpp build..." -ForegroundColor Cyan
& $cmakePath -B $BuildDir -S $SourceDir `
    -G "Visual Studio 17 2022" -A x64 `
    -DWHISPER_BUILD_EXAMPLES=OFF `
    -DWHISPER_BUILD_TESTS=OFF `
    -DWHISPER_BUILD_SERVER=OFF `
    -DBUILD_SHARED_LIBS=ON `
    -DGGML_CUDA=OFF `
    -DGGML_VULKAN=OFF `
    -DGGML_OPENCL=OFF `
    -DGGML_AVX2=ON `
    -DGGML_FMA=ON
if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed." }

# 4. Build whisper.dll
Write-Host "Building whisper.dll (Release)..." -ForegroundColor Cyan
& $cmakePath --build $BuildDir --config Release --target whisper
if ($LASTEXITCODE -ne 0) { throw "whisper build failed." }

# 5. Build pico_whisper_shim.dll
Write-Host "Compiling pico_whisper_shim.dll..." -ForegroundColor Cyan
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"

if (-not (Test-Path $vcvars)) {
    throw "vcvars64.bat not found at $vcvars"
}

$shimSource = Join-Path $PSScriptRoot "pico_whisper_shim.cpp"
$includeWhisper = Join-Path $SourceDir "include"
$includeGgml = Join-Path $SourceDir "ggml\include"
$whisperLib = Join-Path $BuildDir "src\Release\whisper.lib"
$shimDllOut = Join-Path $BuildDir "bin\Release\pico_whisper_shim.dll"

$cmd = "call `"$vcvars`" && cl /O2 /EHsc /MD /DPICO_WHISPER_SHIM_EXPORTS /I `"$includeWhisper`" /I `"$includeGgml`" `"$shimSource`" /LD /Fe:`"$shimDllOut`" /link `"$whisperLib`""
cmd /c $cmd
if ($LASTEXITCODE -ne 0) { throw "pico_whisper_shim.dll compilation failed." }

# 6. Stage DLLs into OutputDir
Write-Host "Staging runtime binaries into $OutputDir..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$dlls = Get-ChildItem -Path (Join-Path $BuildDir "bin\Release") -Filter "*.dll"
foreach ($dll in $dlls) {
    Copy-Item $dll.FullName -Destination $OutputDir -Force
    Write-Host "  Staged: $($dll.Name) ($([math]::Round($dll.Length/1KB, 1)) KB)"
}

Write-Host "=== Build and Staging Complete ===" -ForegroundColor Green

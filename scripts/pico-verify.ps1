<#
.SYNOPSIS
    PICO Development Verification Script.

.DESCRIPTION
    Runs the standard suite of repository integrity checks:
    1. Python bytecode compilation (pico, tests, tools).
    2. Git whitespace and conflict marker check (git diff --check).
    3. Full automated pytest suite.

    Strictly preserves repository state: never commits, pushes, merges, resets, or discards changes.
    Exits with non-zero code if any step fails.

.EXAMPLE
    .\scripts\pico-verify.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  PICO Development Integrity Verification" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host ""

$pythonExe = $null
if (Test-Path (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")) {
    $pythonExe = (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")
} elseif ($env:VIRTUAL_ENV -and (Test-Path "$env:VIRTUAL_ENV\Scripts\python.exe")) {
    $pythonExe = "$env:VIRTUAL_ENV\Scripts\python.exe"
} else {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonExe = $cmd.Source
    }
}
if (-not $pythonExe -or -not (Test-Path $pythonExe)) {
    Write-Host "[FAIL] Python executable / virtual environment not found." -ForegroundColor Red
    exit 1
}

$results = @()
$failed = $false

# -----------------------------------------------------------------------------
# Step 1: Bytecode Compilation
# -----------------------------------------------------------------------------
Write-Host "[1/3] Checking Python Bytecode Compilation..." -ForegroundColor Yellow
$compileSw = [System.Diagnostics.Stopwatch]::StartNew()
& $pythonExe -m compileall -q pico tests tools
$compileExit = $LASTEXITCODE
$compileSw.Stop()

if ($compileExit -eq 0) {
    Write-Host "      Status: PASS ($([math]::Round($compileSw.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green
    $results += @{ Name = "Bytecode Compilation"; Status = "PASS"; Duration = "$([math]::Round($compileSw.Elapsed.TotalSeconds, 2))s" }
} else {
    Write-Host "      Status: FAIL (compileall exited with code $compileExit)" -ForegroundColor Red
    $results += @{ Name = "Bytecode Compilation"; Status = "FAIL"; Duration = "$([math]::Round($compileSw.Elapsed.TotalSeconds, 2))s" }
    $failed = $true
}
Write-Host ""

# -----------------------------------------------------------------------------
# Step 2: Git Diff Check
# -----------------------------------------------------------------------------
Write-Host "[2/3] Checking Git Diff & Whitespace Markers..." -ForegroundColor Yellow
$gitSw = [System.Diagnostics.Stopwatch]::StartNew()
git diff --check
$gitExit = $LASTEXITCODE
$gitSw.Stop()

if ($gitExit -eq 0) {
    Write-Host "      Status: PASS ($([math]::Round($gitSw.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green
    $results += @{ Name = "Git Diff Check"; Status = "PASS"; Duration = "$([math]::Round($gitSw.Elapsed.TotalSeconds, 2))s" }
} else {
    Write-Host "      Status: FAIL (git diff --check detected issues)" -ForegroundColor Red
    $results += @{ Name = "Git Diff Check"; Status = "FAIL"; Duration = "$([math]::Round($gitSw.Elapsed.TotalSeconds, 2))s" }
    $failed = $true
}
Write-Host ""

# -----------------------------------------------------------------------------
# Step 3: Full Automated Test Suite
# -----------------------------------------------------------------------------
if (-not $SkipTests) {
    Write-Host "[3/3] Running Full Pytest Suite..." -ForegroundColor Yellow
    $testSw = [System.Diagnostics.Stopwatch]::StartNew()
    & $pythonExe -m pytest -v
    $testExit = $LASTEXITCODE
    $testSw.Stop()

    if ($testExit -eq 0) {
        Write-Host "      Status: PASS ($([math]::Round($testSw.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green
        $results += @{ Name = "Pytest Suite"; Status = "PASS"; Duration = "$([math]::Round($testSw.Elapsed.TotalSeconds, 2))s" }
    } else {
        Write-Host "      Status: FAIL (pytest exited with code $testExit)" -ForegroundColor Red
        $results += @{ Name = "Pytest Suite"; Status = "FAIL"; Duration = "$([math]::Round($testSw.Elapsed.TotalSeconds, 2))s" }
        $failed = $true
    }
} else {
    Write-Host "[3/3] Pytest Suite: SKIPPED (-SkipTests specified)" -ForegroundColor DarkGray
    $results += @{ Name = "Pytest Suite"; Status = "SKIPPED"; Duration = "0s" }
}
Write-Host ""

# -----------------------------------------------------------------------------
# Verification Summary
# -----------------------------------------------------------------------------
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Verification Summary" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
foreach ($r in $results) {
    $color = if ($r.Status -eq "PASS") { "Green" } elseif ($r.Status -eq "SKIPPED") { "DarkGray" } else { "Red" }
    Write-Host ("  {0,-25} : {1,-8} ({2})" -f $r.Name, $r.Status, $r.Duration) -ForegroundColor $color
}
Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan

if ($failed) {
    Write-Host "Result: FAILED. One or more verification gates did not pass." -ForegroundColor Red
    exit 1
} else {
    Write-Host "Result: PASSED. All verification gates cleared." -ForegroundColor Green
    exit 0
}

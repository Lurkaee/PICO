<#
.SYNOPSIS
    PICO Benchmark Wrapper Script.

.DESCRIPTION
    Safely orchestrates local hardware inference benchmarking:
    1. Runs the preflight script (pico-preflight.ps1) to verify runtime prerequisites.
    2. Aborts immediately if Ollama is unreachable or the candidate model is missing.
    3. Runs the benchmark runner (tools.bench.runner) with configured options.
    4. Supports both live Ollama inference and deterministic mock runs.
    5. Preserves timestamped reports in bench_reports/ and propagates exit codes.

    Safety: Never automatically downloads models or redirects private data to cloud models.

.PARAMETER Model
    The Ollama model to benchmark (default: "qwen3.5:2b").

.PARAMETER BaseUrl
    The Ollama native HTTP endpoint (default: "http://127.0.0.1:11434").

.PARAMETER Repetitions
    Number of measured benchmark iterations to execute (default: 3).

.PARAMETER ContextSize
    Token context budget (default: 4096).

.PARAMETER MaxTokens
    Bounded maximum output tokens per iteration (default: 128).

.PARAMETER OutputDir
    Directory where timestamped JSON and Markdown reports are saved (default: "bench_reports").

.PARAMETER Mock
    Execute deterministic offline benchmark with simulated delays (no Ollama required).

.PARAMETER StartOllama
    Explicit opt-in switch passed to preflight to launch the local Ollama daemon if offline.

.PARAMETER MinTokensPerSec
    Optional quality gate threshold for minimum generation throughput.

.PARAMETER MaxTtftMs
    Optional quality gate threshold for maximum Time To First Token.

.EXAMPLE
    .\scripts\pico-benchmark.ps1 -Mock
    .\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -Repetitions 3
    .\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -MinTokensPerSec 15.0 -MaxTtftMs 1200.0
#>

[CmdletBinding()]
param(
    [string]$Model = "qwen3.5:2b",
    [string]$BaseUrl = "http://127.0.0.1:11434",
    [int]$Repetitions = 3,
    [int]$ContextSize = 4096,
    [int]$MaxTokens = 128,
    [string]$OutputDir = "bench_reports",
    [switch]$Mock,
    [switch]$StartOllama,
    [double]$MinTokensPerSec = 0.0,
    [double]$MaxTtftMs = 0.0
)

$ErrorActionPreference = "Continue"

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  PICO Benchmark Orchestrator" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Target Model : $Model"
Write-Host "  Repetitions  : $Repetitions (+ 1 warmup)"
Write-Host "  Context Size : $ContextSize tokens"
Write-Host "  Max Output   : $MaxTokens tokens"
Write-Host "  Output Dir   : $OutputDir"
Write-Host "  Mode         : $(if ($Mock) { 'MOCK (Simulated Development)' } else { 'LIVE LOCAL OLLAMA' })"
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

$preflightScript = Join-Path $PSScriptRoot "pico-preflight.ps1"
if (-not (Test-Path $preflightScript)) {
    Write-Host "[FAIL] Preflight script not found at: $preflightScript" -ForegroundColor Red
    exit 1
}

# -----------------------------------------------------------------------------
# Step 1: Execute Preflight Gate
# -----------------------------------------------------------------------------
Write-Host "[*] Executing Preflight Checks..." -ForegroundColor Yellow
$preflightArgs = @(
    "-ExecutionPolicy", "Bypass",
    "-File", $preflightScript,
    "-Model", $Model,
    "-BaseUrl", $BaseUrl
)
if ($Mock) { $preflightArgs += "-Mock" }
if ($StartOllama) { $preflightArgs += "-StartOllama" }

& powershell.exe @preflightArgs
$preflightExit = $LASTEXITCODE

if ($preflightExit -ne 0) {
    Write-Host ""
    Write-Host "[!] Preflight Failed (Exit Code: $preflightExit)." -ForegroundColor Red
    Write-Host "    Benchmark execution aborted because prerequisites are not met." -ForegroundColor Red
    Write-Host "    Resolve the issues noted above or run with -Mock for offline validation." -ForegroundColor Red
    exit $preflightExit
}

Write-Host ""
Write-Host "[*] Preflight passed. Launching benchmark runner..." -ForegroundColor Green
if ($Mock) {
    Write-Host "    [NOTE] Mock mode active: results reflect simulated timing and are NOT a live hardware benchmark." -ForegroundColor Yellow
}
Write-Host ""

# -----------------------------------------------------------------------------
# Step 2: Build Runner Arguments & Execute
# -----------------------------------------------------------------------------
$runnerArgs = @(
    "-m", "tools.bench.runner",
    "--model", $Model,
    "--base-url", $BaseUrl,
    "--repetitions", $Repetitions.ToString(),
    "--context-size", $ContextSize.ToString(),
    "--max-tokens", $MaxTokens.ToString(),
    "--output-dir", $OutputDir
)

if ($Mock) {
    $runnerArgs += "--mock"
}
if ($MinTokensPerSec -gt 0.0) {
    $runnerArgs += "--min-tokens-per-sec"
    $runnerArgs += $MinTokensPerSec.ToString()
}
if ($MaxTtftMs -gt 0.0) {
    $runnerArgs += "--max-ttft-ms"
    $runnerArgs += $MaxTtftMs.ToString()
}

& $pythonExe @runnerArgs
$runnerExit = $LASTEXITCODE

Write-Host ""
if ($runnerExit -eq 0) {
    Write-Host "[+] Benchmark execution completed successfully." -ForegroundColor Green
} elseif ($runnerExit -eq 2) {
    Write-Host "[!] Benchmark reported BLOCKED." -ForegroundColor Yellow
} else {
    Write-Host "[-] Benchmark execution failed (Exit Code: $runnerExit)." -ForegroundColor Red
}

exit $runnerExit

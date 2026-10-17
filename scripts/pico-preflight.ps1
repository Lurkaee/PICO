<#
.SYNOPSIS
    PICO Local Runtime Preflight Check Script.

.DESCRIPTION
    Validates that the local environment meets PICO requirements:
    - Verifies Ollama CLI is installed.
    - Checks reachability of the Ollama native API at 127.0.0.1:11434.
    - Optionally starts Ollama if -StartOllama is explicitly requested.
    - Checks whether the target candidate model (default: qwen3.5:2b) is installed.
    - Reports host physical RAM and GPU VRAM telemetry.
    - Emits clear [PASS], [BLOCKED], or [WARNING] status badges.
    - Exits with non-zero code if any prerequisite is blocked.

.PARAMETER Model
    The target model tag to check for in local Ollama (default: "qwen3.5:2b").

.PARAMETER BaseUrl
    The base URL for the local Ollama API (default: "http://127.0.0.1:11434").

.PARAMETER StartOllama
    Explicit opt-in switch to launch the Ollama daemon if it is not currently running.

.PARAMETER Mock
    Bypass live Ollama and model checks for offline development.

.EXAMPLE
    .\scripts\pico-preflight.ps1
    .\scripts\pico-preflight.ps1 -Model "qwen3.5:2b"
    .\scripts\pico-preflight.ps1 -StartOllama
    .\scripts\pico-preflight.ps1 -Mock
#>

[CmdletBinding()]
param(
    [string]$Model = "qwen3.5:2b",
    [string]$BaseUrl = "http://127.0.0.1:11434",
    [switch]$StartOllama,
    [switch]$Mock
)

$ErrorActionPreference = "Continue"

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  PICO Local Runtime Preflight Verification" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Target Model : $Model"
Write-Host "  API Base URL : $BaseUrl"
Write-Host "  Mode         : $(if ($Mock) { 'MOCK (Offline Development)' } else { 'LIVE LOCAL RUNTIME' })"
Write-Host ""

$blockedReasons = @()
$warningMessages = @()

# -----------------------------------------------------------------------------
# Check 0: Mock Mode Bypass
# -----------------------------------------------------------------------------
if ($Mock) {
    Write-Host "[PASS] Execution Mode: MOCK" -ForegroundColor Green
    Write-Host "       Running in offline development mode. Live Ollama daemon is not required."
    Write-Host ""
}

# -----------------------------------------------------------------------------
# Check 1: Ollama CLI Installation
# -----------------------------------------------------------------------------
$ollamaCmd = Get-Command "ollama" -ErrorAction SilentlyContinue

# If not in process PATH, check standard Windows install locations
$standardOllamaPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not $ollamaCmd -and (Test-Path $standardOllamaPath)) {
    $env:PATH = "$env:LOCALAPPDATA\Programs\Ollama;$env:PATH"
    $ollamaCmd = Get-Command "ollama" -ErrorAction SilentlyContinue
}

if (-not $Mock) {
    if ($ollamaCmd) {
        Write-Host "[PASS] Ollama CLI: Installed" -ForegroundColor Green
        Write-Host "       Binary: $($ollamaCmd.Source)"
    } else {
        Write-Host "[BLOCKED] Ollama CLI: Not Found" -ForegroundColor Red
        Write-Host "          Ollama executable was not found on PATH or at '$standardOllamaPath'."
        Write-Host "          Installation instructions:"
        Write-Host "          - Run PowerShell as user: irm https://ollama.com/install.ps1 | iex"
        Write-Host "          - Or download installer from: https://ollama.ai/download/windows"
        $blockedReasons += "Ollama CLI is not installed or not in PATH."
    }
    Write-Host ""
}

# -----------------------------------------------------------------------------
# Check 2: Ollama API Reachability & Optional Startup
# -----------------------------------------------------------------------------
$apiReachable = $false
$modelsList = @()

if (-not $Mock) {
    function Test-OllamaApi {
        param([string]$Url)
        try {
            $resp = Invoke-RestMethod -Uri "$Url/api/tags" -Method Get -TimeoutSec 2 -ErrorAction Stop
            return @{ Success = $true; Data = $resp }
        } catch {
            return @{ Success = $false; Error = $_.Exception.Message }
        }
    }

    $probe = Test-OllamaApi -Url $BaseUrl
    if ($probe.Success) {
        $apiReachable = $true
        $modelsList = $probe.Data.models
    } else {
        # API is not currently reachable
        if ($StartOllama) {
            Write-Host "[*] -StartOllama specified: Attempting to launch local Ollama process..." -ForegroundColor Yellow
            $exeToLaunch = if ($ollamaCmd) { $ollamaCmd.Source } elseif (Test-Path $standardOllamaPath) { $standardOllamaPath } else { $null }

            if ($exeToLaunch) {
                # Launch Ollama serve as a detached background process
                $proc = Start-Process -FilePath $exeToLaunch -ArgumentList "serve" -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
                Write-Host "    Spawned process ID: $($proc.Id). Waiting for API endpoint to become responsive..."

                # Wait up to 5 seconds for port 11434
                $waited = 0
                while ($waited -lt 5) {
                    Start-Sleep -Seconds 1
                    $waited++
                    $retryProbe = Test-OllamaApi -Url $BaseUrl
                    if ($retryProbe.Success) {
                        $apiReachable = $true
                        $modelsList = $retryProbe.Data.models
                        break
                    }
                }

                if ($apiReachable) {
                    Write-Host "    [INFO] Service is running. To stop Ollama later, run:" -ForegroundColor Cyan
                    Write-Host "           Stop-Process -Name 'ollama' -Force" -ForegroundColor Cyan
                } else {
                    Write-Host "    [!] Process started but API endpoint did not respond within 5 seconds." -ForegroundColor Red
                }
            } else {
                Write-Host "    [!] Cannot start Ollama: executable path not found." -ForegroundColor Red
            }
        }
    }

    if ($apiReachable) {
        Write-Host "[PASS] Ollama API: Reachable ($BaseUrl)" -ForegroundColor Green
    } else {
        Write-Host "[BLOCKED] Ollama API: Unreachable ($BaseUrl)" -ForegroundColor Red
        Write-Host "          The local Ollama inference service is not running."
        Write-Host "          To start Ollama:"
        Write-Host "          - In terminal: Run 'ollama serve' in a separate terminal window."
        Write-Host "          - Or re-run with explicit opt-in: .\\scripts\\pico-preflight.ps1 -StartOllama"
        Write-Host "          - Or start Ollama from the Windows Start menu / system tray."
        $blockedReasons += "Ollama API is unreachable at $BaseUrl."
    }
    Write-Host ""
}

# -----------------------------------------------------------------------------
# Check 3: Candidate Model Availability
# -----------------------------------------------------------------------------
if (-not $Mock -and $apiReachable) {
    $modelFound = $false
    $matchedDigest = $null

    foreach ($m in $modelsList) {
        $name = $m.name
        if ($name -eq $Model -or $name.StartsWith("$Model`:") -or $Model.StartsWith("$name`:")) {
            $modelFound = $true
            $matchedDigest = $m.digest
            break
        }
    }

    if ($modelFound) {
        Write-Host "[PASS] Model Availability: Installed ('$Model')" -ForegroundColor Green
        if ($matchedDigest) {
            Write-Host "       Digest: $matchedDigest"
        }
    } else {
        $availableNames = ($modelsList | ForEach-Object { $_.name }) -join ", "
        if (-not $availableNames) { $availableNames = "none" }

        Write-Host "[BLOCKED] Model Availability: Missing ('$Model')" -ForegroundColor Red
        Write-Host "          The required candidate model '$Model' is not installed."
        Write-Host "          Currently installed models: [$availableNames]"
        Write-Host "          To install the model manually, run:"
        Write-Host "          ollama run $Model" -ForegroundColor Yellow
        Write-Host "          (PICO safety invariants strictly forbid automated model downloads)."
        $blockedReasons += "Candidate model '$Model' is not installed in local Ollama."
    }
    Write-Host ""
}

# -----------------------------------------------------------------------------
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
if ($pythonExe -and (Test-Path $pythonExe)) {
    try {
        $telemetryJson = & $pythonExe -c "
from tools.bench.telemetry import get_system_memory, get_gpu_memory
import json
sys_mem = get_system_memory()
gpu_mem = get_gpu_memory()
res = {
    'ram_total_mb': sys_mem.total_mb,
    'ram_avail_mb': sys_mem.available_mb,
    'ram_used_mb': sys_mem.used_mb,
    'gpu_available': gpu_mem.available,
    'gpu_name': gpu_mem.gpu_name,
    'gpu_vram_total_mb': gpu_mem.total_vram_mb,
    'gpu_vram_used_mb': gpu_mem.used_vram_mb,
    'gpu_source': gpu_mem.source,
    'gpu_error': gpu_mem.error_reason
}
print(json.dumps(res))
"
        $telem = $telemetryJson | ConvertFrom-Json

        # RAM Evaluation
        if ($telem.ram_total_mb -ge 14000) { # ~16 GB
            Write-Host "[PASS] System Physical RAM: $([math]::Round($telem.ram_total_mb / 1024, 1)) GB total ($([math]::Round($telem.ram_avail_mb / 1024, 1)) GB available)" -ForegroundColor Green
        } elseif ($telem.ram_total_mb -gt 0) {
            Write-Host "[WARNING] System Physical RAM: $([math]::Round($telem.ram_total_mb / 1024, 1)) GB" -ForegroundColor Yellow
            Write-Host "          PICO baseline targets 16 GB RAM. Context budget will be constrained."
            $warningMessages += "Host RAM is below the 16 GB baseline."
        }

        # GPU Evaluation
        if ($telem.gpu_available) {
            Write-Host "[PASS] Dedicated GPU: $($telem.gpu_name)" -ForegroundColor Green
            Write-Host "       Dedicated VRAM: $([math]::Round($telem.gpu_vram_total_mb / 1024, 1)) GB (Source: $($telem.gpu_source))"
        } else {
            Write-Host "[WARNING] Dedicated GPU: Not Available" -ForegroundColor Yellow
            Write-Host "          Reason: $($telem.gpu_error)"
            Write-Host "          Inference will execute on CPU only."
            $warningMessages += "No dedicated NVIDIA GPU detected via nvidia-smi."
        }
    } catch {
        Write-Host "[WARNING] Telemetry: Failed to query system resources ($($_.Exception.Message))" -ForegroundColor Yellow
    }
} else {
    Write-Host "[WARNING] Python Virtual Environment: Not found at '$pythonExe'." -ForegroundColor Yellow
    $warningMessages += "Python virtual environment .venv is missing."
}
Write-Host ""

# -----------------------------------------------------------------------------
# Summary & Exit Code
# -----------------------------------------------------------------------------
Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan
if ($blockedReasons.Count -gt 0) {
    Write-Host "Preflight Result: BLOCKED ($($blockedReasons.Count) issue(s) require action)" -ForegroundColor Red
    foreach ($r in $blockedReasons) {
        Write-Host "  - $r" -ForegroundColor Red
    }
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan
    exit 2
} elseif ($warningMessages.Count -gt 0) {
    Write-Host "Preflight Result: PASS (with $($warningMessages.Count) warning(s))" -ForegroundColor Yellow
    foreach ($w in $warningMessages) {
        Write-Host "  - $w" -ForegroundColor Yellow
    }
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan
    exit 0
} else {
    Write-Host "Preflight Result: PASS (All local runtime prerequisites satisfied)" -ForegroundColor Green
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan
    exit 0
}

# PICO — Personal Intelligence Coordinator & Operator

PICO is a local-first, multi-model personal intelligence system engineered for verifiable persistence, private context compilation, transparent multi-model routing, and safe tool execution.

---

## Architecture & Documentation

- [AGENTS.md](AGENTS.md) — Non-negotiable operating rules, hardware constraints, and engineering principles.
- [docs/architecture.md](docs/architecture.md) — System architecture, subsystem boundaries, and request lifecycle data flow.
- [docs/data-model.md](docs/data-model.md) — Relational persistence models, SQLite WAL configuration, and schema evolution.
- [docs/api-contract.md](docs/api-contract.md) — REST API endpoints, schemas, validation, and error envelopes.
- [docs/security-and-risk-register.md](docs/security-and-risk-register.md) — Threat modeling, local trust boundaries, and risk mitigations.
- [docs/implementation-plan.md](docs/implementation-plan.md) — Phased delivery plan and completion statuses.
- [docs/evaluation/README.md](docs/evaluation/README.md) — Evaluation framework and representative test case specifications.
- [docs/model-gateway-design.md](docs/model-gateway-design.md) — Per-step model router, profiles, and data classification policies.

---

## Local Setup & Quickstart (Windows PowerShell)

### 1. Prerequisites
- Python 3.12+ (tested on Python 3.14.7)
- Windows PowerShell

### 2. Environment Activation & Dependencies
```powershell
# Create virtual environment (if not already created)
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install pinned dependencies
pip install -r requirements.txt
```

### 3. Database Migrations & Seeding
```powershell
# Apply Alembic schema migrations (conversations, messages, projects, tasks, routing_decisions)
alembic upgrade head

# (Optional) Seed deterministic local development data
python -m pico.db.seed
```

### 4. Run Backend Server
```powershell
uvicorn pico.main:app --host 127.0.0.1 --port 8000 --reload
```
Once running:
- **Interactive OpenAPI Documentation**: `http://127.0.0.1:8000/docs`
- **Health Check**: `http://127.0.0.1:8000/health`

---

## Model Providers & Streaming Chat (Phase 1, Slice 2)

PICO features a provider-agnostic model gateway with per-step deterministic routing.

### 1. Exercise Streaming Chat with Development Mock (Default)
The system defaults to a deterministic, offline `mock` provider that requires no external runtimes or credentials:

```powershell
# In PowerShell: create a conversation and invoke streaming chat
$conv = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/conversations" -Method Post -ContentType "application/json" -Body '{"title": "Demo Thread"}'

$body = @{
    conversation_id = $conv.id
    message = "Explain PICO local persistence."
    data_classification = "INTERNAL"
} | ConvertTo-Json

# Send request to streaming endpoint
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/chat/stream" -Method Post -ContentType "application/json" -Body $body
```

### 2. Check and Use Local Ollama
To switch to local Ollama inference:

```powershell
# 1. Verify Ollama is running on localhost
curl.exe -s http://127.0.0.1:11434/api/tags

# 2. Verify that qwen3.5:2b is already installed (PICO never downloads models automatically)
# If missing, pull manually outside PICO:
# ollama pull qwen3.5:2b

# 3. Configure PICO to use Ollama by setting DEFAULT_PROVIDER in .env or PowerShell session
$env:DEFAULT_PROVIDER = "ollama"
$env:OLLAMA_MODEL = "qwen3.5:2b"
```

### 3. Explicit Cloud Opt-In
Cloud inference is strictly disabled by default. Private and secret data never leaves the machine. To explicitly enable cloud inference for `PUBLIC` data:

```powershell
# Enable cloud access in .env or session
$env:CLOUD_ENABLED = "true"
$env:CLOUD_BASE_URL = "https://api.openai.com/v1"
$env:CLOUD_MODEL = "gpt-4o-mini"
$env:CLOUD_API_KEY = "sk-..."  # Your private API key
```

> [!IMPORTANT]
> Any prompt marked `INTERNAL`, `SENSITIVE`, or `SECRET` will remain pinned to local inference even if cloud is enabled.

---

## Local Hardware Benchmarking (`tools/bench/`)

PICO includes a local benchmark runner to measure model readiness and system resource consumption (TTFT, tokens/sec, RAM, GPU VRAM) under the Windows target hardware baseline.

### 1. Check Ollama Availability & Installed Models
Before running a live benchmark, verify that Ollama is running and inspect locally available models without initiating downloads:
```powershell
# Probe Ollama API tags endpoint
curl.exe -s http://127.0.0.1:11434/api/tags

# Or use the Ollama CLI
ollama list
```

If Ollama is not installed or running, install it from https://ollama.ai and start it via `ollama serve`. To install the candidate model manually:
```powershell
ollama run qwen3.5:2b
```

### 2. Run Mock Benchmark (Offline Development Path)
Test runner plumbing, timing calculations, and report generation without requiring Ollama or active models:
```powershell
.\.venv\Scripts\python.exe -m tools.bench.runner --mock --repetitions 3 --output-dir bench_reports
```
> [!NOTE]
> A successful `--mock` test run exercises the runner tooling only and must **never** be cited as a completed hardware evaluation.

### 3. Run Live Hardware Benchmark
Once Ollama is running and `qwen3.5:2b` is installed:
```powershell
# Run 3 measured iterations (+ 1 warmup run)
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 3 --output-dir bench_reports

# Run with custom repetitions and context size
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 5 --context-size 4096 --max-tokens 128

# Run with quality gate thresholds (e.g. minimum 15 tok/s, max 1200ms TTFT)
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 3 --min-tokens-per-sec 15.0 --max-ttft-ms 1200.0
```

### 4. Reading Benchmark Reports
Benchmark reports are saved to `bench_reports/` with unique UTC timestamps:
- **Markdown summary**: Open `bench_reports/bench_qwen3.5_2b_<timestamp>.md` to view formatted tables with medians, min-max ranges, memory telemetry, and quality gate evaluations.
- **Machine-readable JSON**: Open `bench_reports/bench_qwen3.5_2b_<timestamp>.json` for complete auditable per-run logs.

---

## Safe Local Runtime Automation (PowerShell Scripts)

PICO provides safe, repeatable PowerShell automation scripts under `scripts/` to validate local runtimes, inspect host hardware, run development verification checks, and execute benchmarks with strict safety invariants (no automatic downloads, no silent cloud routing, and strictly opt-in service startup).

### 1. Local Runtime Preflight (`scripts/pico-preflight.ps1`)

Verifies that local prerequisites are met prior to running live inference:
- Probes for the Ollama CLI on PATH and default Windows install paths (`$env:LOCALAPPDATA\Programs\Ollama`).
- Checks if the Ollama local API at `http://127.0.0.1:11434` is actively listening.
- Verifies if the candidate model (`qwen3.5:2b` by default) has been pulled locally.
- Inspects and reports system RAM and dedicated GPU VRAM (via `nvidia-smi`).
- Emits explicit `[PASS]`, `[BLOCKED]`, or `[WARNING]` status badges with actionable guidance.
- Returns exit code `0` on PASS and `2` on BLOCKED.

```powershell
# Run preflight verification in live mode
.\scripts\pico-preflight.ps1

# Run preflight verification targeting a specific model
.\scripts\pico-preflight.ps1 -Model "qwen3.5:2b"

# Run preflight verification in simulated mock mode (offline testing)
.\scripts\pico-preflight.ps1 -Mock

# Explicitly opt in to starting Ollama if not running (does not download models)
.\scripts\pico-preflight.ps1 -StartOllama
```

> [!NOTE]
> If Ollama is started via `-StartOllama`, PICO will check if it is already running first to avoid spawning duplicate processes. To stop the service later, run:
> ```powershell
> Stop-Process -Name "ollama" -Force
> ```

### 2. Development Verification Pipeline (`scripts/pico-verify.ps1`)

Runs the continuous integration verification gate locally without committing, pushing, or discarding any working tree changes:
1. **Python Bytecode Compilation**: Runs `python -m compileall -q pico tests tools` to ensure zero syntax errors across the workspace.
2. **Git Whitespace & Marker Integrity**: Runs `git diff --check` to catch conflict markers and trailing whitespace errors.
3. **Automated Test Suite**: Executes `pytest -v` across all unit, integration, and automation test suites.

```powershell
# Run full development verification (compilation, git diff check, and all tests)
.\scripts\pico-verify.ps1

# Run fast verification (compilation and git check only, skipping test execution)
.\scripts\pico-verify.ps1 -SkipTests
```

### 3. Safe Benchmark Wrapper (`scripts/pico-benchmark.ps1`)

Safely orchestrates hardware benchmarks:
- Always executes `pico-preflight.ps1` before initiating any benchmark.
- Halts with exit code `2` if preflight checks are BLOCKED (e.g. Ollama unreachable or model missing).
- Never switches to cloud inference or pulls models automatically.
- Supports offline mock benchmarking with prominent simulation disclaimers.
- Preserves timestamped reports under `bench_reports/` and accurately propagates exit codes.

```powershell
# Run offline simulated benchmark (exercises plumbing and report generation)
.\scripts\pico-benchmark.ps1 -Mock -Repetitions 3

# Run live hardware benchmark on local Ollama (requires preflight pass)
.\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -Repetitions 3

# Run live benchmark with custom context size and quality gates
.\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -Repetitions 5 -ContextSize 4096 -MaxTokens 128 -MinTokensPerSec 15.0 -MaxTtftMs 1200.0
```

---

## Troubleshooting & Common Failures

- **`BENCHMARK BLOCKED`**:
  - Cause: Ollama is unreachable at `http://127.0.0.1:11434` or the requested model is missing.
  - Fix: Start Ollama desktop or run `ollama serve`. If the model is missing, run `ollama run qwen3.5:2b`.
- **`PROVIDER_CONNECTION_ERROR` (503)**:
  - Cause: Ollama is selected (`DEFAULT_PROVIDER=ollama`) but the Ollama daemon is not running.
  - Fix: Start Ollama desktop or run `ollama serve` in a separate terminal (or run `.\scripts\pico-preflight.ps1 -StartOllama`).
- **`PROVIDER_MODEL_NOT_FOUND` (404)**:
  - Cause: Ollama is running but `qwen3.5:2b` has not been pulled.
  - Fix: Run `ollama pull qwen3.5:2b` manually. PICO will never trigger automatic background model downloads.
- **`PROVIDER_CONFIG_ERROR` (503)**:
  - Cause: Cloud profile was requested or routed, but `CLOUD_ENABLED` is false or `CLOUD_API_KEY` is missing.
  - Fix: Enable cloud explicitly via `CLOUD_ENABLED=true` and provide `CLOUD_API_KEY`.
- **`FOREIGN KEY constraint failed` (404/422)**:
  - Cause: Message or task created with a non-existent conversation or project ID.
  - Fix: Create parent conversation or project first.

---

## Automated Tests

Run the full automated test suite (offline, no external network calls):
```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
All 71 unit, integration, and automation tests execute using mock transports and isolated SQLite databases:
- `tests/test_automation_scripts.py` (5 tests)
- `tests/test_bench_runner.py` (9 tests)
- `tests/test_chat_stream_api.py` (8 tests)
- `tests/test_cloud_provider.py` (6 tests)
- `tests/test_conversations.py` (4 tests)
- `tests/test_foreign_keys.py` (2 tests)
- `tests/test_health.py` (1 test)
- `tests/test_messages.py` (5 tests)
- `tests/test_mock_provider.py` (4 tests)
- `tests/test_ollama_provider.py` (6 tests)
- `tests/test_persistence_reopen.py` (1 test)
- `tests/test_projects.py` (5 tests)
- `tests/test_routing_persistence.py` (1 test)
- `tests/test_routing_service.py` (9 tests)
- `tests/test_tasks.py` (5 tests)

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
- Python 3.12+ (CI matrix: Python 3.12, 3.13, 3.14; locally verified: Python 3.14.7 on Windows 11; remote CI execution unverified as no Git remote is configured)
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
- **Desktop Web UI** (when built): `http://127.0.0.1:8000/`

---

## Desktop Web Experience (Phase 10.1)

PICO ships a polished local-first desktop UI in the `frontend/` directory. It connects directly to the FastAPI backend and exposes streaming chat, workflow timelines, memory inspection, approval handling, and system status in one command-centre interface.

### Prerequisites

- **Node.js** 22+ (22 LTS recommended)
- **pnpm** 10+ (`npm install -g pnpm` or `winget install pnpm`)

### Development mode (hot-reload, proxied to backend)

```powershell
# Start the FastAPI backend first (see above)
uvicorn pico.main:app --host 127.0.0.1 --port 8000 --reload

# In a second terminal — install and start the dev server
cd frontend
pnpm install
pnpm run dev
# Opens http://localhost:5173 with proxy to http://127.0.0.1:8000
```

### Production build (served directly by FastAPI)

```powershell
cd frontend
pnpm run build          # Outputs to frontend/dist/
# FastAPI auto-serves frontend/dist when dist/assets/ exists
uvicorn pico.main:app --host 127.0.0.1 --port 8000
# Now visit http://127.0.0.1:8000/
```

### Frontend test suite

```powershell
cd frontend
pnpm run test           # Vitest — all 20 frontend tests
pnpm run typecheck      # TypeScript strict type check
```

### Local operator pairing (for consequential approvals)

Consequential tool-execution approvals require a verified local operator session. No `APPROVAL_SECRET_TOKEN` is transmitted to the browser.

```powershell
# With the backend running, generate a one-time pairing code:
python -m pico.cli.pair
# Enter the 6-digit code shown in the desktop UI's pairing modal.
# The HttpOnly session cookie is set; CSRF token is returned to the browser.
```

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

## Home Assistant Integration (Milestones P7.1, P7.2 & P7.3)

PICO integrates with Home Assistant as a local smart-home hub for situational awareness and bounded, verified physical actuation:
- **Strictly Disabled by Default**:
  - Read-Only: Set `HOME_ASSISTANT_ENABLED=true`, `HOME_ASSISTANT_BASE_URL`, and `HOME_ASSISTANT_ACCESS_TOKEN` in `.env` to enable read-only discovery.
  - Physical Actuation: Set `HOME_ASSISTANT_ACTUATION_ENABLED=true` and `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID` (must be a single entity in the `light` domain).
- **REST & Pure-Python WebSocket Clients (Stage A Read-Only Awareness)**:
  - `GET /api/homeassistant/status` — Inspect integration health and connection state.
  - `GET /api/homeassistant/entities` — Discover entities and reported states with sanitized attributes.
  - `GET /api/homeassistant/entities/{entity_id}` — Inspect a specific entity.
  - `GET /api/homeassistant/overview` — Bounded smart-home summary for assistant awareness.
  - WebSocket command allowlist strictly enforced: `get_states`, `get_config`, `ping`. Zero mutation over WebSocket.
- **First Verified Physical Action (Milestone P7.3 Single Test Light)**:
  - `POST /api/homeassistant/actuation/plan` — Strictly read-only planning endpoint to inspect proposed actuation without side effects.
  - `POST /api/homeassistant/actuation/propose` — Proposes an actuation execution requiring valid `X-Pico-Approval-Token`.
  - `GET /api/homeassistant/actuation/{execution_id}` — Inspects actuation execution status and verification proof.
  - Dedicated capability `homeassistant.test_light_actuate` with risk tier `HIGH` and `consequential=True`.
- **Fail-Closed Physical Actuation Guardrails**:
  - **Single Entity & Actions**: Only controls `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID` with `turn_on` or `turn_off`. Any other entity or action (including `toggle` or non-light domains) fails closed.
  - **Server-Side Token Authentication**: Physical proposal, approval, and execution require `X-Pico-Approval-Token` matching server `APPROVAL_SECRET_TOKEN`. Loopback callers are NOT exempt.
  - **Dedicated Permission**: Requires `homeassistant:actuate`, which is strictly excluded from generic default tool permissions.
  - **Triple-Digest Integrity**: Persisted input recomputed SHA-256 == stored execution digest == approval bound digest verified immediately before mutation dispatch.
  - **Single-Use Replay Protection**: Approvals are single-use; any duplicate or replayed dispatch fails closed with `AuthorizationError`.
  - **Zero Blind Retries**: Independent read-after-write verification polling via read-only state API; never automatically retries a mutation on timeout or failure.
  - **Trusted Privacy Gate**: Household data defaults to `INTERNAL` (or `SENSITIVE` for locks, alarms, cameras, and entry points). Never sent to cloud models.

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
All 89 unit, integration, and automation tests execute using mock transports and isolated SQLite databases:
- `tests/test_automation_scripts.py` (5 tests)
- `tests/test_bench_runner.py` (9 tests)
- `tests/test_chat_stream_api.py` (11 tests)
- `tests/test_cloud_provider.py` (10 tests)
- `tests/test_conversations.py` (4 tests)
- `tests/test_foreign_keys.py` (2 tests)
- `tests/test_health.py` (1 test)
- `tests/test_messages.py` (5 tests)
- `tests/test_mock_provider.py` (4 tests)
- `tests/test_ollama_provider.py` (17 tests)
- `tests/test_persistence_reopen.py` (1 test)
- `tests/test_projects.py` (5 tests)
- `tests/test_routing_persistence.py` (1 test)
- `tests/test_routing_service.py` (9 tests)
- `tests/test_tasks.py` (5 tests)

<div align="center">
  <img src="assets/brand/pico-logo.jpg" alt="PICO — Local-first Personal Intelligence" width="680" />
  <p><strong>A personal intelligence system built around your machine, your data, and your control.</strong></p>
  <p>
    <a href="#-what-is-pico">Overview</a> ·
    <a href="#-quickstart">Quickstart</a> ·
    <a href="#-capabilities">Capabilities</a> ·
    <a href="#-security-by-design">Security</a> ·
    <a href="#-documentation">Documentation</a>
  </p>
</div>

---

## What is PICO?

**PICO (Personal Intelligence Coordinator & Operator)** is a local-first, privacy-first personal intelligence system. It coordinates specialized components for perception, memory, model routing, computer control, home automation, and multi-step workflows.

PICO is **not one all-knowing model with unrestricted access**. It is an orchestrated system: models reason, deterministic services enforce policy, tools operate within explicit boundaries, and independent verification checks what actually happened.

> **The guiding idea:** keep private context and core control local; make every consequential action bounded, reviewable, and verifiable.

**Current foundation:** Python 3.12+ · FastAPI · SQLite · React · TypeScript · Vite · Ollama

The canonical milestone tracker is [docs/implementation-plan.md](docs/implementation-plan.md). It records what is implemented, verified, in progress, and still planned.

## Capabilities

| Area | What is in the codebase |
|---|---|
| **Context & memory** | A privacy-aware context compiler, persistent conversations and tasks, and working, episodic, semantic, and procedural memory with provenance and controlled approval. |
| **Model routing** | A provider-agnostic gateway with per-step routing, local Ollama support, a deterministic offline mock provider, and a strict cloud boundary. |
| **Perception** | Push-to-talk audio, on-demand visual inspection, motion-event detection, and normalized environmental/sensor events. |
| **Computer control** | Bounded Windows application/window automation, staged filesystem changes, and isolated browser inspection. |
| **Home automation** | Home Assistant awareness plus narrowly allowlisted light, switch, and climate operations with explicit approvals and post-action verification. |
| **Workflow coordination** | Persistent task graphs, logical worker roles, single-model resource scheduling, independent output verification, and controlled compensation. |
| **Desktop experience** | A local web interface for streaming chat, push-to-talk voice, memory, workflows, approvals, and system status. |

### Important capability boundaries

- The current voice interface is **push-to-talk**. It is not an always-listening wake-word assistant.
- The desktop experience is a **local web UI** served by the FastAPI backend; it is not yet a packaged native Windows desktop application.
- Browser automation is intentionally bounded and isolated. It is not unrestricted control of every website or browser session.
- Local inference can work without a cloud model, but internet-backed services (for example, YouTube) still require an internet connection.
- Smart-home actuation is disabled by default and limited to the operations explicitly configured by the operator.

## Quickstart

The primary development target is **Windows 11 with PowerShell**.

### Prerequisites

- Python 3.12 or newer
- Windows PowerShell
- Optional frontend: Node.js 22+ and pnpm 10+

### 1. Create the environment and install dependencies

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create a local environment file from the template when you need to override defaults:

```powershell
Copy-Item .env.example .env
```

Review <code>.env.example</code> before adding credentials. Never commit <code>.env</code> or real secrets.

### 2. Prepare the database

```powershell
alembic upgrade head

# Optional: seed deterministic local development data
python -m pico.db.seed
```

### 3. Start the backend

```powershell
uvicorn pico.main:app --host 127.0.0.1 --port 8000 --reload
```

When the server is running:

| URL | Purpose |
|---|---|
| [http://127.0.0.1:8000/](http://127.0.0.1:8000/) | Local desktop interface, when the frontend has been built |
| [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) | Interactive API documentation |
| [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) | Backend health check |

## Desktop experience

PICO's frontend lives in <code>frontend/</code>. It connects to the local FastAPI backend and brings the primary interaction surfaces together: streaming chat, push-to-talk voice, workflow timelines, memory inspection, approvals, and system status.

### Run the frontend in development mode

Keep the backend running in one terminal. In a second PowerShell terminal:

```powershell
cd frontend
pnpm install
pnpm run dev
```

Vite serves the UI at <code>http://localhost:5173</code> and proxies API requests to <code>http://127.0.0.1:8000</code>.

### Build the frontend for local serving

```powershell
cd frontend
pnpm run build
cd ..
uvicorn pico.main:app --host 127.0.0.1 --port 8000
```

FastAPI serves <code>frontend/dist/</code> when the production assets are present.

### Pair a local operator

Consequential approvals and privileged operations require a verified operator session. Pairing is initiated by the trusted local CLI—not by the browser.

```powershell
python -m pico.cli.pair
```

The CLI prints a one-time six-digit challenge. Enter it in the desktop UI's pairing dialog.

- Challenges are stored in SQLite, single-use, salt-hashed, and expire after 300 seconds.
- Five consecutive failed attempts trigger lockout.
- The browser never receives <code>APPROVAL_SECRET_TOKEN</code>.
- Successful pairing establishes an <code>HttpOnly; SameSite=Strict</code> session cookie and a separate CSRF token.
- Unpaired users retain read-only access to safe views; consequential actions remain locked.

## Model providers

PICO uses a provider-agnostic model gateway. The default **mock** provider is deterministic and offline, making it useful for development without a model runtime. **Mock mode is not an LLM.**

### Use local Ollama

PICO does not download models automatically. Install Ollama and pull a model yourself, then select it:

```powershell
# Check the local Ollama service and installed models
curl.exe -s http://127.0.0.1:11434/api/tags
ollama list

# Pull manually only if the model is not already installed
ollama pull qwen3.5:2b

# Select local inference for this PowerShell session
$env:DEFAULT_PROVIDER = "ollama"
$env:OLLAMA_MODEL = "qwen3.5:2b"
```

### Enable cloud inference only by explicit choice

Cloud inference is disabled by default. When explicitly enabled, **only <code>PUBLIC</code> context may leave the machine**.

```powershell
$env:CLOUD_ENABLED = "true"
$env:CLOUD_BASE_URL = "https://api.openai.com/v1"
$env:CLOUD_MODEL = "gpt-4o-mini"
$env:CLOUD_API_KEY = "replace-with-your-key"
```

The effective classification of assembled context follows the highest-sensitivity contributing content. <code>INTERNAL</code>, <code>SENSITIVE</code>, <code>SECRET</code>, and missing or unknown classifications stay pinned to local inference—even when cloud access is enabled.

## Capabilities in more detail

<details>
<summary><strong>Perception: voice, vision, motion, and sensors</strong></summary>

- **Audio:** explicit push-to-talk capture with provider-neutral speech-to-text and text-to-speech adapters, including local adapters. Capture state is observable; temporary PCM buffers are disposed promptly.
- **Vision:** on-demand frame ingestion requires explicit user consent. Payload size and image dimensions are bounded, and transient frame buffers are released.
- **Motion:** a layered adapter supports PIR presence events, deterministic CPU frame-difference analysis, and cross-modal event correlation.
- **Environmental events:** modalities share a normalized perception-event contract with validation, per-sensor deduplication, cooldowns, and read-only boundaries.

Microphone and camera capture are not silently activated. See [docs/architecture.md](docs/architecture.md) and the perception modules under <code>pico/perception/</code>.

</details>

<details>
<summary><strong>Computer and workspace control</strong></summary>

PICO's computer-control layer is provider-neutral and deliberately constrained:

- **Windows/application automation:** bounded window inspection and server-configured application allowlists. Launches are consequential, require approval, use direct argument lists rather than shell execution, and do not retry automatically.
- **Filesystem operations:** stage and inspect text diffs before applying them or rolling them back. Paths must remain inside configured roots; writes are digest-bound to approval and independently verified. Windows mutations use handle-bound operations to resist path and file-identity races.
- **Browser inspection:** optional Playwright support with an empty-by-default origin allowlist, isolated ephemeral contexts, SSRF protections, bounded observations, and no persistence of browser credentials or sessions.

These capabilities are not a general-purpose remote-control backdoor. See [docs/security-and-risk-register.md](docs/security-and-risk-register.md) for the trust boundaries.

</details>

<details>
<summary><strong>Home Assistant and physical-world controls</strong></summary>

PICO integrates with Home Assistant through authenticated REST and a pure-Python WebSocket client.

**Read-only awareness**
- <code>GET /api/homeassistant/status</code>
- <code>GET /api/homeassistant/entities</code>
- <code>GET /api/homeassistant/entities/{entity_id}</code>
- <code>GET /api/homeassistant/overview</code>
- WebSocket commands are restricted to <code>get_states</code>, <code>get_config</code>, and <code>ping</code>.

**Verified, allowlisted operations**
- A single configured test light may be switched on or off.
- Explicitly allowlisted switches/plugs may be switched on or off.
- Explicitly allowlisted climate entities support verified single-setpoint and dual-setpoint temperature operations, subject to their supported features, active HVAC mode, reported limits, units, and trusted server-configured step-grid origin.

**Not supported:** arbitrary Home Assistant service calls, generic <code>toggle</code>, automatic HVAC mode changes, fan-mode changes, or preset activation.

Physical actuation is disabled by default. It requires the independent configuration gates, exact entity allowlists, the server-side approval secret, the appropriate permission, a single-use human approval, digest checks immediately before dispatch, and independent read-after-write verification. Mutations are never blindly retried. PICO does not claim that a successful API response proves physical device connectivity.

Relevant configuration includes:
- <code>HOME_ASSISTANT_ENABLED</code>
- <code>HOME_ASSISTANT_ACTUATION_ENABLED</code>
- <code>HOME_ASSISTANT_SWITCH_ALLOWLIST</code>
- <code>HOME_ASSISTANT_CLIMATE_ALLOWLIST</code>
- <code>HOME_ASSISTANT_CLIMATE_GRID_ORIGINS</code>
- <code>HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED</code>
- <code>HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED</code>
- <code>APPROVAL_SECRET_TOKEN</code>

Readiness checks are available before enabling live operations. See [docs/implementation-plan.md](docs/implementation-plan.md) for the exact P7 scope and verification history.

</details>

<details>
<summary><strong>Memory, workflows, and independent verification</strong></summary>

- **Memory:** working, episodic, semantic, and procedural stores. Retrieved content carries provenance and classification; proposed facts are distinct from approved knowledge. Procedural skills have a reviewed and approved lifecycle.
- **Workflow coordination:** persistent task graphs validate dependencies before execution and run eligible steps through the capability registry and execution engine.
- **Scheduling:** logical worker roles share a single configured resident local model through bounded, priority-aware scheduling. Queue limits, timeouts, cancellation, and resource leases prevent uncontrolled model contention.
- **Independent verification:** deterministic structure and execution-evidence checks gate downstream work. Model-assisted review may add semantic feedback but cannot override failed deterministic checks.
- **Recovery:** interrupted workflows are reconciled against persisted execution records and the event ledger; consequential compensations require approval.

</details>

## Security by design

PICO is built around explicit trust boundaries rather than convenience-first automation.

| Invariant | Meaning |
|---|---|
| **Local-first routing** | Cloud access is opt-in; only <code>PUBLIC</code> effective context may be sent to a cloud provider. |
| **Fail-closed permissions** | Unknown, missing, or untrusted authority does not grant access. Capabilities declare permissions, risk, schemas, and execution limits. |
| **Human approval** | Consequential actions require a verified operator or approved token path, with approval bound to the exact input digest. |
| **Single-use dispatch** | Atomic claims and replay protection prevent duplicate consequential dispatch. Automatic retries are disabled where they could repeat side effects. |
| **Independent verification** | PICO checks authoritative state or output evidence before reporting success. Uncertain outcomes remain uncertain until reconciled. |
| **Auditable execution** | Lifecycle events are recorded in an append-only event ledger with secret redaction. |

### Offline and degraded operation

PICO's offline behavior is designed to fail honestly and avoid duplicate side effects:

- The service worker caches only the static application shell and approved build assets. API responses, private conversations, memory, execution records, pairing tokens, cookies, CSRF tokens, and approval state are not cached.
- API requests remain network-only. The service worker never queues or replays <code>POST</code>, <code>PUT</code>, or <code>DELETE</code> requests.
- An in-memory draft outbox can retain up to 25 unsent messages during a backend outage. Drafts are never auto-submitted after reconnection; the user must review and send them.
- Interrupted streams and ambiguous mutations are marked as interrupted or **Outcome unknown**. PICO does not blindly retry them.
- The UI distinguishes backend connectivity from local inference health and revalidates the operator session after reconnection.

## Benchmarks and runtime tools

### Local model benchmarks

The benchmark runner in <code>tools/bench/</code> measures time to first token (TTFT), tokens per second, RAM, and GPU VRAM.

```powershell
# Offline plumbing/report test — not a hardware performance result
.\.venv\Scripts\python.exe -m tools.bench.runner --mock --repetitions 3 --output-dir bench_reports

# Live benchmark after Ollama and the model are installed
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 3 --output-dir bench_reports

# Custom context/output limits and quality gates
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 5 --context-size 4096 --max-tokens 128
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 3 --min-tokens-per-sec 15.0 --max-ttft-ms 1200.0
```

Mock benchmarking checks runner behavior and report generation only. It must not be described as a completed hardware evaluation.

Reports are written to <code>bench_reports/</code> as timestamped Markdown summaries and machine-readable JSON logs.

<details>
<summary><strong>PowerShell safety and verification scripts</strong></summary>

**Runtime preflight — <code>scripts/pico-preflight.ps1</code>**

Checks Ollama availability, installed model presence, system RAM, and dedicated GPU VRAM. It returns explicit <code>PASS</code>, <code>BLOCKED</code>, or <code>WARNING</code> states and never downloads a model.

```powershell
.\scripts\pico-preflight.ps1
.\scripts\pico-preflight.ps1 -Model "qwen3.5:2b"
.\scripts\pico-preflight.ps1 -Mock

# Starts Ollama only when explicitly requested; does not download models
.\scripts\pico-preflight.ps1 -StartOllama
```

**Development verification — <code>scripts/pico-verify.ps1</code>**

Runs Python bytecode compilation, <code>git diff --check</code>, and the backend test suite. It does not commit, push, or discard working-tree changes.

```powershell
.\scripts\pico-verify.ps1

# Fast checks without running tests
.\scripts\pico-verify.ps1 -SkipTests
```

**Safe benchmark wrapper — <code>scripts/pico-benchmark.ps1</code>**

Runs preflight before a live benchmark, stops when prerequisites are blocked, never silently falls back to cloud inference, and preserves timestamped reports.

```powershell
.\scripts\pico-benchmark.ps1 -Mock -Repetitions 3
.\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -Repetitions 3
.\scripts\pico-benchmark.ps1 -Model "qwen3.5:2b" -Repetitions 5 -ContextSize 4096 -MaxTokens 128 -MinTokensPerSec 15.0 -MaxTtftMs 1200.0
```

</details>

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| <code>BENCHMARK BLOCKED</code> | Ollama is unreachable or the selected model is missing. | Start Ollama; check <code>ollama list</code>; install the model manually if needed. |
| <code>PROVIDER_CONNECTION_ERROR</code> (503) | <code>DEFAULT_PROVIDER=ollama</code>, but the Ollama service is not running. | Start Ollama Desktop or run <code>ollama serve</code>. |
| <code>PROVIDER_MODEL_NOT_FOUND</code> (404) | Ollama is running, but the configured model is not installed. | Run <code>ollama pull qwen3.5:2b</code> manually. |
| <code>PROVIDER_CONFIG_ERROR</code> (503) | Cloud inference was selected without explicit enablement or a usable key. | Check <code>CLOUD_ENABLED</code> and <code>CLOUD_API_KEY</code>; keep cloud disabled unless intended. |
| Foreign-key error | A message or task references a missing conversation or project. | Create the parent conversation or project first. |

## Run the tests

PICO's test suites are designed for offline execution with deterministic mock transports, synthetic OS/filesystem/browser adapters, and isolated temporary SQLite databases.

```powershell
# Backend
.\.venv\Scripts\pytest.exe

# Frontend
pnpm --dir frontend run test
pnpm --dir frontend run typecheck
pnpm --dir frontend run build

# Full local verification gate
powershell -ExecutionPolicy Bypass -File .\scripts\pico-verify.ps1
```

**Last recorded P10.3.3 verification snapshot:** 723 backend tests and 125 frontend tests passing; CI covers Python 3.12, 3.13, and 3.14. Test counts evolve, so run the commands above for the current result.

## Documentation

| Document | What it covers |
|---|---|
| [AGENTS.md](AGENTS.md) | Non-negotiable engineering rules, hardware constraints, and contributor guidance. |
| [Architecture](docs/architecture.md) | System boundaries, components, and request/data flow. |
| [Data model](docs/data-model.md) | SQLite persistence, relational models, WAL configuration, and migrations. |
| [API contract](docs/api-contract.md) | REST endpoints, schemas, validation, and error envelopes. |
| [Security and risk register](docs/security-and-risk-register.md) | Threat model, trust boundaries, and mitigations. |
| [Implementation plan](docs/implementation-plan.md) | Canonical milestone status, verified scope, and remaining roadmap. |
| [Model gateway design](docs/model-gateway-design.md) | Per-step routing, model profiles, and data-classification policy. |
| [Evaluation framework](docs/evaluation/README.md) | Evaluation strategy and representative test cases. |

---

<div align="center">
  <sub>Built local-first. Designed to be inspectable. Governed by explicit permission.</sub>
</div>

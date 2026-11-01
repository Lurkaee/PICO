<div align="center">
  <img src="assets/brand/pico-logo.jpg" alt="PICO — Local-first Personal Intelligence" width="400" />
  <p><strong>A personal intelligence system built around your machine, your data, and your control.</strong></p>
  <p>
    <a href="#what-is-pico">Overview</a> ·
    <a href="#quickstart">Quickstart</a> ·
    <a href="#capabilities">Capabilities</a> ·
    <a href="#security-by-design">Security</a> ·
    <a href="#documentation">Documentation</a>
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

<details>
<summary><strong>Operator authorization states</strong></summary>

The desktop UI distinguishes these states instead of treating every session as simply signed in or signed out:

1. **Unpaired / Read-only:** safe views remain available; consequential approval controls stay locked.
2. **Pairing required:** consequential proposals wait in <code>awaiting_approval</code> until the local operator completes CLI pairing.
3. **Authenticated operator session:** a verified local operator can review and approve consequential actions bound to their canonical input digest.
4. **Expired or revoked session:** approval controls lock again until a new pairing ceremony succeeds.
5. **Unauthorized action:** a request without the required capability permission fails closed, with no side effect.
6. **Authorized pending approval:** an eligible consequential step remains paused until the human operator explicitly approves it.

</details>

## Model providers

PICO uses a provider-agnostic model gateway. The default **mock** provider is deterministic and offline, making it useful for development without a model runtime. **Mock mode is not an LLM.**

<details>
<summary><strong>Exercise streaming chat with the offline mock provider</strong></summary>

The default <code>mock</code> provider is useful for checking the conversation and streaming API without Ollama, credentials, or cloud access. It returns deterministic development responses; it is not a local language model.

With the backend running, execute this from PowerShell:

`§§powershell
$conv = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/conversations" -Method Post -ContentType "application/json" -Body '{"title": "Demo Thread"}'

$body = @{
    conversation_id = $conv.id
    message = "Explain PICO local persistence."
    data_classification = "INTERNAL"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/chat/stream" -Method Post -ContentType "application/json" -Body $body
§§§

</details>

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



**API surface**

| Scope | Endpoint | Purpose |
|---|---|---|
| Read-only | <code>GET /api/homeassistant/status</code> | Integration health and connection status |
| Read-only | <code>GET /api/homeassistant/entities</code> | Bounded entity discovery |
| Read-only | <code>GET /api/homeassistant/entities/{entity_id}</code> | Inspect one entity |
| Read-only | <code>GET /api/homeassistant/overview</code> | Bounded home overview |
| Test light | <code>POST /api/homeassistant/actuation/plan</code> | Read-only actuation plan |
| Test light | <code>POST /api/homeassistant/actuation/propose</code> | Propose an allowlisted light operation |
| Test light | <code>GET /api/homeassistant/actuation/readiness</code> | Readiness inspection |
| Test light | <code>GET /api/homeassistant/actuation/{execution_id}</code> | Inspect execution and verification |
| Switch | <code>POST /api/homeassistant/switch/plan</code> | Plan a switch operation without mutation |
| Switch | <code>POST /api/homeassistant/switch/propose</code> | Propose an allowlisted switch operation |
| Switch | <code>GET /api/homeassistant/switch/readiness</code> / <code>GET /api/homeassistant/switch/{execution_id}</code> | Readiness and execution inspection |
| Climate | <code>POST /api/homeassistant/climate/plan</code> | Plan a single-setpoint operation without mutation |
| Climate | <code>GET /api/homeassistant/climate/observe/{entity_id}</code> / <code>GET /api/homeassistant/climate/readiness</code> | Observe an entity and inspect readiness |
| Climate | <code>POST /api/homeassistant/climate/propose</code> | Propose a single-setpoint temperature operation |
| Climate | <code>GET /api/homeassistant/climate/execution/{execution_id}</code> | Inspect single-setpoint execution |
| Dual-setpoint climate | <code>POST /api/homeassistant/climate/dual-setpoint/plan</code> | Plan a dual-setpoint operation without mutation |
| Dual-setpoint climate | <code>POST /api/homeassistant/climate/dual-setpoint/propose</code> | Propose a dual-setpoint temperature operation |
| Dual-setpoint climate | <code>GET /api/homeassistant/climate/dual-setpoint/execution/{execution_id}</code> | Inspect dual-setpoint execution |

The WebSocket command allowlist remains read-only: <code>get_states</code>, <code>get_config</code>, and <code>ping</code>. Readiness can also be inspected locally with <code>python -m pico.homeassistant.readiness</code>, adding <code>--switch</code> or <code>--climate</code> for those domains.

Local loopback proposals can use the verified paired operator session. Remote/headless privileged calls use the approved token path. All consequential operations remain subject to the server-side configuration gates and capability permissions.

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

<details>
<summary><strong>Offline behavior and recovery checks</strong></summary>

The offline shell is deliberately narrow: the service worker caches the application shell (<code>/</code>, <code>/index.html</code>) and approved static build assets (<code>/assets/*</code>) only. It does not persist private conversations, messages or drafts, memory records, tool inputs/results, pairing material, cookies, CSRF tokens, approval tickets, or workflow state. Dynamic API requests remain network-only.

When a message cannot be sent, the UI may retain up to 25 unsent drafts in active memory. They can be reviewed, edited, discarded, or explicitly sent after recovery. They are not written to browser storage and disappear when the page is refreshed or closed. Reconnection never auto-submits drafts.

Interrupted streams display <code>[Response stream interrupted]</code>. Ambiguous mutations—such as approvals, execution dispatch, workflow advances, compensations, memory-forget requests, and audio-session operations—are marked <strong>Outcome unknown</strong> and are not blindly retried. The UI reconciles against authoritative server state before unlocking the affected control. On recovery it revalidates the local operator session through <code>GET /api/operator/status</code>.

Useful manual checks:

- Stop the backend while the frontend is open: the UI should show backend disconnection, preserve the current in-memory view, and lock consequential controls.
- Keep the backend running but stop Ollama: the UI should report inference as degraded/unavailable rather than claiming the whole backend is offline.
- Restore connectivity: drafts should remain explicit user actions; consequential controls should stay locked until the operator session is revalidated.

</details>

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

Benchmark output is stored under <code>bench_reports/</code> with unique UTC timestamps:
- Markdown summaries include medians, ranges, memory telemetry, and quality-gate evaluations.
- JSON reports retain machine-readable per-run measurements for inspection.



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



The preflight script probes the Ollama CLI and local API, checks whether the selected model is already installed, and reports system RAM and dedicated GPU VRAM (when <code>nvidia-smi</code> is available). It returns exit code <code>0</code> for <code>PASS</code> and <code>2</code> for <code>BLOCKED</code>. <code>-StartOllama</code> is an explicit opt-in to start the local service; it does not download models.

The verification script compiles <code>pico</code>, <code>tests</code>, and <code>tools</code>, runs <code>git diff --check</code>, and executes the backend tests. Use <code>-SkipTests</code> for the faster compile/whitespace checks only.

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

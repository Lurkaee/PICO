# PICO Architecture & Dependency Audit Findings
**Execution Date:** 2026-11-20  
**Repository:** `https://github.com/Lurkaee/PICO`  
**Agent Role:** Second Antigravity engineering agent (Repository Governance & Stabilization)  
**Standard:** Canonical Architecture Contract (`docs/architecture.md`) & Milestone Plan (`docs/implementation-plan.md`)

---

## 1. Executive Evaluation
The PICO codebase strictly implements its documented architecture:
1. **Local-First, Privacy-First Invariant:** Rigorously upheld across context assembly, model routing, and provider dispatch.
2. **Fail-Closed Gate:** Zero silent cloud fallbacks. Non-`PUBLIC` context is strictly barred from remote endpoints.
3. **Multi-Component Modularity:** PICO remains an orchestrated system of distinct, replaceable components, never collapsing into a monolithic model wrapper.
4. **Independent Verification:** No capability reports success merely because a tool was invoked; postcondition evidence is mandatory.
5. **No Architectural Regressions:** All core contracts, ADRs 0001–0006, and security boundaries remain fully intact.

---

## 2. Subsystem-by-Subsystem Audit Findings

### 2.1 Context Compilation & Data Classification (Milestone P1, P4)
- **Implementation Path:** `pico/services/context_compiler.py`, `pico/schemas/context.py`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - Context items carry strict provenance (`msg:`, `proj:`, `task:`, `system:`, `mem:`).
  - Effective context sensitivity evaluates across user inputs, conversation history, memory, retrieval, and tool outputs.
  - Hierarchy strictly enforced: `SECRET` > `SENSITIVE` > `INTERNAL` > `PUBLIC`.
  - Non-downgrade invariant verified: transform operations cannot lower a higher classification to `PUBLIC`.
  - Budgeting: `conservative-prompt-budget-v1` clamps prompt ceiling to 3,328 tokens on a 4,096 context window, preserving 512 completion tokens and 256 safety headroom tokens. ChatML (5 tokens/msg) and OpenAI (3 tokens/msg) message framing overheads are explicitly budgeted.

### 2.2 Privacy Gate & Provider Call Gateway (Milestones P1, B1)
- **Implementation Path:** `pico/services/routing_service.py`, `pico/providers/`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - `RULE_PRIVATE_LOCAL_PIN` strictly enforced at Router, SSE chat endpoint, and inside `CloudProvider.stream_chat`.
  - Fail-closed behavior: Any missing, malformed, or non-`PUBLIC` classification results in immediate local pinning or `PrivacyViolationError`.
  - Provider Default: Deterministic `mock` provider enabled by default for zero-dependency CI and local testing.
  - Ollama Adapter: Enforces `GenerationOptions.think` precedence: explicit option > provider default (`think=False`) > omitted.
  - Cloud Adapter: OpenAI-compatible provider strictly requires `PUBLIC` context AND explicit opt-in (`CLOUD_ENABLED=true`).

### 2.3 Memory Subsystem & Relational Persistence (Milestones P2, P3)
- **Implementation Path:** `pico/db/`, `pico/models/`, `pico/repositories/`, `pico/services/memory_service.py`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - SQLite WAL mode and foreign key constraints strictly enforced across all database connections.
  - Four distinct memory layers operational:
    1. *Working Memory:* Bounded, session-scoped active task state.
    2. *Episodic Memory:* Time-indexed historical event log, outcomes, and failure traces.
    3. *Semantic Memory:* Source-grounded knowledge with SQLite FTS5 full-text indexing.
    4. *Procedural Memory:* Reusable skills and workflow templates with draft/review/approval/activation state machines.
  - Database migrations: Version-controlled via Alembic (migrations `0001` through `0004` verified reproducible).

### 2.4 Capability Registry & Policy A Authorization (Milestones P5, P10.3)
- **Implementation Path:** `pico/capabilities/`, `pico/services/capability_service.py`, `pico/services/operator_service.py`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - Capability allowlist: Tools must be explicitly registered and bound to risk tiers (`READ_ONLY`, `MUTATION_REVERSIBLE`, `CONSEQUENTIAL`).
  - Safe operations (`READ_ONLY`) auto-dispatch within bounded resource limits.
  - Consequential operations halt at `awaiting_approval`. Execution requires human operator confirmation via Policy A.
  - Operator Pairing: Single-use pairing challenge generated on CLI; operator session tokens cryptographically bound to caller profile with replay protection.
  - Triple-Digest Gate: Consequential physical actuation (e.g. Home Assistant switch/climate) requires matching input digest, execution claim, and operator approval tokens.

### 2.5 Sandboxed Execution Adapters & Independent Verification (Milestones P7, P8, P9)
- **Implementation Path:** `pico/os/`, `pico/filesystem/`, `pico/browser/`, `pico/homeassistant/`, `pico/services/workflow_service.py`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - Filesystem: Reversible operations with staged diffs; bounded within approved project root.
  - Browser: Sandboxed Playwright automation with ephemeral browsing contexts and origin allowlists.
  - Home Assistant: Read-only entity discovery baseline; actuation restricted to Stage D verified test fixtures (`P7.3` single test light, `P7.4` climate setpoint planning/actuation).
  - Workflow Synthesis: Typed DAG execution engine with Kahn acyclicity check, step-level verification gates, and compensation handlers.
  - Independent Verification: Postcondition verification executed against authoritative external evidence (HTTP response codes, DOM elements, device state attributes).

### 2.6 Desktop Shell & Native Integration (Milestone P11.2)
- **Implementation Path:** `frontend/src-tauri/`, `frontend/src/`
- **Audit Result:** **CONFORMS STRICTLY.**
- **Details:**
  - Tauri 2 + Rust native desktop shell (`frontend/src-tauri`).
  - Secure loopback bridge: Desktop companion communicates with local backend via authenticated loopback bridge.
  - Expressive Animated Eyes (`P11.2.4.3`): EMO-inspired visual evolution with 11 distinct companion emotional states (`neutral`, `blink`, `focused_verifying`, `waking_up_starting`, `curious`, `attentive`, `happy`, `confused`, `concerned_error`, `resting`, `thinking`).
  - Windows NSIS Packaging: CPython 3.14 runtime isolation and SHA-256 verified wheel provisioning.

### 2.7 Voice Subsystem (Milestone P11.3)
- **Implementation Path:** `pico/services/audio_capture.py`, `docs/evaluation/voice-engine-qualification.md`
- **Audit Result:** **ACTIVE QUALIFICATION IN PROGRESS.**
- **Details:**
  - Local Audio Capture abstraction complete: strict WAV format validation, fail-closed STT delivery, zero cloud audio streaming.
  - Stage 1 Engine Selection: Hardware qualification completed and documented.
  - Stage 2 Prototype: Native whisper.cpp CPU qualification prototype merged (`PR #93`).
  - Stage 3 Promotion Rules: Architectural decision record `0006` approved; promotion to default voice engine gated behind passing command corpus accuracy threshold (GATE-VOICE-09).

---

## 3. Dependency Management & Lockfile Audit

### 3.1 Backend Dependencies
- **Manifest:** `pyproject.toml`, `requirements.txt`
- **Runtime Lockfile:** `tools/packaging/requirements-windows-runtime.lock`
- **Audit Result:** **SYNCHRONIZED & CONSISTENT.**
- **Details:**
  - FastAPI pinned to `0.163.1`, pydantic-settings pinned to `2.16.3`.
  - Both packages synchronized in `requirements.txt` and `requirements-windows-runtime.lock` via `PR #102`.
  - Lockfile contains 26 entries with verified PyPI SHA-256 hashes for Windows AMD64 runtime wheels.
  - Pytest and development utilities strictly excluded from production runtime lockfile.

### 3.2 Frontend Dependencies
- **Manifest:** `frontend/package.json`
- **Lockfile:** `frontend/pnpm-lock.yaml`
- **Audit Result:** **SYNCHRONIZED & CONSISTENT.**
- **Details:**
  - Pinned pnpm 10/12 configuration.
  - React 19.3.1, Vite 8.3.8, Vitest 5.1.0, TypeScript 7.0.2.
  - Typecheck (`tsc -b --noEmit`) passes with 0 errors.
  - Production build (`vite build --mode packaged-production`) compiles cleanly in 2.22s.

---

## 4. Discrepancies, Risks & Architectural Recommendations

| ID | Finding Category | Severity | Description | Verified Resolution / Recommendation |
| :--- | :--- | :--- | :--- | :--- |
| **DISC-01** | Documentation Sync | Low | `README.md` previously cited outdated test counts (795 backend / 144 frontend) and stopped milestone status at P10.3/P11.1. | Updated `README.md` to reflect actual 1,060 backend tests, 197 frontend tests, and P11.2/P11.3 status. |
| **DISC-02** | Character Encoding | Low | `README.md` contained UTF-8 mojibake (`â€”`, `â”‚`, `â–¼`) from previous encoding mismatches. | Cleaned up all mangled characters and re-encoded `README.md` cleanly. |
| **RISK-01** | Script Virtualenv Resolution | Medium | PowerShell wrapper `scripts/pico-benchmark.ps1` checks `..\.venv\Scripts\python.exe` relative to script location. In isolated git worktrees, this path resolves to worktree root rather than main checkout. | Set `$env:VIRTUAL_ENV` during worktree test runs; recommend enhancing script to check `git rev-parse --git-common-dir` or parent worktree paths. |
| **RISK-02** | Local Scratch Build Artifacts | Low | Desktop workspace contains 30+ uncommitted Python scratch scripts and CSS drafts from recent recovery work. | Retained safely in local workspace; ensured they are not committed to `main`. |

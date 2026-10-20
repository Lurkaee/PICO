# AGENTS.md — PICO Engineering Rules & Operating Principles

Welcome to **PICO** (Personal Intelligence Coordinator & Operator), a local-first, multi-model personal intelligence system.

Every agent, developer, and automated assistant contributing to this repository MUST strictly follow the non-negotiable principles, operational invariants, and security rules defined below.

---

## 1. Non-Negotiable Core Principles

1. **Personal Intelligence System, Not a Chatbot Wrapper**
   PICO is an autonomous, reliable coordinator for knowledge, context, memory, tasks, and tool execution. It is never to be diluted into a generic chatbot or a simplistic multi-agent wrapper.

2. **Model-Provider Agnostic**
   PICO Core does not depend on any single AI provider or proprietary API. The system communicates through a clean, provider-independent model gateway interface (`StreamingResponse`, `StructuredOutput`, `ToolProposals`, `Metadata`). Switching or upgrading providers must never necessitate rewriting core logic.

3. **Selective Context with Provenance and Classification**
   Never dump raw logs or entire databases into a model prompt. Context must be selectively compiled, strictly budgeted, prioritized by relevance, and carry cryptographic/relational provenance and data classification tags (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).

4. **Secrets Must Never Reach Models or the Browser**
   API keys, passwords, bearer tokens, private keys, and machine-specific sensitive paths must never be injected into prompts, transmitted to model providers, leaked into client-side bundles, or committed to source control. Secrets live exclusively in server-side secure configuration.

5. **Permission-Controlled and Verifiable Tool Execution**
   Model output is untrusted input. Tool execution is governed by an explicit allowlist, strict JSON schema validation, server-side authorization checks, idempotency tracking, and independent verification. Never execute arbitrary shell commands, code, or filesystem mutations solely because a model proposed them.

6. **Clear Separation Between Memory and Event Ledger**
   - **Memory Service**: Manages synthesized working, episodic, semantic, and procedural knowledge (subject to decay, updating, refinement, and pruning).
   - **Event Ledger**: An append-only, immutable historical log of all inputs, state changes, tool executions, and model decisions enabling deterministic audit, replay, and recovery.

7. **Consequential Actions Require Explicit Authorization**
   Actions that mutate external state, delete resources, perform financial transactions, send communications, or transmit sensitive data require human approval through the Policy & Approval Engine. Untrusted external content cannot grant or elevate permissions.

8. **Untrusted External Content Cannot Grant Permissions**
   Data retrieved from web pages, emails, ingested documents, or external APIs is treated as potentially adversarial (prompt injection, indirect instructions). Content from untrusted sources is strictly isolated from system instructions.

9. **Incremental, Reviewable Slices**
   Development proceeds in small, verifiable vertical slices. Do not attempt monolithic application rewrites. Each slice must have automated tests, documentation, and explicit acceptance criteria.

10. **Truthful Reporting**
    Test runs, benchmark scores, build outcomes, and operational statuses must be reported truthfully. Never claim tests passed or capabilities exist without executing them and recording the actual output.

11. **Safety and Integrity of Existing Work**
    Never introduce unrelated changes, overwrite files without review, perform destructive git operations (force push, reset), or introduce unreviewed destructive operations.

---

## 2. Hardware Constraints & Local-First Strategy

Target Development & Deployment Environment:
- **Processor**: Intel Core i5-13420H
- **Memory**: 16 GB System RAM
- **GPU**: NVIDIA GeForce RTX 3050 Laptop GPU (4 GB dedicated VRAM)
- **Disk**: ~60 GB available storage
- **Host OS**: Windows 11 with PowerShell

### Hard Hardware Rules
- **No Heavy Model Dependencies**: Do not depend on 125B+ or 70B parameter models. Never assume GPU offloading makes an oversized model fit into 4 GB VRAM.
- **Model Download Size != Runtime Memory**: In-memory KV cache and context buffers consume significant RAM/VRAM. Keep active context limit at 4096 tokens (test 8192 only with measured headroom).
- **At Most One Model Loaded**: Enforce strict single-model residency in local inference runtimes (e.g., Ollama). Never load chat and embedding models simultaneously if the cumulative footprint exceeds RAM/VRAM budgets.
- **Graceful Degradation**: PICO must remain functional when local inference runtimes or cloud APIs are unavailable. Offline persistence, task management, and deterministic workflows must never crash due to network or provider outages.

---

## 3. Model Profiles & Deterministic Routing

1. **`local-small`** (Provider: Ollama, Default: `qwen3.5:2b`)
   - Scope: Fast classification, intent extraction, tagging, summarization, memory-worthiness proposals.
   - Limitation: No autonomous multi-step planning or unrestricted tool calling. Must pass benchmark gates before a task category is enabled.
2. **`local-mid`** (Provider: Ollama, Candidate: `qwen3.5:4b`)
   - Disabled by default. Enabled only after benchmark validation demonstrates acceptable latency and system stability.
3. **`cloud-strong`** (Configurable Cloud Provider, e.g., Anthropic / OpenAI / Gemini)
   - Scope: Complex architectural reasoning, multi-step verification, deep coding, and edge-case resolution.
   - Privacy & Cloud Routing Policy:
     - Only `PUBLIC` effective context is eligible for cloud processing.
     - `INTERNAL`, `SENSITIVE`, `SECRET`, missing classifications, and unknown classifications must remain local or fail closed.
     - Cloud eligibility requires explicit cloud enablement (e.g., `CLOUD_ENABLED=true`).
     - There is no sensitive-data consent exception in the current implementation.
     - Guardrails: Visual UI indicator, strict data redaction/minimization. Data classified as `SECRET`, `SENSITIVE`, `INTERNAL`, or unknown must NEVER be sent to cloud providers.
4. **`embeddings`** (Local CPU/lightweight GPU model)
   - Scope: Local semantic vector generation for episodic/semantic memory retrieval. Persist model identifier and vector dimensionality.

---

## 4. Engineering & Code Standards

- **Backend**: Python 3.12+ (tested on current runtime) with FastAPI, SQLAlchemy 2.0, Pydantic v2, Alembic, and SQLite (WAL mode).
- **Clean Architecture**: Clear separation across Database Config -> Migrations -> ORM Models -> Schemas -> Repositories -> Services -> API Routers.
- **Database Invariants**:
  - `PRAGMA foreign_keys = ON;` enforced on every SQLite connection.
  - `PRAGMA journal_mode = WAL;` enabled for concurrent read/write throughput.
  - Transactions for all multi-record mutations.
  - No raw string interpolation for SQL queries; use SQLAlchemy typed constructs.
- **Deterministic Identifiers**: Use stable typed prefixes (e.g., `conv_`, `msg_`, `proj_`, `task_`) or standard UUIDv4 strings.
- **Time Standards**: All timestamps stored and transmitted as UTC ISO8601 strings with timezone indicators.

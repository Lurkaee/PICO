# PICO Implementation Plan

## 1. Plan Overview

The implementation of PICO is structured into distinct, reviewable vertical slices. Each slice establishes working, tested functionality with explicit acceptance criteria. Capabilities planned for future phases are strictly marked as **Planned** and never marked as completed until verified by automated tests.

---

## 2. Phase Breakdown & Slice Roadmap

### Phase 0: Foundation & System Governance
- [x] **0.1 System Governance (`AGENTS.md`)**
  - Establish non-negotiable architectural rules, hardware constraints, and coding standards.
  - *Status*: **COMPLETED**.
- [x] **0.2 Core Architecture Document (`docs/architecture.md`)**
  - Define subsystem boundaries, data flow sequence diagram, and model gateway contracts.
  - *Status*: **COMPLETED**.
- [x] **0.3 Data Model & Schema Specification (`docs/data-model.md`)**
  - Specify Slice 1 relational models and future schema evolution.
  - *Status*: **COMPLETED**.
- [x] **0.4 REST API Contract (`docs/api-contract.md`)**
  - Specify endpoint routes, request/response models, and error envelopes.
  - *Status*: **COMPLETED**.
- [x] **0.5 Security & Risk Register (`docs/security-and-risk-register.md`)**
  - Enumerate security boundaries, injection defenses, and risk mitigations.
  - *Status*: **COMPLETED**.
- [x] **0.6 Evaluation Suite Design (`docs/evaluation/`)**
  - Establish initial 10 representative evaluation cases across system capabilities.
  - *Status*: **COMPLETED**.
- [x] **0.7 Model Gateway Architecture (`docs/model-gateway-design.md`)**
  - Define per-step model routing policies, profile schemas, and privacy rules.
  - *Status*: **COMPLETED**.

---

### Phase 1: Core System & Multi-Model Execution
- [ ] **Slice 1: Durable Core Persistence (ACTIVE IMPLEMENTATION)**
  - SQLite with WAL mode (`PRAGMA journal_mode = WAL`) and foreign key enforcement (`PRAGMA foreign_keys = ON`).
  - Alembic database migration management.
  - SQLAlchemy 2.0 ORM models for Conversations, Messages, Projects, Tasks.
  - Pydantic v2 validation schemas and repository/service layer abstraction.
  - FastAPI endpoints for Conversations, Messages, Projects, Tasks, and Health.
  - Complete automated test suite verifying persistence, cascade deletes, error handling, and session re-opening.
  - *Dependencies*: Phase 0 foundation.
  - *Status*: **IN PROGRESS**.

- [ ] **Slice 2: Model Interface, Streaming Chat & Deterministic Router (PLANNED)**
  - Abstract `ModelProvider` interface supporting streaming, structured JSON, tool proposals, and metadata.
  - Clearly labelled development-only `MockModelProvider`.
  - Native Ollama provider adapter (`qwen3.5:2b`).
  - Configurable cloud adapter (Anthropic/OpenAI/Gemini) with zero secret leakage.
  - Deterministic per-step model router enforcing data classification and fallback hierarchy.
  - Persistent routing decision log.
  - *Dependencies*: Slice 1.
  - *Status*: **PLANNED**.

- [ ] **Slice 3: Local Benchmark Gate (`tools/bench/`) (PLANNED)**
  - Benchmark test runner measuring Time To First Token (TTFT), tokens/sec, RAM, and VRAM.
  - 20-case test battery for JSON validity, intent, extraction, tool proposals, and privacy refusal.
  - Acceptance gate disabling tasks that fail accuracy or latency thresholds.
  - *Dependencies*: Slice 2.
  - *Status*: **PLANNED**.

- [ ] **Slice 4: Local Embedding Adapter & Memory Retrieval Skeleton (PLANNED)**
  - CPU-compatible local embedding adapter.
  - Persisted embedding metadata (model tag, dimensions).
  - Basic similarity retrieval and re-embedding migration utility.
  - *Dependencies*: Slice 1, Slice 2.
  - *Status*: **PLANNED**.

---

### Phase 2: Memory, Context & Execution Engine (PLANNED)
- [ ] **2.1 Four-Part Memory Service** (Working, Episodic, Semantic, Procedural).
- [ ] **2.2 Context Compiler** with provenance and token budgeting.
- [ ] **2.3 Append-Only Event Ledger** with replay and idempotency verification.
- [ ] **2.4 Capability Registry** with schema validation and tool sandboxing.
- [ ] **2.5 Policy & Approval Engine** for human-in-the-loop authorization.

---

### Phase 3: Autonomous Verification & Experience (PLANNED)
- [ ] **3.1 Independent Verifier** for tool outputs and model claims.
- [ ] **3.2 Experience Layer** (Responsive desktop web interface).
- [ ] **3.3 Background Job Runner** for asynchronous task execution.
- [ ] **3.4 Observability Dashboard** with token usage and cost accounting.

# PICO Canonical Project State & Roadmap Ledger
**Last Updated:** 2026-11-20  
**Repository:** `https://github.com/Lurkaee/PICO`  
**Governing Standard:** Canonical Implementation Plan (`docs/implementation-plan.md`)  
**Status Taxonomy:** `IMPLEMENTED` | `PARTIALLY IMPLEMENTED` | `IN PROGRESS` | `PLANNED` | `DEFERRED` | `BLOCKED`

---

## 1. Overview & Operational Principles
This document serves as the authoritative, auditable single source of truth for PICO's development sequence.
- **Never claim unverified progress:** Incomplete or planned features are never marked as completed until verified by concrete tests.
- **Fail-closed privacy & classification:** All effective context compilation adheres to canonical classifications (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).
- **Concurrent agent boundaries:** Phases 11.3, 11.4, and 11.5 progress independently without cross-branch renames, resets, or premature pruning. Cline maintains ownership of ongoing frontend redesign and backend repair work.

---

## 2. Milestone State Matrix

| Phase | Subsystem / Slice | Status | Owner | Active Branch | Relevant PR / Tag | Relevant SHA | Dependencies | Canonical Acceptance Criteria | Verification Evidence | Blockers / Open Items |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **P0** | System Architecture Baseline & Core Persistence | `IMPLEMENTED` | Core Team | `main` | PR #1, PR #2 | `36e0e14` | Hardware baseline | SQLite WAL, relational schema (conversations, messages, projects, tasks), Alembic migrations, AGENTS.md invariants. | `tests/test_db_persistence.py` (all passing) | None |
| **B1** | Think-Behavior Reliability | `IMPLEMENTED` | Core Team | `main` | PR #1 | `36e0e14` | P0 | Strict precedence: GenerationOptions.think -> Provider default (False) -> Omitted. | 11 parametrized tests in `tests/test_ollama_provider.py` | None |
| **P1** | Central Privacy Gate & Sensitivity Routing | `IMPLEMENTED` | Core Team | `main` | PR #2, PR #3 | `36e0e14` | P0, B1 | Effective context classification (`SECRET` > `SENSITIVE` > `INTERNAL` > `PUBLIC`), `RULE_PRIVATE_LOCAL_PIN`, zero non-PUBLIC data sent to cloud. | `tests/test_routing_service.py`, `tests/test_cloud_provider.py` | None |
| **P2** | Context Compiler v0 & Framing Overhead | `IMPLEMENTED` | Core Team | `main` | PR #4, PR #7 | `36e0e14` | P1 | Conservative token budgeting (`max_prompt_budget` = 3328 / 4096 ctx), ChatML & OpenAI message framing overhead explicitly calculated. | `tests/test_context_compiler.py` (22 tests) | None |
| **P3** | Four-Part Memory Subsystem | `IMPLEMENTED` | Core Team | `main` | PR #7 | `36e0e14` | P2 | Working, Episodic, Semantic (SQLite FTS5), and Procedural memory with lifecycle state machines and decay/pruning. | `tests/test_memory_api.py`, `tests/test_memory_service.py` | None |
| **P4** | Context Budgeting & Token Bounding | `IMPLEMENTED` | Core Team | `main` | PR #7 | `36e0e14` | P3 | Fail-closed token bounds, priority-driven context truncation, non-downgrade transformation invariant. | `tests/test_context_compiler.py` | None |
| **P5** | Capability Registry & Execution Engine | `IMPLEMENTED` | Core Team | `main` | PR #16 | `36e0e14` | P4 | Allowlisted tool execution, risk tiers (`READ_ONLY`, `MUTATION_REVERSIBLE`, `CONSEQUENTIAL`), Pydantic v2 schemas. | `tests/test_capability_registry.py` | None |
| **P6** | Multimodal Audio Perception Foundation | `IMPLEMENTED` | Core Team | `main` | PR #16 | `36e0e14` | P5 | Local AudioSessionManager lifecycle, mock STT/TTS adapters, visual microphone states, fail-closed audio validation. | `tests/test_audio_session_lifecycle.py` | None |
| **P7** | Home Assistant Smart Home Integration | `IMPLEMENTED` | Core Team | `main` | PR #17, PR #21, PR #42–44 | `36e0e14` | P5 | Read-only entity discovery, allowlisted climate planning, Stage D actuation restricted to verified test light (P7.3) & climate setpoint (P7.4). | `tests/test_homeassistant_*.py` (48 tests) | None |
| **P8** | OS, Filesystem & Sandboxed Browser Automation | `IMPLEMENTED` | Core Team | `main` | PR #25, PR #28, PR #30 | `36e0e14` | P5, P7 | Accessible OS automation, staged filesystem diffs within project root, Playwright sandboxed browser with origin allowlists. | `tests/test_os_automation.py`, `tests/test_filesystem_*.py` | None |
| **P9** | Autonomous Workflow Orchestration Engine | `IMPLEMENTED` | Core Team | `main` | PR #31, PR #33, PR #46 | `36e0e14` | P5, P8 | Typed DAG execution engine, Kahn acyclicity verification, Stage A–D verifiers, compensation handlers, independent verification. | `tests/test_workflow_coordination.py` (18 tests), `tests/test_resource_scheduler.py` (16 tests), `tests/test_workflow_verification.py` (13 tests) | None |
| **P10.1** | Desktop Web Command Centre | `IMPLEMENTED` | Frontend Agent / Cline | `main` | PR #32 | `36e0e14` | P9 | Local-first React 19 + Vite 8 frontend, streaming chat, execution timeline, memory inspector, approval inbox. | 125 Vitest tests in `frontend/src/__tests__/` | None |
| **P10.2** | Offline Shell & Safe Recovery | `IMPLEMENTED` | Frontend Agent / Cline | `main` | PR #34 | `36e0e14` | P10.1 | Service worker offline shell, bounded in-memory draft outbox, ambiguous outcome tagging, session revalidation. | `frontend/src/__tests__/resilience.test.tsx` | None |
| **P10.3** | Operator Authorization & Standalone Dispatch | `IMPLEMENTED` | Core Team | `main` | PR #46, PR #47 | `36e0e14` | P10.1 | Policy A pairing challenge CLI, mandatory physical actuation token gate, standalone dispatch lifecycle without auto-dispatching. | `tests/test_operator_pairing.py`, `frontend/src/__tests__/approvals.test.tsx` | None |
| **P11.1** | Conversational Workflow Synthesis | `IMPLEMENTED` | Core Team | `main` | PR #50 | `36e0e14` | P9, P10.3 | Natural language intent extraction to validated DAGs; planning gated behind benchmark evaluation on `local-small`. | `tests/test_workflow_synthesis.py` (72 tests) | Hardware evaluation pending |
| **P11.2.1** | Native Desktop Companion Shell (Tauri 2) | `IMPLEMENTED` | Desktop Agent | `main` | PR #69 | `36e0e14` | P10.1 | Native Windows desktop companion shell using Tauri 2 + Rust, frameless transparent window, system tray supervisor. | `frontend/src-tauri/src/supervisor.rs` tests | None |
| **P11.2.2** | Secure Loopback Bridge & Native Proxy | `IMPLEMENTED` | Desktop Agent | `main` | PR #71 | `36e0e14` | P11.2.1 | Authenticated loopback bridge between desktop companion and local backend; native proxy security architecture (ADR 0002/0003). | `tests/test_bridge_proof.py` (all passing) | None |
| **P11.2.3** | Native Idle Companion & Deferred Command Center | `IMPLEMENTED` | Desktop Agent | `main` | PR #72 | `36e0e14` | P11.2.2 | Compact floating desktop companion widget with expandable Command Center pane (ADR 0004). | `frontend/src/__tests__/desktop-shell.test.tsx` | None |
| **P11.2.4** | Windows Packaging & Runtime Isolation | `IMPLEMENTED` | Packaging Agent | `main` | PR #72, PR #73, PR #74 | `36e0e14` | P11.2.3 | Windows NSIS installer packaging, isolated CPython 3.14 runtime, PyPI SHA-256 verified wheels. | `tools/packaging/requirements-windows-runtime.lock` | None |
| **P11.2.4.3** | Native Expressive Animated Eyes | `IMPLEMENTED` | UI Agent | `main` | PR #91 | `36e0e14` | P11.2.3 | EMO-inspired visual evolution with 11 distinct expressive eye states synchronizing with PICO system state. | `docs/evaluation/native-expressive-eyes-audit.md` | None |
| **P11.3.1** | Local Voice Runtime & Capture Slice | `IMPLEMENTED` | Voice Agent | `main` / `feature/p11-3-1-voice-runtime-and-capture-slice` | PR #88, PR #89 | `693d357` | P6, P11.2 | Local audio capture abstraction, strict WAV format validation, Policy A transcript authorization, fail-closed STT. | `tests/test_voice_runtime_slice.py` (all passing) | None |
| **P11.3.2** | Native Whisper STT Hardware Qualification | `PARTIALLY IMPLEMENTED` | Voice Agent | `feature/p11-3-2-native-whisper-qualification` | PR #93 (Prototype merged) | `614319c` | P11.3.1 | Native whisper.cpp CPU qualification prototype; 20-case local command corpus benchmarking. | `docs/evaluation/voice-qualification-results.md` | Hardware promotion gate GATE-VOICE-09 |
| **P11.3.3** | Voice Promotion Rules & Adapter Closure | `IN PROGRESS` | Voice Agent | `docs/p11-3-voice-engine-qualification`, `proposal/p11-3-qualification-promotion-rules` | PR #94, PR #95, PR #96 | `e06d75b` | P11.3.2 | ADR 0006 promotion rules: allow Stage 3 adapter implementation before GATE-VOICE-09, requiring hardware verification before activation. | `docs/decisions/0006-proposal-p11-3-voice-qualification-promotion-rules.md` | Active qualification execution |
| **P11.4** | End-to-End Multimodal Task Actuation | `IN PROGRESS` | Actuation Agent | Active Roadmap | Implementation in planning | `36e0e14` | P11.1, P11.2, P11.3, P8, P7.4 | Spoken voice commands transcribed locally -> routed through Context Compiler -> synthesized into DAG -> executed via OS/browser/FS capabilities. | Specified in `docs/implementation-plan.md` | Awaiting P11.3 voice promotion closure |
| **P11.5** | Contextual Desktop Perception & Screen Understanding | `IN PROGRESS` | Perception Agent | `docs/p11-5-desktop-perception-spec` | Active Worktree | `c113199` | P6, P8.1, P11.1, P11.2 | Explicit on-demand screen capture, ephemeral zero-retention RAM frame lifecycle, untrusted data classification (`INTERNAL`/`SENSITIVE`), local visual reasoning. | Architectural specification draft in `docs/decisions/0007-p11-5-contextual-desktop-perception-architecture.md` | Specification under active review |

---

## 3. Active Agent & Worktree Governance
- **Cline Workspace (`C:\Users\AYUSH\OneDrive\Desktop\PICO`):**
  - Active Branch: `recovery/pico-frontend-redesign`
  - Scope: Frontend chat workspace design system, SystemStatus component repair, and CSS stylesheet reconstruction.
  - Policy: Protected. Zero resets, checkouts, or file overwrites.
- **Phase 11.5 Perception Workspace (`C:\Users\AYUSH\OneDrive\Desktop\PICO-p11-5-spec`):**
  - Active Branch: `docs/p11-5-desktop-perception-spec`
  - Scope: Architecture decision record `0007` for contextual desktop perception and screen understanding.
  - Policy: Protected. Active independent worktree.
- **Governance Workspace (`C:\Users\AYUSH\.gemini\antigravity\scratch\PICO_governance_worktree`):**
  - Active Branch: `chore/repository-stabilization-and-governance`
  - Scope: Branch consolidation, file audit, documentation synchronization, and workflow hardening.

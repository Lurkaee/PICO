# PICO Implementation Plan & Milestone Roadmap
## Canonical P0–P10 Milestone Tracking

---

## 1. Overview & Operating Invariants

PICO's implementation proceeds through verified, reviewable vertical slices. Each slice establishes functioning, automatedly tested capabilities with explicit acceptance criteria.
- **Incomplete or Planned features are never marked as completed** until verified by concrete tests.
- **Hardware constraints** (Intel i5-13420H, 16 GB RAM, RTX 3050 4 GB VRAM) govern all runtime allocations.
- **Canonical data classifications** (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`) are enforced across all context compilation and model routing.
- **Precedence Order**: B1 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8 -> P9 -> P10.

---

## 2. Milestone Breakdown & Status

### Milestone P0: Architecture & Repository Baseline
- [x] **0.1 System Governance (`AGENTS.md`)**: Non-negotiable architectural invariants, data classifications, hardware limits.
- [x] **0.2 Canonical Architecture Record (`docs/architecture.md`)**: Comprehensive system record with multimodal perception, Semantic Context Layer, JARVIS execution, capability inventory, and P0-P10 roadmap.
- [x] **0.3 Data Model Contract (`docs/data-model.md`)**: Relational models (SQLite WAL), migrations, future ledger/memory schemas.
- [x] **0.4 API Contract (`docs/api-contract.md`)**: REST endpoints, SSE streaming specs, error envelopes.
- [x] **0.5 Security Register (`docs/security-and-risk-register.md`)**: Threat models, trust boundaries, privacy safeguards.
- [x] **0.6 Evaluation Suite Baseline (`docs/evaluation/`)**: Initial representative cases for routing, privacy, and models.
- [x] **0.7 Core Persistence Engine (`pico/db/`, `pico/models/`, `pico/repositories/`)**: SQLite WAL mode, foreign keys enforced, Conversations, Messages, Projects, Tasks tables with Alembic migrations.
- **Status**: **COMPLETED & VERIFIED**.

### Milestone B1: Think-Behavior Reliability
- [x] **B1.1 Ollama Think Precedence**: Implemented strict precedence: `GenerationOptions.think` -> Provider default (`think=False`) -> Omitted.
- [x] **B1.2 Payload Serialization**: Verified that Ollama payload serializes boolean `think` or omits it according to the precedence contract.
- [x] **B1.3 Multi-Provider Compatibility**: Verified mock and cloud providers gracefully handle generation options with think flags.
- **Status**: **COMPLETED & VERIFIED** (11 parametrized test assertions in `tests/test_ollama_provider.py`).

### Milestone P1: Central Privacy Gate & Context Sensitivity
- [x] **P1.1 Effective Context Classification**: Context-level sensitivity hierarchy (`SECRET` > `SENSITIVE` > `INTERNAL` > `PUBLIC`). Missing or invalid classifications default to `INTERNAL`.
- [x] **P1.2 Strict Local Pinning**: Non-`PUBLIC` context is strictly prohibited from cloud processing (`RULE_PRIVATE_LOCAL_PIN`). Zero non-`PUBLIC` data sent to cloud.
- [x] **P1.3 Cloud Eligibility Gate**: Cloud inference strictly requires `PUBLIC` context and explicit `CLOUD_ENABLED=true`.
- [x] **P1.4 Defense-in-Depth Verification**: Safeguards enforced at Router, chat streaming endpoint, and inside `CloudProvider.stream_chat`.
- **Status**: **COMPLETED & VERIFIED** (`tests/test_routing_service.py`, `tests/test_chat_stream_api.py`, `tests/test_cloud_provider.py`).

### Milestone P2: Context Compiler v0
- [x] **P2.1 Typed Context Items**: Context items with role, item type, content, token count, priority, and explicit source provenance (`msg:`, `proj:`, `task:`, `system:`, `mem:`).
- [x] **P2.2 Documented Conservative Budget Strategy**: Implemented `conservative-prompt-budget-v1` clamping prompt ceiling to `max_prompt_budget` (3328 tokens on a 4096 context window) with 512 completion reserve and 256 safety headroom so `prompt + completion + headroom <= 4096`. Enforced at provider boundary via `options.num_predict` and `options.num_ctx`.
- [x] **P2.3 Model-Aware Tokenizer & Conservative Upper Bound**: Model-aware tokenizer selection (`chatml` for Qwen, `openai_chat` for GPT-4). `is_exact_count=False` for all compiled chat requests because complete provider requests include estimated message-framing overhead; when an exact BPE vocabulary is available for content (e.g. `tiktoken-cl100k` for GPT-4), `is_exact_content_tokenizer=True` and `bound_type='exact_content_estimated_framing'`. For Qwen models (Ollama), `is_exact_count=False` and token footprint is upper-bounded by UTF-8 byte length (`conservative-utf8-byte-bound-v1`) guaranteeing non-underestimation in byte-fallback BPE tokenizers.
- [x] **P2.4 Chat Framing Overhead & Model Config Validation**: Explicitly accounts for template framing overhead (ChatML: 5 tokens/message envelope + 3 tokens generation prompt; OpenAI: 3 tokens/message + 3 generation prompt) in total token footprint. Validates Qwen ChatML decomposition and active model configuration via `ContextCompiler.validate_chat_template_overhead()`.
- [x] **P2.5 Mandatory Instruction Protection & Deterministic Pruning**: Mandatory system prompts and privacy/safety rules (`is_mandatory=True` or `role='system'`) are protected and cannot be pruned for optional history. If mandatory instructions alone exceed prompt budget, the compiler fails closed with `ValidationError`. Optional history and context are pruned first.
- [x] **P2.6 Boundary Truncation & Fail-Closed Classification**: Binary search truncation for oversized current turn; effective sensitivity computed from included items; invalid/missing tags default to `INTERNAL`.
- [x] **P2.7 Inspectable Decision Logs**: Audit report of included, truncated, and excluded context items with explicit reasons and budget attribution.
- [x] **P2.8 Streaming Chat & Provider Boundary Enforcement**: `POST /api/chat/stream` routes context through `ContextCompiler`. Ollama provider explicitly passes `num_ctx=4096` and `num_predict <= 512` in API options to strictly enforce context and generation limits at provider runtime.
- **Status**: **CLOSED & FULLY VERIFIED** (`tests/test_context_compiler.py`, `tests/test_chat_stream_api.py`, `tests/test_ollama_provider.py`).

### Milestone P3: Hardware Benchmarks & Quality Gates
- [x] **P3.1 Telemetry Runner**: Automated measurement of TTFT, tok/s, prompt processing speed, RAM (kernel32), VRAM (nvidia-smi), and disk usage with auditable JSON/Markdown reports.
- [x] **P3.2 Baseline Repetitions**: 5 valid benchmark repetitions per case across 8 representative workloads (40 measured runs + 1 warmup) on `qwen3.5:2b`. Evaluated candidate `qwen3.5:4b` and recorded as `NOT RUN — MODEL NOT INSTALLED` (zero unauthorized downloads).
- [x] **P3.3 Single-Model Invariant Gate**: Verified single-model residency in VRAM (~2437 MB peak VRAM on RTX 3050 Laptop GPU, leaving ~1657 MB headroom). Evaluated output quality (95.0% pass rate) and confirmed deterministic routing necessity for intent classification.
- **Status**: **CLOSED & FULLY VERIFIED** (`bench_reports/bench_qwen3.5_2b_20261020_085749.md`, `bench_reports/bench_qwen3.5_2b_20261020_085749.json`, `tests/test_bench_runner.py`).

### Milestone P4: Memory v1 (Four-Part Subsystem)
- [x] **P4.1 Working Memory Service**: Ephemeral, bounded session scratchpad and active task memory with explicit TTL and bounded retention.
- [x] **P4.2 Episodic Memory Service**: Time-indexed interaction logs and workflow outcomes with deduplication and observed vs. inferred tracking.
- [x] **P4.3 Semantic Memory Store**: Verified facts and preferences with SQLite FTS5 index, conflict handling (no silent overwrite), and user-directed forget.
- [x] **P4.4 Procedural Memory Store**: Reusable skills with strict `draft -> reviewed -> approved -> active -> retired` lifecycle requiring mandatory human approval.
- [x] **P4.5 Context Compiler Integration**: Memory retrieval propagates source provenance (`mem:...`, `wmem:...`) and data classification, enforcing fail-closed privacy.
- **Status**: **CLOSED & FULLY VERIFIED** (`pico/models/`, `pico/repositories/`, `pico/services/memory_service.py`, `pico/api/memory.py`, 130 passing tests).

### Milestone P5: Tool Execution & Verification Engine
- [x] **P5.1 Capability Registry & Built-ins**: Explicit capability allowlist with declared input/output JSON schemas, risk tiers, permissions, idempotency flags, timeouts, and output bounds (`core.calculator`, `core.validate_json`, `db.read_project`, `db.read_task`, `system.diagnostics`, and test capabilities).
- [x] **P5.2 Task Execution State Machine & Lifecycle**: Transitions `queued -> running -> verifying -> succeeded` with `awaiting_approval`, `failed`, `cancelled`, `timed_out`, `verification_failed`, and `retry_scheduled`. Transactional state changes and startup crash recovery.
- [x] **P5.3 Idempotency, Deadlines & Cancellation**: Deterministic idempotency key deduplication, execution timeouts (`timeout_seconds`), cooperative cancellation, bounded retries.
- [x] **P5.4 Independent Deterministic Verification**: Verifier stage independent from handler result checking schema adherence, mathematical or state consistency, and execution evidence; verifier failures fail closed (`verification_failed`).
- [x] **P5.5 Trusted Authorization & Human Approval**: Consequential capabilities bound to SHA-256 `input_digest` requiring trusted human approver identity (`settings.TRUSTED_APPROVER_IDENTITIES`) and token before execution; tampered inputs invalidate approval; memory/caller cannot escalate privileges.
- [x] **P5.6 Append-Only Event Ledger**: Immutable audit log (`event_ledger`) for lifecycle transitions with automatic secret/credential redaction.
- [x] **P5.7 Procedural Memory Integration**: Skill validation endpoint (`/api/tools/procedures/{id}/validate`) ensuring procedural steps map to valid registered capabilities without self-activation.
- [x] **P5.8 REST API Endpoints**: Endpoints under `/api/tools/` for capabilities discovery, execution submission/running/cancellation/events, and approvals.
- **Status**: **CLOSED & FULLY VERIFIED** (151 total passing tests, 21 dedicated P5 tests, 0 failures).

### Milestone P6: Multimodal Perception Layer
- [x] **P6.1 Push-to-Talk Voice Adapter**: Push-to-talk lifecycle (`AudioSessionManager`), provider-neutral STT/TTS adapters (`MockSpeechToTextAdapter`, `MockTextToSpeechAdapter`, `LocalWhisperSpeechToTextAdapter`, `LocalTextToSpeechAdapter`), observable capture states, immediate PCM buffer disposal, honest offline capability reporting.
- [x] **P6.2 On-Demand Vision Adapter**: On-demand visual frame ingestion (`VisionAdapter`), mandatory user consent (`user_consent=True`), input size (4 MB) and dimension (4096x4096) boundaries, transient buffer release, pure-Python dimension parsers.
- [x] **P6.3 Motion Event Adapter**: 3-tier motion architecture (`MotionAdapter`): Tier 1 PIR motion detection (presence only), Tier 2 deterministic CPU frame-differencing (delta ratio, centroid displacement, direction, magnitude), Tier 3 cross-modal event correlation with temporal windows (`CrossModalCorrelator`).
- [x] **P6.4 Sensor Normalization & Fusion**: Shared canonical `NormalizedPerceptionEvent` contract across all 4 modalities, bounded validation, per-sensor deduplication cooldown, strict read-only constraints, persistent `perception_events` table via Alembic migration (`4e195bc031da`).
- [x] **P6.5 Core Integration & Tool Capabilities**: Context Compiler integration (priority 45 sensor items), Tool Engine capabilities (`perception.status`, `perception.get_recent_events`, `perception.validate_sensor_payload`, `perception.analyze_motion`), REST API router (`/api/perception/`).
- **Status**: **CLOSED & FULLY VERIFIED** (174 total passing tests, 23 dedicated P6 tests, 0 failures).

### Milestone P7: Home Automation & Physical Actuation
- [ ] **P7.1 Home Assistant Hub Connector**: REST/WebSocket integration with entity discovery.
- [ ] **P7.2 Stage A Read-Only Awareness**: Inspect and report states without mutating devices.
- [ ] **P7.3 Stage B Verified Light Toggle**: Intent -> Plan -> Approval -> Act -> State Verification -> Event Ledger.
- [ ] **P7.4 Stage C/D Everyday Appliances**: Authorized switches, plugs, and climate controls.
- **Status**: **PLANNED**.

### Milestone P8: Computer Control & Engineering Workspace
- [ ] **P8.1 OS & Application Automation**: Sandboxed application launching and window state inspection.
- [ ] **P8.2 Staged Filesystem Diffs**: Reversible file writes with approval diffs.
- [ ] **P8.3 Sandboxed Browser Automation**: Playwright adapter with untrusted web content isolation.
- **Status**: **PLANNED**.

### Milestone P9: Multi-Agent Workflow Coordination
- [ ] **P9.1 Task Delegation**: Subtask execution over persistent Task Engine.
- [ ] **P9.2 Single-Model Resource Scheduling**: Logical workers scheduled over single resident model.
- [ ] **P9.3 Independent Verification**: Verifier audits worker outputs before dependent tasks proceed.
- **Status**: **PLANNED**.

### Milestone P10: Unified PICO Experience
- [ ] **P10.1 Desktop Web Experience**: Streaming chat, voice toggle, memory inspector, task timeline, approvals.
- [ ] **P10.2 Offline Resilience**: Graceful degradation when inference or network is offline.
- **Status**: **PLANNED**.

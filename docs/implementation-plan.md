# PICO Implementation Plan & Milestone Roadmap
## Canonical P0–P11 Milestone Tracking

---

## 1. Overview & Operating Invariants

PICO's implementation proceeds through verified, reviewable vertical slices. Each slice establishes functioning, automatedly tested capabilities with explicit acceptance criteria.
- **Incomplete or Planned features are never marked as completed** until verified by concrete tests.
- **Hardware constraints** (Intel i5-13420H, 16 GB RAM, RTX 3050 4 GB VRAM) govern all runtime allocations.
- **Canonical data classifications** (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`) are enforced across all context compilation and model routing.
- **Precedence Order**: B1 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8 -> P9 -> P10 -> P11.

---

## 2. Milestone Breakdown & Status

### Milestone P0: Architecture & Repository Baseline
- [x] **0.1 System Governance (`AGENTS.md`)**: Non-negotiable architectural invariants, data classifications, hardware limits.
- [x] **0.2 Canonical Architecture Record (`docs/architecture.md`)**: Comprehensive system record with multimodal perception, Semantic Context Layer, JARVIS execution, capability inventory, and P0-P11 roadmap.
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
- [x] **P7.1 Home Assistant Hub Connector**: Authenticated REST and pure-Python asyncio WebSocket client with fail-closed URL validation, token protection, request/response correlation, and strict read-only command allowlisting. Zero new external dependencies.
- [x] **P7.2 Stage A Read-Only Awareness**: Read-only entity discovery (`GET /api/homeassistant/entities`), single entity state inspection (`GET /api/homeassistant/entities/{id}`), bounded overview (`GET /api/homeassistant/overview`), trusted classification (`INTERNAL`/`SENSITIVE`), context compiler integration (`format_homeassistant_context_items`), and tool capabilities (`homeassistant.status`, `homeassistant.list_entities`, `homeassistant.get_entity_state`, `homeassistant.overview`). Absolute read-only boundary enforced: zero actuation or state mutation paths.
- [x] **P7.3 Stage B Verified Light Toggle (First Physical Action)**:
  - Single configured entity limit (`HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID`) restricted strictly to the `light` domain with `turn_on` and `turn_off` actions only.
  - Fail-closed authorization gate requiring server-side `APPROVAL_SECRET_TOKEN` validation via `X-Pico-Approval-Token` header for proposal, approval, and execution. No loopback bypass for physical actuation. `homeassistant:actuate` permission separated from default tool permissions.
  - Triple-digest integrity verification immediately before dispatch (recomputed input SHA-256 == stored execution digest == approval bound digest).
  - Single-use approval ticket tracking preventing replayed or duplicate dispatches.
  - Dedicated REST mutation client isolated from read-only WebSocket commands; independent read-after-write verification polling via read-only state API; strictly zero automatic retries on timeout or failure.
  - Audit trail in immutable event ledger recording intent, approval, dispatch, and verification outcomes without credential leaks.
  - Read-only planning endpoint (`POST /api/homeassistant/actuation/plan`) and execution proposal endpoint (`POST /api/homeassistant/actuation/propose`).
  - Commissioning & Readiness Gate: Read-only preflight evaluation via CLI (`python -m pico.homeassistant.readiness`), REST API (`GET /api/homeassistant/actuation/readiness`), and tool capability (`homeassistant.actuation_readiness`). Inspects integration configuration, target test light domain constraints, approval credential presence without value revelation, fail-closed authorization availability, transport security, and actuation-disabled-by-default safety invariants. Strictly zero mutation calls, zero unsolicited network calls during offline preflight, and explicit operator opt-in required for live read-only inspection.
- [x] **P7.4.1 Stage C Explicitly Allowlisted Switch/Plug Control (First Stage C Capability)**:
  - Narrow, separately registered Stage C capability: `homeassistant.allowlisted_switch_actuate`.
  - Triple enablement gate: requires `HOME_ASSISTANT_ENABLED=true`, `HOME_ASSISTANT_ACTUATION_ENABLED=true`, and `HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED=true` (disabled by default).
  - Explicit entity allowlist (`HOME_ASSISTANT_SWITCH_ALLOWLIST`): exact JSON list of `switch.*` domain entities. Rejects wildcards, regex, prefixes, empty list, non-switch domains, and unlisted entities.
  - Fail-closed authorization: requires `homeassistant:actuate` permission, server-side `APPROVAL_SECRET_TOKEN` validation via constant-time comparison, triple-digest agreement, and single-use dispatch protection.
  - Operations strictly limited to `switch.turn_on` and `switch.turn_off`. `toggle`, custom services, and generic service execution (`call_service`) are strictly prohibited.
  - State satisfaction check: skips mutation if switch is already in the requested state (`dispatch_status='already_satisfied'`).
  - Read-after-write independent verification via read-only state query (`GET /api/states/{id}`). Never claims `physical_device_connectivity_claimed`. Zero automatic retries (`max_retries=0`).
  - REST API routes: `POST /api/homeassistant/switch/plan`, `POST /api/homeassistant/switch/propose`, `GET /api/homeassistant/switch/readiness`, and `GET /api/homeassistant/switch/{execution_id}` (strictly isolated to switch capability).
  - Preflight readiness inspection via CLI (`python -m pico.homeassistant.readiness --switch`), REST API (`GET /api/homeassistant/switch/readiness`), and tool capability (`homeassistant.switch_readiness`).
- [x] **P7.4.1.2 Truthful State Preflight & Fail-Closed Switch Readiness**:
  - Structured 4-way state readiness classification (`ready`, `already_satisfied`, `state_unavailable`, `state_unknown`) in `SwitchActuationPlan` and `SwitchActuationReadinessReport`.
  - Honest planner behavior: read failures report `state_readiness="state_unavailable"`, `current_state="unavailable"`, and `is_mutation_needed=False` with truthful precondition descriptions rather than claiming no mutation is needed. Indeterminate states (`unknown`, empty, or non-on/off) report `state_readiness="state_unknown"`.
  - Fail-closed pre-dispatch policy: `HomeAssistantSwitchActuationService.dispatch_actuation` executes an authoritative pre-dispatch read before physical mutation. Missing entity (404), network failure, timeout, or reported state `unknown`/`unavailable`/invalid returns structured `dispatch_status="blocked_precondition"` with zero physical mutations.
  - Zero mutations on already-satisfied state: authoritative read matching requested state skips physical dispatch (`dispatch_status="already_satisfied"`) and verifies through independent read-after-write evidence.
  - Fail-closed live commissioning readiness: `HomeAssistantSwitchReadinessChecker` marks `live_switch_readability` as `FAIL` with `STATE_UNAVAILABLE` or `STATE_UNKNOWN` error categories when target switch reports non-on/off states, preventing false commissioning clearance.
  - Execution failure output preservation: `ToolExecutionEngine._handle_execution_failure` records structured output in the execution repository so blocked precondition diagnostics remain inspectable via `GET /api/homeassistant/switch/{execution_id}`.
- [x] **P7.4.2.1 Stage D Read-Only Climate Awareness & Safe Setpoint Feasibility (Preparatory Slice)**:
  - Strictly read-only awareness and planning slice. Zero physical thermostat/HVAC changes or mutations (`is_mutation_performed=False`).
  - Separately scoped Stage D configuration: `HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED: bool = False` (disabled by default) and `HOME_ASSISTANT_CLIMATE_ALLOWLIST: str = "[]"` (exact JSON list of `climate.*` entity IDs).
  - Exact string allowlist validation: rejects wildcards, regex, prefixes, empty list, whitespace variations, and non-climate domains before issuing any Home Assistant requests.
  - Typed climate contracts (`ClimateObservation`, `ClimateSetpointPlan`, `ClimateReadinessReport`) with bounded sanitized attributes.
  - Deterministic setpoint feasibility planning: evaluates requested setpoints against reported `min_temp` and `max_temp` without inventing operating limits or converting ambiguous units. Structured status classifications (`ready`, `target_out_of_range`, `state_unavailable`, `state_unknown`, `unit_unknown`, `not_allowlisted`, `disabled`).
  - Read-only readiness inspection: CLI (`python -m pico.homeassistant.readiness --climate`), REST API (`GET /api/homeassistant/climate/readiness`), and tool capability (`homeassistant.climate_readiness`).
  - REST API endpoints: `POST /api/homeassistant/climate/plan`, `GET /api/homeassistant/climate/observe/{entity_id}`, and `GET /api/homeassistant/climate/readiness`.
  - Zero climate mutation or generic service capabilities: WebSocket allowlist unchanged, zero POST/PUT/PATCH/DELETE calls issued for climate planning/observation.
- [x] **P7.4.2.1.1 Stage D Climate Feasibility Metadata & Mode-Semantics Closure**:
  - Closed the `target_temp_step` gap: Decimal-safe increment alignment check (`dec_req % dec_step == 0`) with tolerance. Distinguishes supported aligned setpoint (`ready`), misaligned setpoint (`target_step_mismatch`), and missing/invalid/zero/negative/bool/non-finite step metadata (`step_unknown`). Does not fabricate default step increments or invent grid origins.
  - Verified capability bitmask validation (`supported_features`): Verified against Home Assistant climate contract (`TARGET_TEMPERATURE = 1`, `TARGET_TEMPERATURE_RANGE = 2`). Rejects single-setpoint requests against dual-range-only entities as `unsupported_capability`. Reports `capability_unknown` when capability metadata is missing, boolean, or malformed.
  - Operating mode compatibility enforcement: Prohibits reporting single setpoint as feasible when reported `hvac_mode` is `"off"` (powered off) or `"fan_only"` (fan only does not regulate temperature) or unrecognized non-regulating mode (`mode_incompatible`).
  - Sanitization boundary hardening: Added `supported_features` to `APPROVED_ATTRIBUTE_KEYS`. Enforced strict rejection of booleans where numeric values are expected across all numeric attributes (`supported_features`, `target_temp_step`, `min_temp`, `max_temp`, `temperature`, `current_temperature`).
  - API & client transparency: Exposes `target_temp_step: float | None` and `supported_features: int | None` on `ClimateSetpointPlan` and `supported_features` on `ClimateObservation`.
  - Explicit feasibility semantics: A `ready` plan certifies metadata feasibility (valid unit, numerical bounds, step alignment, single setpoint feature flag, active regulating mode); does not claim physical device connectivity or guarantee subsequent actuation success. Strict zero-mutation boundary maintained.
- [x] **P7.4.2.1.2 Stage D Climate Mode-Semantics & Step-Grid Correctness**:
  - Corrected single-setpoint operating mode semantics: Evaluated against authoritative Home Assistant core contract. `auto` mode is incompatible (`mode_incompatible`) because Home Assistant explicitly documents temperature cannot be set in this mode. `heat_cool` mode is incompatible (`mode_incompatible`) because it requires a dual-setpoint target range (`target_temp_low`/`target_temp_high`). `dry` mode fails closed (`mode_incompatible`) as universal single-setpoint compatibility is unverified. `off` and `fan_only` remain incompatible. `heat` and `cool` are the supported single-setpoint modes.
  - Feature bitmask precedence: A `supported_features` bitmask advertising `TARGET_TEMPERATURE` cannot override an incompatible operating mode.
  - Corrected step-grid alignment semantics: Upstream contract documents `target_temp_step` as the increment without defining a universal grid origin (neither 0.0 nor `min_temp` can be assumed). When `grid_origin` is not established, the planner fails closed as `step_alignment_unknown` (`is_feasible=false`). When a verified `grid_origin` is provided, offset alignment is evaluated with decimal-safe modulo (`abs(dec_req - dec_origin) % dec_step == 0`).
  - Preserved invariants: Zero rounding/mutation of requested temperature, strict rejection of booleans/malformed step values, strictly read-only awareness without climate mutation endpoints.
- [x] **P7.4.2.1.3 Stage D Trusted Climate Grid-Origin Configuration**:
  - Removed caller authority over grid origin: Public requests must NOT establish or choose a step-grid origin. Supplying `grid_origin` in `ClimatePlanRequest` is strictly forbidden and rejected with HTTP 422 (`extra="forbid"` and explicit validator).
  - Server-controlled per-entity configuration: Added `HOME_ASSISTANT_CLIMATE_GRID_ORIGINS` (default `"{}"`), validated fail-closed. Requires exact allowlisted `climate.*` entity IDs, no wildcards, no duplicates, exactly `{"origin", "unit"}` fields, finite non-negative non-boolean numeric origins, and canonical units matching the entity's observed temperature unit.
  - Fail-closed alignment: Unconfigured entities or unit mismatches fail closed as `step_alignment_unknown`. Missing or invalid `target_temp_step` fails closed as `step_unknown`.
  - Canonical plan digest binding: `input_digest` binds exact entity ID, requested temperature, requested unit, and effective server-configured origin and unit (`f"{origin}:{unit}"` or `"none:none"`).
  - Preserved strict zero-mutation boundaries: Zero climate actuation endpoints, zero mutation calls.
- [x] **P7.4.2.2 Stage D Allowlisted Single-Setpoint Climate Actuation**:
  - First physical climate actuation slice under Milestone P7.4.2. Supports strictly one operation: set requested temperature for an explicitly allowlisted climate entity.
  - Entity must already be in a verified, compatible operating mode (`heat` or `cool`). Strictly zero automatic mode changes, fan mode changes, preset activation, `heat_cool` dual-setpoint ranges, or generic service calls (`call_service`).
  - Read-only observation and feasibility planning (`POST /api/homeassistant/climate/plan`) remain strictly read-only; new actuation capability is separately registered: `homeassistant.allowlisted_climate_setpoint_actuate`.
  - Independent enablement gates: `HOME_ASSISTANT_ENABLED=true`, `HOME_ASSISTANT_ACTUATION_ENABLED=true`, `HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=true`, `HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true` (disabled by default), validated server-side `HOME_ASSISTANT_CLIMATE_ALLOWLIST`, server-configured `HOME_ASSISTANT_CLIMATE_GRID_ORIGINS`, `APPROVAL_SECRET_TOKEN`, and `homeassistant:actuate` permission.
  - Fail-closed pre-dispatch verification: fresh authoritative state read re-evaluating exact allowlist membership, single setpoint feature flag (`TARGET_TEMPERATURE`), compatible mode (`heat`/`cool`), range limits (`min_temp <= target <= max_temp`), trusted grid origin step alignment (`abs(dec_req - dec_origin) % dec_step == 0`), unit match, single-use approval ticket validity, and triple-digest agreement (`recomputed == persisted == approved`).
  - Idempotency & state satisfaction: skips physical mutation if the reported target setpoint (`temperature` attribute) already equals the requested setpoint (`dispatch_status='already_satisfied'`).
  - Single dispatch: invokes `POST /api/services/climate/set_temperature` with `{"entity_id": target_id, "temperature": target_temp}` exactly once. Strictly zero automatic retries (`max_retries=0`, `idempotent=False`, `consequential=True`) on timeouts or failures.
  - Independent read-after-write verification: polls read-only state endpoint (`GET /api/states/{id}`) to verify reported temperature against approved setpoint. Inconclusive outcomes fail closed (`verification_failed`). Never claims physical device connectivity (`physical_device_connectivity_claimed=False`).
  - REST API routes: `POST /api/homeassistant/climate/propose` (returns 201 with execution in `awaiting_approval`) and `GET /api/homeassistant/climate/execution/{execution_id}` (inspects execution status). Client `grid_origin` input strictly forbidden.
- [x] **P7.4.2.3 Stage D Read-Only Dual-Setpoint Climate Feasibility Planning**:
  - Read-only feasibility assessment enabling PICO to evaluate proposed dual-temperature setpoint ranges (`target_temp_low` and `target_temp_high`) without physical HVAC mutation.
  - Strictly read-only planning endpoint: `POST /api/homeassistant/climate/dual-setpoint/plan` returning `ClimateDualSetpointPlan`.
  - Zero Physical Actuation Guarantee: Zero mutation capabilities registered, zero service calls dispatched (`climate.set_temperature`, `climate.set_hvac_mode`, fan, preset, and generic services are never invoked). `is_mutation_performed: false` guaranteed across all valid and invalid assessment paths.
  - Operating Mode & Feature Validation: Entity must explicitly support dual-setpoint capability (bit 1 `TARGET_TEMPERATURE_RANGE` of `supported_features`) and currently be operating in `heat_cool` mode. The planner never switches operating modes.
  - Strict Range & Numeric Constraints: Both `target_temp_low` and `target_temp_high` must be non-boolean, finite numeric values with `target_temp_low < target_temp_high`. Equal or inverted ranges fail closed (`target_out_of_range`). Both values must independently lie within authoritative reported bounds `[min_temp, max_temp]`.
  - Trusted Server-Side Grid Origin Alignment: Decimal-safe alignment of both targets against entity's `target_temp_step` relative to server-configured `HOME_ASSISTANT_CLIMATE_GRID_ORIGINS`. Client-supplied `grid_origin` and unexpected fields are strictly forbidden and rejected with HTTP 422.
  - Canonical SHA-256 Digest Binding: Binds exact entity ID, lower target, upper target, requested unit, and effective server grid origin and unit (`clean_id:low:high:unit:origin:origin_unit`). Sensitive to any parameter or configuration change.
  - Test Suite: 104 climate awareness tests passing (+35 tests covering valid ranges, mode incompatibility, feature requirements, numeric safety, boundary limits, step alignment, and zero-mutation guarantees).
- [x] **P7.4.2.4 Stage D Allowlisted Dual-Setpoint Climate Actuation**:
  - Scoped physical actuation slice under Milestone P7.4.2 for dual-setpoint climate control. Supports strictly one operation: set lower and upper target temperatures (`target_temp_low` and `target_temp_high`) for an explicitly allowlisted climate entity.
  - Dedicated independent enablement gate: `HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED=true` (defaults to `False`), requiring all parent gates (`HOME_ASSISTANT_ENABLED=true`, `HOME_ASSISTANT_ACTUATION_ENABLED=true`, `HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=true`, `HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true`, non-empty `HOME_ASSISTANT_CLIMATE_ALLOWLIST`, valid server-configured `HOME_ASSISTANT_CLIMATE_GRID_ORIGINS`, `APPROVAL_SECRET_TOKEN`, and `homeassistant:actuate` permission).
  - Explicit Feature & Mode Requirements: Entity must advertise `TARGET_TEMPERATURE_RANGE` (bit 1 of `supported_features`) and currently be operating in `heat_cool` mode. Zero automatic operating-mode transitions: PICO never switches mode before, during, or after dispatch. Incompatible modes fail closed with zero mutations.
  - Strictly zero fan mode changes, preset activation, single-setpoint `temperature` adjustments, or generic service calls (`call_service`).
  - Numeric Safety & Validation: Rejects client-supplied `grid_origin`, booleans, and non-finite numeric/string values (`inf`, `-inf`, `nan`, `1e999`). Requires `target_temp_low < target_temp_high` and both within authoritative reported bounds `[min_temp, max_temp]`.
  - Fail-closed pre-dispatch verification: Fresh authoritative state read immediately prior to dispatch re-evaluating exact allowlist membership, dual-setpoint feature flag, active `heat_cool` mode, range bounds, trusted grid origin step alignment for both setpoints, unit match, single-use approval ticket validity, and triple-digest agreement (`recomputed == persisted == approved`).
  - Idempotency & state satisfaction: If reported `target_temp_low` and `target_temp_high` already equal the requested setpoints, physical dispatch is skipped (`dispatch_status='already_satisfied'`) with zero service calls.
  - Single dispatch: Exactly one HTTP `POST /api/services/climate/set_temperature` call with strictly `{"entity_id": target_id, "target_temp_low": target_temp_low, "target_temp_high": target_temp_high}`. Strictly omits `temperature` and `hvac_mode`. Strictly zero automatic retries (`max_retries=0`, `idempotent=False`, `consequential=True`) on timeouts or failures.
  - Independent read-after-write verification: Polls read-only state endpoint (`GET /api/states/{id}`) to verify both reported target setpoints match the approved setpoints. Inconclusive outcomes fail closed (`verification_failed`). Never claims physical device connectivity (`physical_device_connectivity_claimed=False`).
  - REST API routes: `POST /api/homeassistant/climate/dual-setpoint/propose` (returns 201 with execution in `awaiting_approval`) and `GET /api/homeassistant/climate/dual-setpoint/execution/{execution_id}` (inspects execution status). Strict capability ID isolation between single-setpoint and dual-setpoint execution query endpoints.
- [ ] **P7.4.2 Stage D Climate & Environmental Controls (Full Dual-Setpoint & Mode Actuation)**: In Progress (Dual-setpoint actuation completed; HVAC mode switching, fan mode control, and preset actuation remain planned and strictly unsupported).
- **Status**: **STAGE A, STAGE B, STAGE C, STAGE D PREPARATORY, STAGE D SINGLE-SETPOINT ACTUATION, STAGE D DUAL-SETPOINT FEASIBILITY PLANNING, & STAGE D DUAL-SETPOINT ACTUATION (P7.1, P7.2, P7.3, P7.4.1, P7.4.1.2, P7.4.2.1, P7.4.2.1.1, P7.4.2.1.2, P7.4.2.1.3, P7.4.2.2, P7.4.2.3, P7.4.2.4) COMPLETED & VERIFIED; ADVANCED CLIMATE MODES (MODE/FAN/PRESET SWITCHING) PLANNED**.


### Milestone P8: Computer Control & Engineering Workspace
- [x] **P8.1 OS & Application Automation**:
  - Provider-neutral OS adapter boundary (`pico.os_automation`): `BaseOSAutomationAdapter`, `WindowsAutomationAdapter`, and `FakeOSAutomationAdapter`.
  - Read-only visible desktop window inspection (`os.inspect_windows`): `RiskTier.READ_ONLY`, `os:inspect` permission, `INTERNAL` classification, hard bounding on window count (max 100) and title length (max 512), standard-library `ctypes` enumeration (`EnumWindows`), honest reporting on non-Windows platforms (`unsupported`).
  - Server-configured application allowlist (`OS_APPLICATION_ALLOWLIST`): strictly empty by default. Strict validation against path traversal (`..`), relative paths, and env var expansion (`%`, `$`). Prohibits shells and interpreters (`cmd.exe`, `powershell.exe`, `pwsh.exe`, `bash.exe`, `python.exe`, etc.) and scripts (`.bat`, `.cmd`, `.ps1`, `.vbs`, etc.).
  - Consequential application launch boundary (`os.launch_application`): `RiskTier.HIGH`, `os:launch` permission, `consequential=True`, `idempotent=False`, strictly zero automatic retries (`max_retries=0`).
  - Launch Profile Fingerprint Binding (P8.1 Hardening): Canonical SHA-256 fingerprint computed from execution-relevant launch profile attributes (alias, executable path, ordered args, working directory). Enriched at submission via `prepare_input` and cryptographically bound into human approval ticket. At execution boundary, re-verifies current allowlist profile fingerprint against approved fingerprint; any post-approval mutation to executable, arguments, working directory, or allowlist configuration fails closed with zero process creations.
  - Direct argument-list process creation with `shell=False`. Minimal sanitized child environment stripping PICO secrets, approval tokens, Home Assistant credentials, and cloud API keys (`build_sanitized_environment()`).
  - Single-use human operator approval bound to SHA-256 canonical digest; triple-digest agreement and atomic compare-and-set claim.
  - Honest launch verification: distinct statuses for `request_rejected`, `awaiting_approval`, `launch_dispatched`, `process_creation_confirmed`, `launch_uncertain`, `launch_failed`, and `independent_window_observation_confirmed`. Process creation is never equated with desktop usability.
  - 23 dedicated adversarial and functional tests in `tests/test_os_automation.py` ensuring zero real desktop side effects during CI.
- [x] **P8.2 Staged Filesystem Diffs, Security Follow-ups 1, 2, Final Replacement Safety Check & Temporary-File Identity Hardening (Handle-Bound Rename & Post-Verification Backup Preservation)**:
  - Provider-neutral filesystem boundary (`pico.filesystem`): `BaseFilesystemAdapter`, `WindowsFilesystemAdapter`, and `FakeFilesystemAdapter`.
  - Server-configured filesystem root allowlist (`PICO_FILESYSTEM_ALLOWED_ROOTS`): strictly empty by default. Target paths must resolve strictly within an authorized canonical root.
  - Fail-Closed Permissions: Removed missing-secret fallback in `submit_apply` and `submit_rollback`. `filesystem:write` permission is strictly granted only when a non-empty `APPROVAL_SECRET_TOKEN` is configured and matched via constant-time `secrets.compare_digest`. Unset secret, missing token, or mismatched token fails closed with zero mutations.
  - Windows Handle Boundary & Directory Hierarchy Pinning: Locks every directory from the root down to the target parent via `pin_directory_hierarchy` using `CreateFileW` with `FILE_LIST_DIRECTORY`, `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, and `FILE_SHARE_READ | FILE_SHARE_WRITE` (omitting `FILE_SHARE_DELETE`). The Windows NT kernel blocks external process attempts to delete, rename, or swap parent directories with junctions (`WinError 32: ERROR_SHARING_VIOLATION`).
  - Target Identity Bound Replacement: Target file opened with `GENERIC_READ | DELETE` and `FILE_SHARE_READ` (omitting `FILE_SHARE_WRITE` and `FILE_SHARE_DELETE`) during attribute and original-digest checks; relocated via its handle using `SetFileInformationByHandle(FileRenameInfo)` to `.pico_backup_*`.
  - Temporary-File Handle Identity & Non-Overwriting Rename: Staged content is written to an open temporary file handle `h_temp` (`.pico_tmp_*`) created with `GENERIC_READ | GENERIC_WRITE | DELETE` and `FILE_SHARE_READ` in the pinned parent directory. `h_temp` is held continuously open with exclusive sharing so external processes cannot tamper with, overwrite, rename, or delete it. Final rename is performed directly on `h_temp` via `SetFileInformationByHandle(FileRenameInfo)` with `ReplaceIfExists = False`. If an external process creates or substitutes a file at the target path, `SetFileInformationByHandle` fails closed (`ERROR_ALREADY_EXISTS`), marking `h_temp` for disposition and closing it, leaving the competitor's file 100% untouched and preserving the displaced original in backup with zero overwrite.
  - Post-Verification Backup Preservation: The original backup file (`.pico_backup_*`) is preserved until independent post-write handle verification (`h_post`) passes. If verification fails or cleanup encounters a conflict, the backup file is preserved on disk for recovery, the proposal remains `staged`, and the failure is recorded in the execution result and event ledger.
  - Non-Windows Fail-Closed Policy: On non-Windows platforms where Windows NT kernel handle sharing and directory pinning semantics are unavailable, atomic filesystem mutations fail closed (`ExecutionError`) rather than falling back to unverified path-based operations.
  - Handle-Based Operations & Deletion: Reads and mutations operate via open handles (`FILE_FLAG_OPEN_REPARSE_POINT`), verified via `GetFileInformationByHandle` and `GetFinalPathNameByHandleW`. Rollback deletion uses `SetFileInformationByHandle(FileDispositionInfo, True)` on verified, non-reparse handles with `FILE_SHARE_READ` and expected-digest validation, preventing redirected deletions outside the allowlisted root.
  - Fail-Closed Reparse Inspection: `is_symlink_or_reparse_point` distinguishes `FileNotFoundError` (genuinely absent file during creation) from `PermissionError` and other unexpected `OSError`/`ValueError` states, which fail closed (`ValidationError`).
  - Safe Temporary File Cleanup: `safe_unlink` verifies handles before unlinking, ensuring temporary staging files (`.pico_tmp_*`) are never unlinked if redirected outside the authorized root.
  - Exclusive file creation (`CREATE_NEW`): Kernel-level atomic creation for new files within pinned parent directory, guaranteeing concurrently created targets cannot be overwritten.
  - Dual Path Safety: Dual lexical and canonical path containment checks; rejects path traversal (`..`), alternate separators (`/` or mixed), Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`), UNC/device namespaces, and env vars (`%`, `$`).
  - Bounded UTF-8 text contract: max 256 KB file size, max 4096 char line length, max 256 KB diff size, rejects NUL bytes and binary content, LF newline normalization.
  - Allowed operations strictly limited to `create` (new text file, parent exists, target absent) and `replace` (full text replacement of existing file).
  - Staged proposal lifecycle: `StagedDiff` entity recording proposal ID, root ID, relative path, operation, expected original digest (`ABSENT` or SHA-256), proposed SHA-256, unified diff text, diff SHA-256, and lifecycle transitions (`staged`, `applied`, `rolled_back`, `discarded`).
  - Read-only staging capabilities: `filesystem.stage_diff` and `filesystem.inspect_diff` (`RiskTier.READ_ONLY`, `filesystem:read`, `INTERNAL` classification).
  - Consequential execution boundary: `filesystem.apply_staged_diff` and `filesystem.rollback_staged_diff` (`RiskTier.HIGH`, `filesystem:write`, `consequential=True`, `idempotent=False`, `max_retries=0`).
  - Triple-digest approval binding: Server `prepare_input` hook injects trusted original digest, proposed content digest, and diff digest into execution input before `input_digest` computation. Human approval ticket cryptographically binds exact operation, path, disk snapshot, proposed content, and diff.
  - Pre-dispatch gate: `ToolExecutionEngine.execute` re-verifies root allowlist membership, checks target file on disk against `expected_original_digest`, and checks stored proposed content and diff digests before execution. Post-approval tampering fails closed with zero writes.
  - Independent post-write verification: re-opens target file handle, computes SHA-256 digest, and asserts exact match against approved proposed digest before declaring success. Zero automatic retries.
  - REST endpoints at `/api/filesystem/proposals` (`POST`, `GET`, `GET /{id}`, `POST /{id}/discard`, `POST /{id}/apply`, `POST /{id}/rollback`).
- [x] **P8.3 Sandboxed Browser Automation**:
  - Provider-neutral browser adapter contract (`pico.browser`): `BaseBrowserAdapter`, `PlaywrightBrowserAdapter`, and deterministic `FakeBrowserAdapter` for offline unit testing and CI without downloading browser binaries or needing external network access.
  - Optional and lazy-loaded Playwright dependency: missing Playwright packages or missing browser binaries fail gracefully with truthful `unavailable` status; zero uncontrolled downloads in CI or test suites.
  - Fail-closed security configuration: disabled by default (`BROWSER_AUTOMATION_ENABLED=False`), server-side origin allowlist (`BROWSER_ALLOWED_ORIGINS=[]`) empty by default with zero wildcards.
  - Comprehensive SSRF mitigation: blocks loopback (`127.0.0.0/8`, `::1`), private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16`), cloud metadata (`169.254.169.254`, `metadata.google.internal`), and resolves destination hostnames.
  - Full redirect and subresource enforcement: redirects, popups, and requests are intercepted and verified against the approved-origin policy and SSRF checks.
  - Ephemeral browser context isolation: fresh, isolated browser context per request with zero persistence of cookies, local storage, sessions, or credentials; Chromium sandbox strictly maintained (never `--no-sandbox`); disabled downloads, service workers, browser permissions, and arbitrary JS execution.
  - Bounded page observation: strict limits on byte size (`max_bytes=65536`) and links count (`max_links=50`); sanitizes URL queries and fragments in observations and logs to prevent credential leaks.
  - Untrusted data & prompt injection defense: page observations carry explicit source provenance and conservative `INTERNAL` classification; strictly pinned locally via `RULE_PRIVATE_LOCAL_PIN` (zero cloud transmission); adversarial page content cannot elevate caller permissions, alter tool schemas, or bypass approvals.
  - Registered capability: `browser.open_page` (`RiskTier.READ_ONLY`, `browser:open` permission, `INTERNAL` classification, idempotent, zero retries, independent verification).
  - 15 dedicated adversarial and functional tests in `tests/test_browser_automation.py` passing 100% offline without live internet access or browser binary downloads.
- **Status**: **P8.1, P8.2 & P8.3 COMPLETED & VERIFIED; P9 COMPLETED & VERIFIED**.

### Milestone P9: Multi-Agent Workflow Coordination
- [x] **P9.1 Task Delegation — Subtask Execution over Persistent Task Engine**:
  - Durable workflow coordination layer separating user tasks (`Task`), workflow runs (`WorkflowRun`), delegated steps (`WorkflowStep`), and tool executions (`ToolExecution`).
  - Pre-dispatch DAG graph validation: cycle detection via Kahn's algorithm, self-dependency rejection, unresolved prerequisite checks, registered capability existence and version matching, input schema validation, undeclared dependency-output reference rejection, and strict graph bounds (max 50 steps, max depth 20, max payload 1MB).
  - Deterministic coordinator executing eligible ready steps in dependency order exclusively through the existing `CapabilityRegistry` and `ToolExecutionEngine`.
  - Dependency output mapping (`{"$from_step": "<step_key>", "$path": "<dot_path>"}`) with runtime validation against the destination capability's declared schema.
  - Human approval integrity: consequential steps pause workflow advancement at `awaiting_approval` and require verified human approval before dispatch. Input-digest bindings prevent step mutation after approval.
  - Truthful restart recovery: interrupted runs reconciled against persistent tool execution records and immutable event ledger evidence without blind replay of non-idempotent operations.
  - Comprehensive adversarial test suite covering cycles, invalid schemas, missing dependencies, approval halts, idempotent deduplication, cancellation, crash recovery, and REST API routes (`/api/workflows`).
  - 18 dedicated tests in `tests/test_workflow_coordination.py` passing cleanly (425 total passing tests across entire regression suite).
- [x] **P9.2 Single-Model Resource Scheduling — Logical Workers Scheduled Over Single Resident Model**:
  - Provider-neutral model-execution scheduling layer (`pico.scheduler`): `ResourceScheduler`, `PriorityFairQueue`, `LocalAdmissionGate`, `CloudAdmissionGate`, and truthful hardware telemetry collector (`SchedulerTelemetryCollector`).
  - Strict single-model residency invariant: enforced hard concurrency limit of 1 on PICO-managed local inference leases. Rejects conflicting local model instantiations (`ModelResidencyConflictError`) without downloading, killing, or unsetting Ollama models.
  - Logical worker multiplexing: workers (`planner`, `researcher`, `executor`, `summarizer`, `routine`, `interactive`) multiplex over the single configured resident model (`qwen3.5:2b` / `settings.OLLAMA_MODEL`).
  - Fair priority scheduling: multi-tier priority classes (`interactive`, `workflow`, `background`) with aging fairness (aging bonus every 5.0s) preventing indefinite background starvation.
  - Full-stream lease retention: local inference leases are acquired before streaming begins and held for the entire token stream lifetime, released deterministically in `finally` cleanups upon completion, client disconnect (`CancelledError`), or failure.
  - Bounded queue & backpressure: max queue depth (50) rejecting excess requests with `QueueFullError` (HTTP 429), deadline timeouts (`SchedulingTimeoutError` / HTTP 504), cooperative cancellation, and duplicate ticket protection.
  - Privacy gate & cloud boundary: central privacy gate remains authoritative; non-public context (`INTERNAL`, `SENSITIVE`, `SECRET`, missing/unknown) is strictly pinned locally; cloud requests require `PUBLIC` data and explicit enablement; cloud fallbacks acquire the local execution lease.
  - Workflow coordinator integration: `WorkflowService.cancel_workflow` purges queued model tickets immediately; `model.infer` registered capability (`RiskTier.READ_ONLY`) enables workflow steps to execute governed inference via `ToolExecutionEngine`.
  - Operational diagnostics & REST API: `/api/scheduler/status` and `/api/scheduler/metrics` providing sanitized metrics, queue depth, and memory telemetry without leaking prompts or credentials.
  - Comprehensive adversarial test suite in `tests/test_resource_scheduler.py` (16 dedicated tests covering residency, worker sharing, queue bounds, deadline timeouts, cancellation, streaming abort lease cleanup, cloud privacy, workflow cancellation, and REST API).
- [x] **P9.3 Independent Verification**: Verifier audits worker outputs before dependent tasks proceed.
  - Distinct verification stage: outputs gated until verified (`verifying` -> `succeeded` / `verification_failed` / `review_required`); downstream steps cannot consume unverified outputs.
  - Multi-stage pipeline: Stage A (structural integrity), Stage B (execution evidence cross-check), Stage C (domain-specific semantic & postcondition verification), Stage D (risk-sensitive policy decision).
  - Model-assisted semantic review: optional `WorkerRole.VERIFIER` scheduled via `ResourceScheduler` over single resident local model; untrusted verdicts cannot override failed deterministic checks; non-public context pinned locally.
  - Reverse-dependency compensation: explicit compensation registry executing in reverse dependency order; consequential compensations require prior operator approval.
  - Durable persistence: `workflow_verifications` and `workflow_compensations` entities with full foreign keys, SQLite WAL indexing, and Alembic migrations.
  - Dedicated adversarial test suite in `tests/test_workflow_verification.py` (13 tests covering arithmetic gating, tampered output rejection, inconclusive reviews, scheduler lease acquisition, crash recovery, reverse compensation, consequential approval halts, cancellation, and REST API).
- **Status**: **P9.1, P9.2 & P9.3 COMPLETED & VERIFIED**.

### Milestone P10: Unified PICO Experience
- [x] **P10.1 Desktop Web Experience**: Streaming chat, voice push-to-talk, memory inspector, workflow timeline, approvals, system status.
  - **Frontend stack**: React 19, TypeScript 5.7, Vite 6, Vitest, pnpm — `frontend/` directory.
  - **Streaming chat**: fetch-based SSE parser (`frontend/src/api/sse.ts`) handling split chunks, multi-event packets, UTF-8 boundaries, completion, error, and user-cancellation; no automatic resubmission; no duplicate messages.
  - **Operator session & security** (`pico/services/operator_session.py`, `pico/api/operator.py`): local loopback-only pairing via 6-digit one-time code (300 s TTL), `HttpOnly SameSite=Strict` session cookie, timing-safe CSRF token; `APPROVAL_SECRET_TOKEN` never reaches browser or logs; 6 dedicated tests in `tests/test_operator_session.py`.
  - **Consequential approvals**: `ApprovalInbox` locks action buttons and directs user to pair before enabling; backend validates operator session + CSRF on every `/api/tools/executions/{id}/approve` call; `authenticated_principal="local_operator"` replaces raw token on server side.
  - **Components**: `ChatPanel` (streaming conversation), `VoiceControl` (explicit PTT, `user_consent=true`), `WorkflowTimeline` (DAG + verification gates A–D), `MemoryInspector` (FTS5 semantic search, confirmed deletion), `ApprovalInbox`, `SystemStatus`, `PairingModal`.
  - **SPA serving**: FastAPI middleware fallback (not a catch-all `GET` route) serves `frontend/dist` for non-API 404s; API routes at any method remain properly isolated; test `test_spa_fallback_serving_and_api_isolation` verifies `POST /api/nonexistent` → 404, not 405 or SPA.
  - **CI**: `frontend` job in `.github/workflows/ci.yml` (Node 22, pnpm 10, frozen lockfile, typecheck, vitest, vite build).
  - **Test suite**: 20 frontend tests (5 SSE, 4 chat, 3 voice, 2 memory, 3 workflow, 3 approvals) — all passing; 460 backend tests — all passing.
  - **Acceptance criteria met**: zero secrets in bundle; zero auth weakening; all pre-existing backend tests green; `git diff --check` clean; `pnpm run typecheck` clean; `pnpm run build` 300 kB gzipped bundle.
- [x] **P10.1.1 Post-Merge Security Closure & Authorization Integrity**:
  - **Finding A (Operator Pairing Process Boundary)**: Resolved cross-process in-memory singleton limitation by persisting short-lived pairing challenges in the shared SQLite database (`operator_pairing_challenges`, `operator_sessions`). Removed self-service browser minting endpoint `POST /api/operator/pairing-code` and its UI control; pairing codes are strictly issued by the trusted local CLI (`python -m pico.cli.pair`). Challenges feature salt-hashed storage, 300s TTL, atomic single-use consumption (`consumed_at`), rate limiting with 5 failed attempt lockout, and server-bound operator identity (`local_operator`).
  - **Finding B (Untrusted Client Workflow Permissions)**: Eliminated trust in request-supplied `caller_permissions` in `WorkflowAdvanceRequest` and `WorkflowCompensateRequest`. Permissions are derived authoritatively on the server from the verified operator session or `X-Pico-Approval-Token`. Unauthenticated requests fail closed for capabilities requiring privileged permissions.
  - **Finding C (Consequential Approval & Dispatch Paths)**: Dynamically check `capability.metadata.consequential` across all registered capabilities in `ToolExecutionEngine.approve()` and `POST /api/tools/executions/{id}/approve`. Completely removed legacy fallback accepting self-asserted approver identity strings matching `TRUSTED_APPROVER_IDENTITIES` without an active session or valid approval token. Removed `"admin:consequential"` from default execution permissions in tool execution endpoints. Recorded authenticated principal into the execution record and event ledger.
  - **Test Suite**: 467 backend tests passing (+7 new tests across `tests/test_operator_session.py`, `tests/test_workflow_authorization.py`, and `tests/test_tool_authorization_approval.py`), 20 frontend tests passing.
- [x] **P10.1.2 Close Residual Default-Permission Leak & Scope Authorization**:
  - **Finding A (`admin:consequential` Residual Default Leak)**: Removed `"admin:consequential"` from default submission permissions in `submit_execution` (`pico/api/tools.py`). Unauthenticated requests submitting consequential capabilities fail closed with HTTP 403 Forbidden without creating database execution records or side effects.
  - **Finding B (Canonical Local Operator Identity)**: Removed arbitrary `--approver` parameter from CLI (`pico/cli/pair.py`) and schemas. Binds operator identity strictly to `LOCAL_OPERATOR_IDENTITY = "local_operator"` in trusted server code (`pico/services/operator_session.py`), ensuring neither HTTP payloads nor CLI flags can override the authenticated approver identity in approval records.
  - **Finding C (Workflow Permission Scoping)**: In `pico/api/workflows.py`, scoped `derive_workflow_permissions` to the minimum safe set required by the specific workflow run steps or registered compensations (`SAFE_DEFAULT_PERMISSIONS | (required & PRIVILEGED)`), rather than granting all privileged permissions indiscriminately.
  - **Test Suite**: 470 backend tests passing (+3 new regression and adversarial tests across `tests/test_tools_api.py` and `tests/test_workflow_authorization.py`), 20 frontend tests passing.
  - **Acceptance criteria met**: Zero unauthenticated consequential submissions permitted; zero client approver identity overrides; workflow permissions scoped to least privilege; `git diff --check` clean; frontend typecheck, tests, and build clean; `scripts/pico-verify.ps1` clean pass.
- [x] **P10.2 Offline Resilience & Safe Recovery**: Graceful degradation when inference or network is offline without leaking private data or causing duplicate operations.
  - **Typed Connectivity Layer**: `ConnectivityManager` and `ConnectivityProvider` distinguishing `online`, `degraded`, `offline`, `reconnecting`, and `unknown` states. Single-flight health probes, bounded exponential backoff (3s base, 1.5x factor, 30s ceiling) with jitter, online/offline/visibility event listeners, and separate core backend reachability from local model/inference degradation.
  - **Offline Application Shell**: Service worker (`frontend/public/sw.js`) caching exclusively static shell assets (`/`, `/index.html`) and versioned build bundles (`/assets/*`). Strict data exclusion: zero caching of `/api/*` endpoints, private conversations, messages, memory records, tool inputs/results, operator pairing tokens, session cookies, CSRF tokens, or approval records in SW cache or browser persistent storage (`localStorage`, `sessionStorage`, `IndexedDB`). Network-only API handling; zero interception or replay of mutating requests (`POST`, `PUT`, `DELETE`).
  - **Structured API Error Model**: Typed error hierarchy (`ApiError`, `NetworkError`, `TimeoutError`, `AuthenticationError`, `ProviderUnavailableError`) with bounded 15s timeouts; distinguishes transport failures from HTTP 401/403/404/422 status codes.
  - **In-Memory Draft Outbox**: Bounded in-memory queue (max 25 items) retaining unsent user messages during backend disconnections or conversation creation failures. Ephemeral memory-only lifecycle; zero auto-submission upon reconnection (explicit user dispatch required).
  - **Ambiguous Outcome & Zero Auto-Replay**: Interrupted SSE chat streams tagged `[Response stream interrupted]` and marked with an ambiguous outcome notice (never marked completed). Mutating operations (tool execution proposals, approvals, workflow advances, compensations, memory forget, voice sessions) with dropped responses marked "Outcome unknown"; strictly zero automatic retries or duplicate dispatches.
  - **Operator Session Revalidation on Reconnect**: Immediate revalidation against `GET /api/operator/session` upon reconnection; consequential controls remain locked until session validity is confirmed by the backend.
  - **State Retention & Stale Indicators**: Retains last in-memory views across `ChatPanel`, `WorkflowTimeline`, `ApprovalInbox`, `MemoryInspector`, `SystemStatus`, and `VoiceControl` with visible stale badges and timestamps.
  - **Test Suite**: 38 frontend tests passing (+18 new tests across `connectivity.test.ts`, `connectivity-context.test.tsx`, `offline-shell.test.ts`, and `resilience.test.tsx`), 470 backend tests passing.
  - **Acceptance criteria met**: Offline shell opens without live backend; zero private data persisted to browser caches; zero automatic replays of mutating actions; operator session revalidated on reconnect; `git diff --check` clean; frontend typecheck, vitest, and build clean; `scripts/pico-verify.ps1` clean pass.
- [x] **P10.2.1 Offline Recovery, Mutation Safety & Cache Boundary Hardening**: Corrective hardening of connectivity lifecycle, service worker cache boundaries, and entity-scoped ambiguous-outcome mutation locks.
  - **Connectivity Lifecycle & StrictMode Safety**: Eliminated disposed-instance restart hazards in `ConnectivityProvider` (`frontend/src/context/ConnectivityContext.tsx`). Instantiates a fresh viable manager if previous was disposed during React StrictMode effect replay; decoupled `onReconnected` callback identity using mutable reference so callback reference changes do not tear down or re-instantiate the manager; triggers operator session revalidation on confirmed backend reachability (`online` or `degraded`) even if local inference runtime is degraded.
  - **Service Worker Cache Allowlist**: Hardened `frontend/public/sw.js` (cache version `pico-shell-v2`) with an explicit static asset allowlist (`/`, `/index.html`, `/favicon.ico`, `/manifest.webmanifest`, `/manifest.json`, `/vite.svg`, `/assets/*`). Strictly bypasses cross-origin requests, non-GET mutations, all `/api/*` endpoints, `/health`, and operational/schema endpoints (`/openapi.json`, `/docs`, `/redoc`).
  - **Entity-Scoped Ambiguous Mutation Locks**: Enforced unambiguous locking across mutating controls in `ApprovalInbox`, `WorkflowTimeline`, `MemoryInspector`, and `VoiceControl` following network drop or timeout. Replaced generic retry options with explicit entity-scoped "Reconcile" actions that query authoritative server state (`GET /api/tools/executions/{id}`, `GET /api/workflows/{id}`) before releasing locks.
  - **Workflow Verification State Retention**: Preserved in-memory verification records with a stale badge and timestamp on fetch failure in `WorkflowTimeline` instead of clearing them.
  - **Voice Cancellation Resilience**: Removed swallowing of voice cancellation network errors in `VoiceControl`; displays "Outcome unknown" and preserves `activeSessionId` until reconciled.
- [x] **P10.2.2 Residual Recovery Safety Closure**: Focused closure of residual recovery, listener lifecycle, and ambiguous reconciliation gaps across frontend and backend.
  - **Eliminate ConnectivityManager Construction & Listener Leaks**: Refactored `ConnectivityContext.tsx` to lazily instantiate an internal manager only when needed; properly cleans up listeners (`online`, `offline`, `visibilitychange`) and timers on unmount; prevents eager construction of discarded instances during parent rerenders; ensures clean ownership lifecycle for both internal and injected managers.
  - **Restrict Service-Worker Cleanup to PICO-Owned Caches**: Scoped `sw.js` cache activation cleanup to delete only obsolete caches prefixed with `pico-shell-`; preserves active PICO cache (`pico-shell-v2`) and all unrelated third-party caches on the same origin; bounds static `/assets/` caching strictly to explicit static asset extensions (`.js`, `.css`, `.png`, `.jpg`, `.jpeg`, `.svg`, `.webp`, `.woff`, `.woff2`, `.ttf`, `.ico`, `.json`, `.webmanifest`).
  - **Safe Voice-Session Recovery**: Added entity-scoped ambiguous voice operation state (`AmbiguousVoiceOperation`) in `VoiceControl.tsx`; disables repeat start, stop, and cancel mutations during ambiguous outcomes; implemented minimal read-only backend status contract `GET /api/perception/audio/session/{session_id}` returning `AudioSessionStatusResponse` without leaking audio buffers or transcriptions; unlocks controls only upon authoritative resolution; missing sessions (404 / backend restart) and capturing state remain inconclusive and keep mutations safely locked without fabricating certainty.
  - **Action-Specific Workflow Reconciliation**: Upgraded `WorkflowTimeline.tsx` to record pre-mutation snapshot (`previousStatus`, `previousCompletedSteps`, `previousStepStatuses`) and reconcile based on the specific recorded action (`advance`, `cancel`, `compensate`). Advancement requires step transition or completed count increase; cancellation requires cancelled or terminal status; compensation inspects authoritative records via `/api/workflows/{id}/compensations`. A generic running or unchanged status remains locked and reports limitations honestly.
  - **Test Suite**: 60 frontend tests passing (+12 new tests across `connectivity-context.test.tsx`, `offline-shell.test.ts`, `voice.test.tsx`, and `workflows.test.tsx`), 470 backend tests passing (+1 new test in `test_perception_api.py`).
- [x] **P10.2.3 Render-Safe Connectivity Lifecycle & Bounded Static Cache**: Focused correctness patch resolving StrictMode render-phase listener registration and service worker static asset response validation / cache bounding.
  - **Render-Safe Connectivity Lifecycle**: `ConnectivityManager` constructor in `frontend/src/api/connectivity.ts` is completely side-effect-free (no listeners attached, no timers started, no health probes triggered during render). Introduced explicit `start()` and `stop()` lifecycle methods; `start()` is idempotent for viable managers and prohibited on disposed managers; `dispose()` is idempotent and tears down all listeners, timers, and subscribers.
  - **StrictMode Replay & Manager Ownership**: `ConnectivityProvider` (`frontend/src/context/ConnectivityContext.tsx`) defers all side effects, listener attachments, and manager disposals to `useEffect`. StrictMode initial mount-unmount-remount replay recovers cleanly without orphaned instances or leaked window/document event listeners. Cleanly separates internally owned managers (disposed on unmount/replacement) from externally injected `customManager` instances (stopped on unmount/replacement, preserving external disposal ownership).
  - **Static Resource Content-Type Validation**: `sw.js` validates that fetched responses under `/assets/` not only match allowed extensions and single-level directory structure (`/^\/assets\/[a-zA-Z0-9_\.-]+\.[a-zA-Z0-9]+$/`), but also carry compatible MIME `Content-Type` headers. HTML or JSON fallback error payloads returned with 200 OK for scripts, styles, images, or fonts are rejected from Cache Storage.
  - **Deterministic Bounded Cache Budget**: Enforced maximum cache capacity (`MAX_CACHE_ENTRIES = 50`) in `sw.js` via `enforceCacheBudget()`. Evicts oldest dynamic assets while strictly preserving essential offline application shell precache entries (`/`, `/index.html`, `/favicon.ico`, etc.).
  - **Test Suite**: 72 frontend tests passing (+12 new tests across `connectivity-context.test.tsx` and `offline-shell.test.ts`), 470 backend tests passing.
- [x] **P10.2.4 Stop-Safe Probes & Lifecycle Finality**: Targeted lifecycle-closure patch eliminating in-flight health probe races on manager stop/disposal and ensuring lifecycle finality.
  - **In-Flight Probe Invalidation & Monotonic Lifecycle Generation**: Introduced monotonic `lifecycleGeneration` token in `ConnectivityManager` (`frontend/src/api/connectivity.ts`). In-flight probes capture generation at start and validate before updating state, notifying subscribers, or scheduling follow-up probes. Probes resolving or rejecting after `stop()` or `dispose()` are completely discarded, preventing revival of automatic polling loops or retry timers.
  - **Controlled Re-Start & Permanent Disposal**: Calling `stop()` cleans up all active timers, in-flight references, and DOM event listeners without marking the instance disposed. Stopped non-disposed managers can be restarted safely via `start()` without duplicate listeners or state corruption. Calling `dispose()` permanently finalizes the manager; subsequent `start()` calls remain strictly ineffective.
  - **Detached Manager Quiescence**: When `ConnectivityProvider` detaches from an externally owned manager, the manager stops cleanly and remains idle after in-flight probes settle, with zero timers or DOM listeners.
  - **Test Suite**: 83 frontend tests passing (+11 new tests across `connectivity.test.ts` and `connectivity-context.test.tsx`), 470 backend tests passing.
  - **Acceptance criteria met**: Stopped managers never schedule subsequent automatic timers; in-flight probe resolution after `stop()` cannot restart polling; restart works cleanly without listener duplication; disposal remains permanent; `git diff --check` clean; frontend typecheck, vitest, and build clean; `scripts/pico-verify.ps1` clean pass.
- [x] **P10.3.2 Operator Authorization & Standalone Execution Lifecycle Closure** (COMPLETED & MERGED, PR #46, Commit `c210136`): Full closure of consequential execution authorization, Home Assistant physical-actuation configuration gates, protected execution history reads, and standalone execution dispatch lifecycle.
  - **Policy A — Paired Local Operator Authorization**: Unified authorization model distinguishing loopback clients from remote clients:
    - *Local desktop browser*: Requires valid paired operator session (HttpOnly cookie) + session-bound CSRF token (`X-CSRF-Token`) for mutating requests, validated strictly on genuine loopback connections (`127.0.0.1`, `::1`). Non-loopback clients presenting operator session cookies are strictly rejected fail-closed.
    - *Remote/headless API client*: Requires valid `X-Pico-Approval-Token` matching server-side `APPROVAL_SECRET_TOKEN` via constant-time comparison.
    - *Authoritative approver identity*: Server derives principal identity (`local_operator` or `authenticated_token_holder`); caller-supplied approver strings in requests are ignored.
    - *Elevated permissions*: Server derives permissions based strictly on verified authentication context and capability registry metadata; client-provided permission lists are ignored.
    - *Home Assistant proposal parity*: Dedicated `/api/homeassistant/*/propose` endpoints accept paired local operator sessions on loopback without requiring `X-Pico-Approval-Token`.
  - **Mandatory Server-Side Actuation Configuration Gate**: Non-negotiable security requirement that `APPROVAL_SECRET_TOKEN` must be configured (non-None, non-empty, non-whitespace) on the PICO server before any physical Home Assistant actuation dispatch:
    - Enforced across all 4 physical capabilities: test light (`homeassistant.test_light_actuate`), switch (`homeassistant.allowlisted_switch_actuate`), single-setpoint climate (`homeassistant.allowlisted_climate_setpoint_actuate`), and dual-setpoint climate (`homeassistant.allowlisted_climate_dual_setpoint_actuate`).
    - Enforced at both precondition checks and the capability execution boundary in `ToolExecutionEngine.execute()` immediately before physical dispatch.
    - A valid paired operator session does NOT waive this configuration gate. If the token is removed after approval, dispatch fails closed. 0 physical mock or real calls are made on failure.
  - **Protected Execution History & Status Reads**: All execution history endpoints (`GET /api/tools/executions`, `GET /api/tools/executions/{id}`, `GET /api/tools/executions/{id}/events`) and Home Assistant status endpoints (`GET /api/homeassistant/actuation/{id}`, `/switch/{id}`, `/climate/execution/{id}`, `/climate/dual-setpoint/execution/{id}`) strictly enforce Policy A: loopback requires active operator session or approval token; remote requires valid approval token. Tested across all 9 matrix cases.
  - **Standalone Desktop Execution Lifecycle**: Desktop operator interface (`ApprovalInbox.tsx`, `client.ts`) cleanly separates the lifecycle stages:
    - Approving an execution transitions status to `queued` without silently auto-dispatching.
    - Approved executions remain persistently visible under "Approved — Ready to Dispatch".
    - Explicit "Dispatch Execution" button calls `POST /api/tools/executions/{id}/execute` exactly once.
    - Atomic compare-and-set claim (`running` state) and triple-digest binding prevent double-dispatch and tampering.
    - Network failure during dispatch marks outcome uncertain, locks controls, and displays an explicit "Reconcile" action querying authoritative backend state before releasing locks.
    - Authoritative execution outcomes and independent verification statuses (`passed`, `failed`, `inconclusive`) are rendered distinctly.
  - **Test Suite**: 723 backend tests passing (+13 new tests in `tests/test_p10_3_2_operator_execution_lifecycle.py`), 125 frontend tests passing (+4 new tests in `standalone_execution_lifecycle.test.tsx`).
  - **Acceptance criteria met**: Strict Policy A enforcement across generic and Home Assistant endpoints; mandatory physical-actuation configuration gate across all 4 capabilities; execution history reads protected against unauthorized local and remote callers; standalone approval-to-dispatch lifecycle fully closed in desktop UI; `git diff --check` clean; frontend typecheck, vitest, and build clean; full test suites passing.
- [x] **P10.3.3 Independent Post-Merge Assurance & Canonical Roadmap Reconciliation**:
  - Independent post-merge security and architecture audit verifying Policy A authorization, mandatory physical actuation configuration gate (`APPROVAL_SECRET_TOKEN`), protected execution history/status reads across all 9 caller combinations, and desktop standalone execution lifecycle.
  - Test suite resilience hardening: increased PowerShell test runner timeout ceiling in `tests/test_automation_scripts.py` from 30s to 60s to eliminate flakiness under system disk contention.
  - Comprehensive repository verification: 723 backend tests passing (100% pass rate in `scripts/pico-verify.ps1`), 125 frontend unit and integration tests passing (`pnpm --dir frontend run test`), strict TypeScript type checking clean (`pnpm --dir frontend run typecheck`), production Vite bundle building cleanly (`pnpm --dir frontend run build`), and zero git whitespace or conflict marker errors (`git diff --check`).
  - Canonical documentation synchronization across `docs/implementation-plan.md`, `docs/architecture.md`, `README.md`, and `AGENTS.md`.
  - Comprehensive remaining roadmap assessment identifying functional integration gaps and proposing next canonical milestones.
  - **Test Suite**: 723 backend tests passing, 125 frontend tests passing (100% pass rate).
- **Status**: **P10.1, P10.1.1, P10.1.2, P10.2, P10.2.1, P10.2.2, P10.2.3, P10.2.4, P10.3.2 & P10.3.3 COMPLETED & VERIFIED**.

### Milestone P11: Conversational Autonomy & Windows Desktop Companion

**Core Principle**:
> The PICO Desktop Companion is an expressive presentation, perception-input, and interaction surface for the existing PICO personal intelligence system. It is not an independent assistant. It strictly reuses PICO's model gateway, context compiler, memory subsystem, perception adapters, capability registry, workflow engine, Policy A authorization, append-only event ledger, and offline resilience infrastructure. It does not introduce duplicate reasoning, memory, authorization, or execution systems.

#### Hardware Target & Operational Baseline:
- **Host**: Windows 11 on Intel Core i5-13420H, 16 GB System RAM, NVIDIA RTX 3050 Laptop GPU (4 GB VRAM), ~60 GB available disk.
- **Inference**: Strict single-model residency in local inference runtime (`qwen3.5:2b` via Ollama), 4096-token budget ceiling, zero unreviewed/automatic model downloads. Multi-step autonomous planning for `local-small` is disabled by default (`WORKFLOW_SYNTHESIS_ENABLED=False`) and gated behind passing benchmark evaluation.
- **Privacy & Security**: Highest data classification wins (`SECRET` > `SENSITIVE` > `INTERNAL` > `PUBLIC`); non-PUBLIC data pinned locally (`RULE_PRIVATE_LOCAL_PIN`); raw audio and screen frames are sensitive and never retained or forwarded to cloud; all consequential actions require explicit human operator approval under Policy A; physical Home Assistant actuation requires server-side `APPROVAL_SECRET_TOKEN` configuration.

---

- [ ] **P11.1 Autonomous Conversational Task Decomposition & Workflow Synthesis** (Implementation Complete, Awaiting Hardware Qualification):
  - **Objective**: Bridge natural-language conversational chat directly to PICO's persistent Task and Workflow Engine (`pico.workflow.service`).
  - **Scope**:
    - Structured intent extraction & DAG proposal: Transform unstructured user requests into valid, typed workflow DAGs (`WorkflowDefinition`) with step keys, registered capability names, versions, dependencies, and parameter bindings. Multi-step autonomous planning for `local-small` (`qwen3.5:2b`) is disabled by default (`WORKFLOW_SYNTHESIS_ENABLED=False`) and gated behind passing benchmark evaluation; fails closed unless an approved model profile with passing benchmark evidence is active.
    - Strict schema validation: Synthesized DAGs validated before submission via Kahn's algorithm for acyclicity, schema compliance via `CapabilityRegistry`, bounded size (max 50 steps, max depth 20), and resolution of `$from_step` output bindings.
    - Non-negotiable invariant: The LLM may propose plans, but can NEVER grant itself capabilities, permissions, or approvals. Consequential steps pause at `awaiting_approval`.
    - Conversational explanation: Model articulates proposed workflow steps, inputs, risks, and required approvals clearly to the user before submitting.
    - REST API & Desktop UI integration: `/api/workflows/synthesize` endpoint returning proposed DAG for review; visual representation in `WorkflowTimeline`.
  - **Prerequisites**: P9.1, P9.2, P9.3, P10.1, P10.3.2.
  - **Acceptance Criteria**:
    - Unstructured prompt compiles into valid multi-step DAG matching declared capability schemas.
    - Invalid schemas, cyclic dependencies, or unregistered capability proposals fail closed with structured validation errors.
    - Consequential steps pause execution and require explicit operator authorization via Policy A.
    - Reversible steps bind to registered compensations.
    - 100% test pass rate across new adversarial synthesis tests without live model or network dependencies.

- [ ] **P11.2 Windows Desktop Companion Shell & Secure Local Bridge**:
  - **Objective**: Establish a compact, unobtrusive Windows desktop presentation and interaction surface for PICO.
  - **Sub-Milestones**:
    - [x] **P11.2.0 Desktop Companion Architecture Evaluation & ADR**: Completed in [ADR 0001: Windows Desktop Companion Architecture](decisions/0001-windows-desktop-companion-architecture.md) selecting Tauri 2 + Rust.
    - [x] **P11.2.1 Native Desktop Shell & System Tray Foundation**: Completed and verified — Tauri 2 + Rust desktop shell scaffolded, frameless window with drag region and controls, system tray with 5 menu items, close-to-tray lifecycle, CI Windows runner build, 144 UI tests.
    - [x] **P11.2.2 Companion Loopback Bridge, Process Verification & Operator Session Security**: Completed and accepted across P11.2.2.1 (Supervisor & Job Objects), P11.2.2.2 Slice 1A (Windows reverse TCP 4-tuple peer verification, private Job Object verification, same-socket Hyper transport, HMAC-SHA256 startup challenge, fail-closed connection lifecycle), and P11.2.2.2 Slice 1B (Native Operator Pairing, Volatile Session Store, per-request `X-Pico-Bridge-Proof: v2` HMAC authentication, Non-decreasing Effective UTC Clock, Atomic Monotonic Replay Protection with inclusive 60s cache expiry and 10,000 capacity, and explicit desktop runtime profiles).
    - [x] **P11.2.3 Expressive Companion Presence & Visual State Machine**: Completed and verified — Companion Visual State Machine (`CompanionStateMachine`), vector SVG eye visual identity (`CompanionEyes`) with natural blinking and pause-on-minimize/hide lifecycle, dual presence display (`CompanionPresence` in both compact floating desktop widget mode and full Command Center header mode), critical truthfulness invariants (operational states without connected active authoritative signals rejected from active display), sequence-guarded asynchronous response filtering, and accessibility (`prefers-reduced-motion` overrides and ARIA live status region).
    - [x] **P11.2.4.1 Native Idle Companion & Deferred Command Center Architecture**: Completed and merged in PR #72 — Native GDI+ expressive eyes, zero idle WebView2 processes, deferred React Command Center lifecycle, fail-closed supervisor & single-instance integration; proposed architecture documented in [ADR 0004](decisions/0004-native-idle-companion-and-deferred-command-center.md) (`Status: Proposed` pending final end-to-end physical and installer qualification).
    - [ ] **P11.2.4.2 Windows Installer & Final Desktop Qualification**: In progress — Bundled CPython 3.14.10 embeddable runtime with SHA-256 hash-locked `win_amd64` dependencies (`tools/packaging/stage_windows_bundle.py`, `tools/packaging/requirements-windows-runtime.lock`), reproducible SHA-256-verified NSIS 3.13 toolchain provisioning (`tools/packaging/provision_nsis.ps1`), fail-closed packaged mode with 45s cold-boot Core verification headroom (`frontend/src-tauri/src/lib.rs`), per-user `%LOCALAPPDATA%\PICO\data\pico.db` persistence, NSIS `currentUser` packaging with `embedBootstrapper` (`frontend/src-tauri/tauri.conf.json`), `$UpdateMode <> 1`-guarded HKCU Run uninstall hook (`frontend/src-tauri/nsis-hooks.nsh`), local Windows 11 60s+ CPython 3.14.10 packaged idle RAM qualification (`48.25 MiB` avg WS, `5.82 MiB` avg PrivWS, 0 idle WebView2, `18.4s` cold boot, Core `PRAGMA integrity_check = ok`, Job Object exit verified), and GitHub Actions NSIS `0.1.0 -> 0.1.1` upgrade + `/UPDATE` autostart preservation + genuine uninstall + 60s idle qualification pipeline (`.github/workflows/ci.yml`, `tools/packaging/qualify_windows_installer.ps1`) implemented; full CI run on `fix/p11-2-4-2-post-merge-qualification` and interactive desktop UI/drag/Command Center open-close physical verification remain pending before marking P11.2.4 complete and accepting ADR 0004.
  - **Scope**:
    - Compact floating presence: Frameless, transparent or styled desktop widget with draggable positioning, minimize-to-tray, and hide/pause controls.
    - Expressive visual identity: Animated two-eye display or expressive PICO iconography reflecting real-time backend state:
      - `idle` (calm ambient scanning/blinking)
      - `listening` (active audio capture indicator)
      - `thinking` (model scheduling / inference lease acquired)
      - `speaking` (local TTS playback in progress)
      - `executing` (active tool execution / workflow in flight)
      - `awaiting_approval` (pulsing amber indicator directing attention to approval inbox)
      - `offline` (calm disconnected indicator, cached shell)
      - `error` (truthful diagnostics badge)
    - System tray integration: Tray icon, context menu (Status, Open Full UI, Pause Perception, Settings, Quit), optional launch-at-login toggle.
    - Compact conversational panel: Quick flyout chat window for typing, viewing responses, and one-click access to approval tickets.
    - Secure local bridge: Communicates with FastAPI backend exclusively over loopback IPv4 (`127.0.0.1` / `localhost`; bracketed IPv6 `[::1]` validation deferred); requires a Policy A paired operator session (`pico_operator_session` cookie) for all bridge requests (including read-only status, conversation, and perception access) and validates the session-bound CSRF token (`X-CSRF-Token`) for mutating requests; zero plaintext secret storage in companion assets; zero alternate privileged execution paths.
    - Architectural Decision Record (ADR): Systematic evaluation of Windows desktop technologies (Tauri/Rust, PySide/Qt, Electron/C# WebView2) documented in [ADR 0001: Windows Desktop Companion Architecture](decisions/0001-windows-desktop-companion-architecture.md), selecting Tauri 2 + Rust based on <100MB idle RAM budget and 100% React/Vite code reuse.
  - **Prerequisites**: P10.1, P10.2, P10.3.2.
  - **Acceptance Criteria**:
    - [x] ADR documented and approved before code implementation: [ADR 0001](decisions/0001-windows-desktop-companion-architecture.md) accepted.
    - Shell launches on Windows 11 with transparent background and system tray integration.
    - Visual state transitions mirror authoritative backend state without latency lag or desync.
    - Companion authenticates via Policy A paired operator session; unauthorized mutations fail closed.
    - Memory footprint stays within baseline budget (<100 MB RAM idle).

- [ ] **P11.3 Local Voice Runtime & Optional Wake-Word**:
  - **Objective**: Extend P6 audio infrastructure with genuine local speech recognition and text-to-speech runtimes, with an optional privacy-safe wake-word detector.
  - **Scope**:
    - Local Speech-to-Text (STT): Replace mock STT with an embedded, efficient local Whisper runtime (e.g., `faster-whisper` / `whisper.cpp` int8/fp16 quantized model) running on CPU or dedicated RTX 3050 VRAM without violating single-model residency.
    - Local Text-to-Speech (TTS): Replace mock TTS with an efficient, natural local TTS engine (e.g., `piper-tts` or Windows native SAPI/Media Foundation) producing low-latency spoken responses without external cloud calls.
    - Push-to-Talk primary mode: Companion push-to-talk button or configurable global Windows hotkey (e.g., `Win+Alt+P`) initiating explicit audio capture session.
    - Optional wake-word detection: Highly optimized, local-only wake-word engine (e.g., `openWakeWord`) listening strictly for "Hey PICO" or "PICO". Disabled by default (`WAKE_WORD_ENABLED=False`).
    - Explicit privacy controls: Prominent microphone-active indicator on companion face and tray; global mic mute toggle; instant capture cancellation (`Esc` or release); zero raw audio persistence on disk; zero raw or base64 audio in generic event ledger (companion speech-capture ledger events record only non-audio metadata and a reference to `perception_events` via `perception_event_id`); zero cloud audio forwarding.
    - Audio session lifecycle: Integrates with `AudioSessionManager` and typed `AudioCaptureState` (`CAPTURING` -> `PROCESSING` -> `COMPLETED`/`CANCELLED`).
  - **Prerequisites**: P6, P10.2.2, P11.2.
  - **Acceptance Criteria**:
    - Push-to-talk transcribes spoken English locally within target latency (<1.5s for 5s utterance on target hardware).
    - Local TTS synthesizes responses with <500ms time-to-first-audio chunk.
    - Wake-word requires explicit opt-in, consumes <3% CPU when idle, and can be disabled instantly.
    - Raw audio buffers scrubbed immediately from RAM after transcription; zero audio saved to disk.
    - Visual indicator illuminates whenever microphone capture is active.

- [ ] **P11.4 Conversational Voice-to-Action Integration**:
  - **Objective**: Connect local speech recognition to P11.1 task synthesis and PICO's existing capability execution engine.
  - **Scope**:
    - End-to-end voice intent execution: Spoken user commands transcribed locally -> routed through Context Compiler and Model Gateway -> synthesized into DAG workflow -> validated and authorized -> executed via existing capabilities (`os.launch_application`, `browser.open_page`, `filesystem.*`, `homeassistant.*`, `memory.*`).
    - Capability bounds & honest limitations: Actions strictly map to registered, allowlisted capabilities; unsupported commands are reported honestly with clear explanations rather than hallucinating success or fabricating mock actions.
    - Consequential action gating: Consequential operations halt at `awaiting_approval`; companion enters `awaiting_approval` state, explains the action, and requires explicit operator confirmation via Policy A.
    - Independent verification & voice confirmation: Outbound spoken response reports the verified postcondition outcome.
  - **Prerequisites**: P11.1, P11.2, P11.3, P8, P7.4.
  - **Acceptance Criteria**:
    - Voice commands trigger appropriate workflows and capabilities across supported domains.
    - Consequential actions halt for human approval; model cannot approve its own actions.
    - Spoken responses accurately reflect independent verification outcomes, never claiming unverified success.
    - Zero hardcoded demo bypasses or special-cased automations.

- [ ] **P11.5 Contextual Desktop Perception & Screen Understanding**:
  - **Objective**: Grant PICO contextual visual awareness of the user's desktop workspace with strict privacy and security boundaries.
  - **Scope**:
    - Explicit on-demand capture: Screen capture or active window snapshot initiated strictly by explicit user command ("PICO, what is this error?") or explicit toggle. Cameras/screen capture off by default.
    - Untrusted data boundary: Screen contents, OCR text, and visual elements carry conservative `INTERNAL` or `SENSITIVE` classification and are treated as untrusted input. Adversarial text in windows (e.g. prompt injection in browser pages) can NEVER grant permissions or elevate caller privileges.
    - Local visual reasoning: Multimodal understanding performed locally via quantized vision-language model (e.g. lightweight Moondream / Qwen2-VL) or structured OCR text extraction; zero automatic cloud forwarding of desktop screenshots.
    - Ephemeral frame lifecycle: Strict zero-retention baseline — captured bitmap frames reside strictly in transient RAM and are discarded immediately post-inference; zero screenshot history or frame export persisted to disk.
    - Contextual explanation: PICO answers questions about visible UI elements, error dialogs, terminal outputs, and application state.
  - **Prerequisites**: P6, P8.1, P11.1, P11.2.
  - **Acceptance Criteria**:
    - Screen capture occurs only with explicit user trigger; visible capture indicator displayed.
    - Captured frames discarded from RAM immediately after analysis; 0 frames saved to disk.
    - Prompt injections present on screen cannot alter system prompts, invoke unapproved tools, or bypass Policy A gates.
    - Unclassified or sensitive screen data is strictly barred from cloud inference (`RULE_PRIVATE_LOCAL_PIN`).

---

#### Thin Vertical-Slice Acceptance Demonstration:
An end-to-end acceptance target validating the entire companion stack:
1. PICO Desktop Companion launches as a lightweight, floating desktop widget with expressive eyes in `idle` state.
2. Companion visual state synchronizes with the local PICO backend over the Policy A loopback bridge.
3. User presses the companion push-to-talk button and speaks: *"PICO, search for the latest James Webb telescope video on YouTube."*
4. Local STT transcribes the speech-to-text on device with zero cloud audio transmission.
5. PICO processes the transcription through the Context Compiler and Model Gateway over resident model meeting benchmark validation gates (e.g., benchmark-validated `local-small` or approved profile).
6. PICO synthesizes a validated 1-step navigation workflow DAG:
   - Step 1: `browser.open_page` navigating to YouTube search query URL within approved origins (`https://www.youtube.com`). *(Note: Video selection and media playback require a future registered interaction capability with authoritative playback verification).*
7. Capability Registry validates schemas, permissions, and origin allowlists (`RiskTier.READ_ONLY`, `browser:open`).
8. Playwright browser adapter executes the navigation in a sandboxed, ephemeral browser context.
   *(Note: Online navigation requires active internet connectivity, while speech recognition, intent formulation, and workflow coordination execute fully offline).*
9. Independent verifier verifies HTTP response and DOM landing evidence.
10. Companion transitions to `speaking` state and delivers a concise spoken confirmation via local TTS: *"Opened YouTube search results for latest James Webb telescope videos."*

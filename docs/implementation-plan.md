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
- [ ] **P7.4 Stage C/D Everyday Appliances**: Authorized switches, plugs, and climate controls.
- **Status**: **STAGE A & STAGE B (P7.1, P7.2, P7.3 COMMISSIONING READINESS) COMPLETED & VERIFIED; P7.4 PLANNED**.


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
- **Status**: **P8.1, P8.2 & P8.3 COMPLETED & VERIFIED; P9 PLANNED**.

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
- [ ] **P10.2 Offline Resilience**: Graceful degradation when inference or network is offline.
- **Status**: **P10.1, P10.1.1 & P10.1.2 COMPLETED & VERIFIED**. P10.2 PLANNED.

# PICO Data Model Specification

## 1. Schema Design Philosophy

The PICO persistence architecture is designed around four foundational constraints:
1. **Local-First Reliability**: SQLite with WAL mode (`PRAGMA journal_mode = WAL`) and immediate foreign-key enforcement (`PRAGMA foreign_keys = ON`) as the primary local persistence engine.
2. **PostgreSQL Migration Parity**: Standard relational modeling using SQLAlchemy 2.0 and Alembic. Database-agnostic column types (e.g., standard UTC ISO8601 strings or DateTime, JSON payload columns, UUID/prefixed text primary keys) guarantee zero-friction migration to PostgreSQL.
3. **Auditability & Append-Only Foundations**: Mutable operational entities (Conversations, Tasks, Projects) are clearly separated from immutable append-only records (Event Ledger, Routing Decisions, Tool Executions).
4. **Data Classification & Provenance**: Core data models support explicit classification levels (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`) and relational provenance links.

---

## 2. Entity Relationship Overview

```mermaid
erDiagram
    PROJECT ||--o{ TASK : "contains"
    CONVERSATION ||--o{ MESSAGE : "contains"
    CONVERSATION ||--o{ TASK : "originates"

    %% Planned Entities
    TASK ||--o{ EXECUTION_JOB : "executes"
    EXECUTION_JOB ||--o{ TOOL_EXECUTION : "runs"
    EXECUTION_JOB ||--o{ APPROVAL_DECISION : "requires"
    CONVERSATION ||--o{ ROUTING_DECISION : "logs"
    MESSAGE ||--o{ MEMORY_RECORD : "synthesizes"
```

---

## 3. Active Implementation Entities (Phase 1, Slice 1)

### 3.1 `conversations`
Represents an ongoing interaction context or inquiry thread.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | VARCHAR(64) | PRIMARY KEY | Stable prefixed identifier (`conv_<uuid>` or UUID) |
| `title` | VARCHAR(255) | NOT NULL | Human-readable conversational title |
| `created_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `updated_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `metadata_json` | TEXT | NULLABLE | Extensible JSON object for tags, client info, state flags |

- **Indexes**: `idx_conversations_updated_at` (for fast descending list ordering).
- **Deletion Behavior**: CASCADE deletes child messages.

---

### 3.2 `messages`
Represents an immutable turn within a conversation (user input, assistant output, or system notice).

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | VARCHAR(64) | PRIMARY KEY | Stable prefixed identifier (`msg_<uuid>` or UUID) |
| `conversation_id` | VARCHAR(64) | NOT NULL, FK -> `conversations.id` ON DELETE CASCADE | Parent conversation reference |
| `role` | VARCHAR(32) | NOT NULL | One of: `user`, `assistant`, `system`, `tool` |
| `content` | TEXT | NOT NULL | Text payload of the message |
| `data_classification`| VARCHAR(32) | NOT NULL, DEFAULT `'INTERNAL'` | `PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET` |
| `token_count` | INTEGER | NULLABLE | Calculated or observed token length |
| `created_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `metadata_json` | TEXT | NULLABLE | Extensible JSON object (model profile, finish reason, tool calls) |

- **Indexes**: `idx_messages_conversation_created` (`conversation_id`, `created_at ASC`).
- **Invariants**: `conversation_id` must reference an existing `conversations` record; foreign keys strictly enforced. In Slice 1, persisting a user message does not fabricate an assistant message.

---

### 3.3 `projects`
Represents a user goal or persistent workspace grouping tasks, context, and documentation.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | VARCHAR(64) | PRIMARY KEY | Stable prefixed identifier (`proj_<uuid>` or UUID) |
| `name` | VARCHAR(255) | NOT NULL | Unique project name |
| `description` | TEXT | NULLABLE | Extended scope or objective description |
| `status` | VARCHAR(32) | NOT NULL, DEFAULT `'active'` | `active`, `archived`, `completed` |
| `created_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `updated_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `metadata_json` | TEXT | NULLABLE | Extensible JSON object |

- **Indexes**: `idx_projects_status`, `idx_projects_updated_at`.
- **Uniqueness**: `name` unique across active projects.
- **Deletion Behavior**: RESTRICT or SET NULL on associated tasks to prevent accidental orphan purges.

---

### 3.4 `tasks`
Represents an actionable unit of work, either linked to a project, originated from a conversation, or standalone.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | VARCHAR(64) | PRIMARY KEY | Stable prefixed identifier (`task_<uuid>` or UUID) |
| `project_id` | VARCHAR(64) | NULLABLE, FK -> `projects.id` ON DELETE SET NULL | Optional parent project |
| `conversation_id`| VARCHAR(64) | NULLABLE, FK -> `conversations.id` ON DELETE SET NULL | Optional originating conversation |
| `title` | VARCHAR(255) | NOT NULL | Descriptive task summary |
| `description` | TEXT | NULLABLE | Detailed instructions, constraints, or checklist |
| `status` | VARCHAR(32) | NOT NULL, DEFAULT `'pending'` | `pending`, `in_progress`, `completed`, `cancelled` |
| `priority` | VARCHAR(32) | NOT NULL, DEFAULT `'medium'` | `low`, `medium`, `high`, `urgent` |
| `due_date` | VARCHAR(32) / DATETIME | NULLABLE | Optional target completion UTC ISO 8601 |
| `completed_at`| VARCHAR(32) / DATETIME | NULLABLE | Timestamp when marked completed |
| `created_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `updated_at` | VARCHAR(32) / DATETIME | NOT NULL | UTC timestamp (ISO 8601) |
| `metadata_json` | TEXT | NULLABLE | Extensible JSON object |

- **Indexes**: `idx_tasks_project_id`, `idx_tasks_status`, `idx_tasks_updated_at`.
- **State Invariants**: When `status` changes to `'completed'`, `completed_at` is automatically stamped with the current UTC timestamp. If reverted to `'pending'` or `'in_progress'`, `completed_at` is cleared to `NULL`.

---

## 4. Planned Schema Evolution (Phase 2+)

The following entities are planned for subsequent phases and do NOT form part of Slice 1 physical tables.

### 4.1 `memory_records` (Phase 2)
- Stores synthesized working, episodic, semantic, and procedural memories.
- Schema: `id`, `memory_type` (`WORKING`, `EPISODIC`, `SEMANTIC`, `PROCEDURAL`), `content`, `embedding_vector` (BLOB/Array), `embedding_model`, `provenance_id` (source message or file), `data_classification`, `importance_score`, `decay_factor`, `access_count`, `last_accessed_at`, `created_at`.

### 4.2 `routing_decisions` (Phase 1, Slice 2)
- Inspectable audit log of every model selection decision.
- Schema: `id`, `conversation_id`, `step_index`, `task_category`, `data_classification`, `selected_profile`, `model_name`, `provider`, `decision_rule`, `cloud_approved` (BOOL), `fallback_occurred` (BOOL), `prompt_tokens`, `completion_tokens`, `latency_ms`, `estimated_cost_cents`, `created_at`.

### 4.3 `execution_jobs` & `job_dependencies` (Phase 2)
- Orchestrates multi-step DAG task plans.
- Schema: `id`, `task_id`, `status` (`QUEUED`, `RUNNING`, `WAITING_APPROVAL`, `COMPLETED`, `FAILED`), `plan_dag_json`, `current_step_index`, `created_at`, `updated_at`.

### 4.4 `tool_executions` (Phase 2)
- Audited record of tool invocations with idempotency keys.
- Schema: `id`, `job_id`, `tool_name`, `idempotency_key` (UNIQUE), `parameters_hash`, `parameters_json`, `output_payload`, `is_error` (BOOL), `duration_ms`, `executed_at`.

### 4.5 `approval_decisions` (Phase 2)
- Human-in-the-loop audit trail for consequential operations.
- Schema: `id`, `tool_execution_id`, `action_type`, `risk_level`, `status` (`PENDING`, `APPROVED`, `REJECTED`), `reason`, `requested_at`, `decided_at`, `decided_by`.

### 4.6 `event_ledger` (Phase 2)
- Append-only immutable log for system replay.
- Schema: `sequence_id` (BIGINT AUTOINCREMENT), `event_id` (UUID), `event_type`, `aggregate_type`, `aggregate_id`, `payload_json`, `prev_hash`, `event_hash`, `timestamp`.

### 4.7 `eval_cases` & `eval_runs` (Phase 1, Slice 3 & Phase 3)
- Version-controlled benchmark suites and performance scores.
- Schema: `id`, `suite_name`, `case_name`, `model_profile`, `prompt_tokens`, `latency_ms`, `ram_peak_mb`, `vram_peak_mb`, `score`, `pass_fail` (BOOL), `error_reason`, `run_timestamp`.

---

## 5. Memory v1 Subsystems (Implemented in Milestone P4)

### 5.1 `working_memory_items` (Working Memory)
- Short-lived, bounded session scratchpad and active task memory.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `wmem_`.
  - `session_id` (VARCHAR(64), INDEX): Conversation or session scope identifier.
  - `key` (VARCHAR(128), INDEX): Unique key within session.
  - `item_type` (VARCHAR(32)): Sub-category (`working_state`, `active_goal`, `scratchpad`).
  - `content` (TEXT): Bounded text or summary payload.
  - `task_id` (VARCHAR(64), FK `tasks.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `project_id` (VARCHAR(64), FK `projects.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `data_classification` (VARCHAR(32)): Classification label (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).
  - `source_provenance` (VARCHAR(255)): Source lineage tag (e.g. `session:working_memory`).
  - `is_active` (BOOLEAN): Soft-active flag.
  - `expires_at` (DATETIME, NULLABLE, INDEX): Timestamp-based TTL eviction.
  - `created_at` (DATETIME): UTC timestamp.
  - `updated_at` (DATETIME): UTC timestamp with auto-update.
  - `metadata_json` (JSON, NULLABLE): Extra structured context.

### 5.2 `episodic_memory_items` (Episodic Memory)
- Time-indexed record of events, task outcomes, decisions, and workflow errors.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `epm_`.
  - `event_type` (VARCHAR(64), INDEX): Event category (`task_completed`, `task_failed`, `decision_recorded`, `workflow_error`).
  - `description` (TEXT): Human-readable event description.
  - `occurred_at` (DATETIME, INDEX): When the event took place.
  - `task_id` (VARCHAR(64), FK `tasks.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `project_id` (VARCHAR(64), FK `projects.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `session_id` (VARCHAR(64), NULLABLE, INDEX).
  - `data_classification` (VARCHAR(32)): Classification label.
  - `source_provenance` (VARCHAR(255)): Provenance tag (e.g. `task:task_id`, `user:explicit_remember`).
  - `is_inferred` (BOOLEAN): Distinguishes direct observed facts from model inferences.
  - `deduplication_key` (VARCHAR(128), NULLABLE, INDEX): Idempotent deduplication key preventing duplicate event logging.
  - `created_at` (DATETIME): UTC creation timestamp.
  - `metadata_json` (JSON, NULLABLE).

### 5.3 `semantic_memories` & `semantic_memories_fts` (Semantic Memory)
- Source-grounded facts, preferences, and verified configurations.
- Schema (`semantic_memories`):
  - `id` (VARCHAR(64), PK): Stable typed prefix `sem_`.
  - `entity_type` (VARCHAR(64), INDEX): Entity type (`user_preference`, `system_fact`, `entity_relation`).
  - `subject` (VARCHAR(255), INDEX): Fact subject.
  - `predicate` (VARCHAR(128), INDEX): Fact predicate relationship.
  - `object` (VARCHAR(255), NULLABLE): Fact object value.
  - `content` (TEXT): Natural language factual statement.
  - `confidence` (FLOAT): 0.0 to 1.0 confidence score.
  - `is_verified` (BOOLEAN): Explicit user verification flag.
  - `conflict_status` (VARCHAR(32)): `clean`, `conflicted`, or `superseded`.
  - `data_classification` (VARCHAR(32)): Classification tag.
  - `source_provenance` (VARCHAR(255)): Source reference (e.g. `user:stated_preference`).
  - `project_id` (VARCHAR(64), FK `projects.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `task_id` (VARCHAR(64), FK `tasks.id`, ON DELETE SET NULL, NULLABLE, INDEX).
  - `created_at` (DATETIME): UTC timestamp.
  - `updated_at` (DATETIME): UTC timestamp.
  - `metadata_json` (JSON, NULLABLE).
- Virtual Table (`semantic_memories_fts`):
  - SQLite FTS5 table indexed over `subject`, `predicate`, `object`, and `content`.
  - Supports BM25-ranked full-text retrieval without requiring external vector databases.

### 5.4 `procedural_memories` (Procedural Memory)
- Reusable skills and automated workflows with mandatory human approval.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `prc_`.
  - `name` (VARCHAR(128), INDEX): Procedure identifier name.
  - `version` (VARCHAR(32)): Semantic version (e.g. `1.0.0`).
  - `description` (TEXT): Overview and purpose.
  - `lifecycle_status` (VARCHAR(32), INDEX): Lifecycle state machine: `draft -> reviewed -> approved -> active -> retired`.
  - `preconditions_json` (JSON, NULLABLE): List of preconditions.
  - `required_capabilities_json` (JSON, NULLABLE): Required capability permissions.
  - `steps_json` (JSON): Ordered operational steps.
  - `expected_effects_json` (JSON, NULLABLE): Expected environment effects.
  - `verification_criteria_json` (JSON, NULLABLE): Validation criteria.
  - `approved_by` (VARCHAR(128), NULLABLE): Human approver identity (mandatory for approval).
  - `approved_at` (DATETIME, NULLABLE): UTC approval timestamp.
  - `retired_at` (DATETIME, NULLABLE): UTC retirement timestamp.
  - `data_classification` (VARCHAR(32)): Classification tag.
  - `source_provenance` (VARCHAR(255)): Origin provenance.
  - `created_at` (DATETIME): UTC timestamp.
  - `updated_at` (DATETIME): UTC timestamp.
  - `metadata_json` (JSON, NULLABLE).

---

## 6. Tool Execution & Event Ledger Subsystems (Implemented in Milestone P5)

### 6.1 `tool_executions` (Tool Execution Engine)
- Persisted state machine tracking capability executions, deadlines, retries, approvals, and verification outcomes.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `exec_`.
  - `task_id` (VARCHAR(64), FK `tasks.id`, ON DELETE SET NULL, NULLABLE, INDEX): Associated task.
  - `capability_id` (VARCHAR(128), INDEX): Identifier of registered capability (e.g. `core.calculator`).
  - `capability_version` (VARCHAR(32)): Semantic version of executed capability.
  - `status` (VARCHAR(32), INDEX): State machine status: `queued`, `awaiting_approval`, `running`, `verifying`, `succeeded`, `failed`, `cancelled`, `timed_out`, `verification_failed`, `retry_scheduled`.
  - `idempotency_key` (VARCHAR(128), NULLABLE, UNIQUE, INDEX): Deduplication key preventing redundant execution.
  - `input_params` (JSON): Validated input parameter payload.
  - `input_digest` (VARCHAR(64), INDEX): Deterministic SHA-256 hash of canonicalized JSON input parameters.
  - `output_payload` (JSON, NULLABLE): Output payload returned from capability handler.
  - `error_message` (TEXT, NULLABLE): Execution or verification failure reason.
  - `duration_ms` (FLOAT, NULLABLE): Handler execution wall-clock time in milliseconds.
  - `retry_count` (INTEGER, DEFAULT 0): Number of retries attempted.
  - `max_retries` (INTEGER, DEFAULT 0): Maximum allowed retry attempts.
  - `timeout_seconds` (INTEGER, DEFAULT 30): Strict deadline in seconds.
  - `requires_approval` (BOOLEAN, DEFAULT FALSE): Flag indicating consequential capability requiring human approval.
  - `approved_by` (VARCHAR(128), NULLABLE): Verified human approver identity.
  - `approved_at` (DATETIME, NULLABLE): Timestamp when approval was granted.
  - `approval_notes` (TEXT, NULLABLE): Optional approval rationale.
  - `verification_passed` (BOOLEAN, NULLABLE): Result of independent deterministic verification.
  - `verification_notes` (TEXT, NULLABLE): Verifier details, output schema validation, or discrepancy notes.
  - `created_at` (DATETIME): UTC timestamp.
  - `started_at` (DATETIME, NULLABLE): UTC timestamp when handler began running.
  - `completed_at` (DATETIME, NULLABLE): UTC timestamp when execution concluded.
  - `metadata_json` (JSON, NULLABLE): Extensible metadata.

### 6.2 `event_ledger` (Append-Only Audit Ledger)
- Write-once, append-only historical audit log of all system transitions and execution lifecycles.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `evt_`.
  - `event_type` (VARCHAR(64), INDEX): Lifecycle transition type (e.g. `task_submitted`, `execution_started`, `verification_passed`).
  - `execution_id` (VARCHAR(64), FK `tool_executions.id`, ON DELETE SET NULL, NULLABLE, INDEX): Related execution record.
  - `capability_id` (VARCHAR(128), NULLABLE, INDEX): Associated capability ID.
  - `actor` (VARCHAR(128)): Initiating actor (e.g. `system`, `lead_engineer`, `caller`).
  - `payload_json` (JSON): Sanitized event payload with automatic credential/secret redaction.
  - `created_at` (DATETIME, INDEX): UTC timestamp.

---

## 7. Multimodal Perception Subsystem (Implemented in Milestone P6)

### 7.1 `perception_events` (Normalized Multimodal Perception Log)
- Bounded, scrubbed persistence for normalized multimodal events across all 4 perception domains: `audio`, `vision`, `motion`, `environmental`.
- Invariant: Never stores raw audio PCM, raw video, or unapproved full images. `raw_payload_scrubbed` is enforced true.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `pevt_<uuid>`.
  - `correlation_id` (VARCHAR(64), NULLABLE, INDEX): Trace correlation across multi-modal observation windows.
  - `session_id` (VARCHAR(64), NULLABLE, INDEX): Optional active audio/vision capture session reference.
  - `modality` (VARCHAR(32), NOT NULL, INDEX): One of `audio`, `vision`, `motion`, `environmental`.
  - `event_type` (VARCHAR(64), NOT NULL, INDEX): Domain event classification (e.g., `speech_transcribed`, `image_snapshot`, `pir_motion_detected`, `movement_interpreted`, `environmental_reading`).
  - `source_device_id` (VARCHAR(128), NOT NULL, INDEX): Originating physical or synthetic sensor/device identifier.
  - `data_classification` (VARCHAR(32), NOT NULL, DEFAULT `'INTERNAL'`): Security classification tag (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).
  - `confidence` (FLOAT, NOT NULL, DEFAULT 1.0): Calibrated confidence score bounded strictly between 0.0 and 1.0.
  - `raw_payload_scrubbed` (BOOLEAN, NOT NULL, DEFAULT TRUE): Cryptographic assertion that raw transient buffers have been discarded.
  - `structured_data` (JSON, NOT NULL): Extracted typed telemetry, transcription, or motion characteristics.
  - `provenance` (VARCHAR(255), NOT NULL): Relational source attribution URI.
  - `retention_policy` (VARCHAR(32), NOT NULL, DEFAULT `'standard'`): Lifecycle retention tier (`transient`, `standard`, `durable`).
  - `parent_event_id` (VARCHAR(64), FK `perception_events.id`, ON DELETE SET NULL, NULLABLE, INDEX): Parent event for fused/correlated observations.
  - `created_at` (DATETIME, NOT NULL, INDEX): Monotonic UTC timestamp.

---

## 8. Workflow Coordination Subsystem (Implemented in Milestone P9.1)

### 8.1 `workflow_runs` (Durable Coordinated Workflow Execution)
- Represents one durable execution of a multi-step dependency-directed acyclic graph (DAG).
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `wf_<uuid>`.
  - `task_id` (VARCHAR(64), FK `tasks.id`, ON DELETE SET NULL, NULLABLE, INDEX): Optional associated parent user task.
  - `session_id` (VARCHAR(64), NULLABLE, INDEX): Optional originating interaction session.
  - `correlation_id` (VARCHAR(64), NULLABLE, INDEX): Tracing identifier across workflow execution and ledger events.
  - `idempotency_key` (VARCHAR(128), UNIQUE, NULLABLE, INDEX): Client-provided token for deterministic deduplication.
  - `name` (VARCHAR(255), NOT NULL): Human-readable workflow plan title.
  - `description` (TEXT, NULLABLE): Extended workflow intent or purpose.
  - `status` (VARCHAR(32), NOT NULL, DEFAULT `'queued'`, INDEX): Lifecycle state (`queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`).
  - `data_classification` (VARCHAR(32), NOT NULL, DEFAULT `'INTERNAL'`): Security classification tag (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).
  - `total_steps` (INTEGER, NOT NULL, DEFAULT 0): Total number of subtask steps in plan.
  - `completed_steps` (INTEGER, NOT NULL, DEFAULT 0): Count of successfully completed steps.
  - `failed_steps` (INTEGER, NOT NULL, DEFAULT 0): Count of failed steps.
  - `error_message` (TEXT, NULLABLE): Top-level failure summary or blocker description.
  - `metadata_json` (JSON, NULLABLE): Extensible metadata.
  - `created_at` (DATETIME, NOT NULL): UTC creation timestamp.
  - `updated_at` (DATETIME, NOT NULL, INDEX): UTC last update timestamp.
  - `started_at` (DATETIME, NULLABLE): UTC execution start timestamp.
  - `completed_at` (DATETIME, NULLABLE): UTC termination timestamp.

### 8.2 `workflow_steps` (Delegated Units of Work)
- Individual subtask step within a workflow run, executing a registered capability with explicit dependencies.
- Table constraint: `UNIQUE(workflow_run_id, step_key)` ensures step identifier uniqueness per workflow.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `step_<uuid>`.
  - `workflow_run_id` (VARCHAR(64), FK `workflow_runs.id`, ON DELETE CASCADE, NOT NULL, INDEX): Parent workflow run.
  - `step_key` (VARCHAR(128), NOT NULL, INDEX): Plan-local unique step identifier.
  - `capability_id` (VARCHAR(128), NOT NULL, INDEX): Registered capability identifier.
  - `capability_version` (VARCHAR(32), NOT NULL, DEFAULT `'1.0.0'`): Capability version constraint.
  - `status` (VARCHAR(32), NOT NULL, DEFAULT `'pending'`, INDEX): Step lifecycle state (`pending`, `ready`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`, `blocked`, `verification_failed`).
  - `dependencies_json` (JSON, NOT NULL): Array of prerequisite `step_key` strings.
  - `input_template_json` (JSON, NOT NULL): Declared input template including static values and dependency references (`{"$from_step": "<step_key>", "$path": "<dot_path>"}`).
  - `resolved_input_json` (JSON, NULLABLE): Sanitized, resolved input parameters dispatched to the execution engine.
  - `tool_execution_id` (VARCHAR(64), FK `tool_executions.id`, ON DELETE SET NULL, NULLABLE, INDEX): Dispatched execution record.
  - `output_json` (JSON, NULLABLE): Output payload returned from successful execution.
  - `error_message` (TEXT, NULLABLE): Error details if step failed or was blocked.
  - `order_index` (INTEGER, NOT NULL, DEFAULT 0): Topological execution order index.
  - `created_at` (DATETIME, NOT NULL): UTC creation timestamp.
  - `updated_at` (DATETIME, NOT NULL, INDEX): UTC last update timestamp.
  - `started_at` (DATETIME, NULLABLE): UTC execution start timestamp.
  - `completed_at` (DATETIME, NULLABLE): UTC completion timestamp.

### 8.3 `workflow_verifications` (Independent Verification Decisions — Milestone P9.3)
- Durable record of independent output verification and evidence auditing before step results are released downstream.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `wfver_<uuid>`.
  - `workflow_run_id` (VARCHAR(64), FK `workflow_runs.id`, ON DELETE CASCADE, NOT NULL, INDEX): Associated workflow run.
  - `workflow_step_id` (VARCHAR(64), FK `workflow_steps.id`, ON DELETE CASCADE, NOT NULL, INDEX): Associated workflow step.
  - `tool_execution_id` (VARCHAR(64), FK `tool_executions.id`, ON DELETE SET NULL, NULLABLE, INDEX): Dispatched tool execution record evaluated.
  - `policy_id` (VARCHAR(128), NOT NULL, INDEX): Active verification policy identifier (e.g. `evidence.default.v1`, `evidence.model_assisted.v1`).
  - `policy_version` (VARCHAR(32), NOT NULL, DEFAULT `'1.0.0'`): Verification policy semver.
  - `method` (VARCHAR(64), NOT NULL): Verification method utilized (`deterministic_recomputation`, `schema_and_evidence`, `filesystem_handle_digest`, `device_state_poll`, `model_assisted_review`, `unsupported_fallback`).
  - `status` (VARCHAR(32), NOT NULL, INDEX): Terminal evaluation status (`passed`, `failed`, `inconclusive`, `requires_review`).
  - `stage` (VARCHAR(32), NOT NULL): Pipeline evaluation stage reached (`stage_a_structural`, `stage_b_evidence`, `stage_c_semantic`, `stage_d_decision`).
  - `input_digest` (VARCHAR(64), NOT NULL, INDEX): SHA256 digest of evaluated input data.
  - `output_digest` (VARCHAR(64), NOT NULL, INDEX): SHA256 digest of evaluated output data.
  - `passed` (BOOLEAN, NOT NULL, DEFAULT FALSE): Boolean assertion of successful verification.
  - `reason` (TEXT, NULLABLE): Human and machine-readable explanation of verification outcome.
  - `findings_json` (JSON, NULLABLE): Structured diagnostic findings from each pipeline stage.
  - `evidence_json` (JSON, NULLABLE): Cryptographic/observational evidence references.
  - `blockers_json` (JSON, NULLABLE): List of unresolved blockers or integrity violations.
  - `data_classification` (VARCHAR(32), NOT NULL, DEFAULT `'INTERNAL'`): Security classification tag.
  - `created_at` (DATETIME, NOT NULL, INDEX): UTC verification start timestamp.
  - `completed_at` (DATETIME, NULLABLE): UTC verification completion timestamp.

### 8.4 `workflow_compensations` (Controlled Reverse-Dependency Compensation — Milestone P9.3)
- Durable record of registered and executed compensation actions for reversible completed workflow steps.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `wfcomp_<uuid>`.
  - `workflow_run_id` (VARCHAR(64), FK `workflow_runs.id`, ON DELETE CASCADE, NOT NULL, INDEX): Associated workflow run.
  - `workflow_step_id` (VARCHAR(64), FK `workflow_steps.id`, ON DELETE CASCADE, NOT NULL, INDEX): Original step being compensated.
  - `original_tool_execution_id` (VARCHAR(64), FK `tool_executions.id`, ON DELETE SET NULL, NULLABLE, INDEX): Original execution record.
  - `compensation_capability_id` (VARCHAR(128), NOT NULL, INDEX): Compensating capability identifier (e.g. `filesystem.rollback_staged_diff`).
  - `compensation_capability_version` (VARCHAR(32), NOT NULL, DEFAULT `'1.0.0'`): Compensating capability semver.
  - `compensation_tool_execution_id` (VARCHAR(64), FK `tool_executions.id`, ON DELETE SET NULL, NULLABLE, INDEX): Dispatched compensating execution record.
  - `status` (VARCHAR(32), NOT NULL, DEFAULT `'registered'`, INDEX): Compensation lifecycle status (`registered`, `awaiting_approval`, `dispatched`, `completed`, `failed`).
  - `input_json` (JSON, NOT NULL): Immutable input parameters constructed for the compensating action.
  - `input_digest` (VARCHAR(64), NOT NULL, INDEX): SHA256 digest of compensation input parameters.
  - `order_index` (INTEGER, NOT NULL, DEFAULT 0): Step order index used for deterministic reverse dependency ordering.
  - `reason` (TEXT, NULLABLE): Operator or coordinator rationale for triggering compensation.
  - `created_at` (DATETIME, NOT NULL, INDEX): UTC registration timestamp.
  - `updated_at` (DATETIME, NOT NULL): UTC last update timestamp.
  - `completed_at` (DATETIME, NULLABLE): UTC execution completion timestamp.

---

## 9. Operator Session & Pairing Entities (Milestones P10.1 & P10.1.1)

Persists cross-process pairing challenges and authenticated operator sessions in the shared SQLite database.

### 9.1 `operator_pairing_challenges`
- Cross-process storage for one-time 6-digit pairing codes generated via `python -m pico.cli.pair` and verified by the FastAPI backend.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `pairch_<uuid>`.
  - `code_hash` (VARCHAR(64), NOT NULL): SHA-256 hash of `salt + code`. Raw 6-digit code is never stored in plaintext.
  - `salt` (VARCHAR(32), NOT NULL): Random hexadecimal salt generated per challenge.
  - `approver` (VARCHAR(128), NOT NULL, DEFAULT `'local_operator'`): Operator identity bound to challenge issuance.
  - `attempts` (INTEGER, NOT NULL, DEFAULT 0): Number of failed verification attempts (max 5).
  - `is_locked` (BOOLEAN, NOT NULL, DEFAULT FALSE): Rate-limit lockout flag set when failed attempts >= 5.
  - `consumed_at` (DATETIME, NULLABLE): UTC timestamp when successfully verified and consumed; prevents replay.
  - `expires_at` (DATETIME, NOT NULL, INDEX): UTC expiration timestamp (300 seconds from issuance).
  - `created_at` (DATETIME, NOT NULL, INDEX): UTC challenge generation timestamp.

### 9.2 `operator_sessions`
- Durable store of active and revoked local operator sessions created following successful pairing.
- Schema:
  - `id` (VARCHAR(64), PK): Stable typed prefix `opsess_<uuid>`.
  - `session_token_hash` (VARCHAR(64), NOT NULL, UNIQUE, INDEX): SHA-256 hash of the session secret transmitted in the `pico_operator_session` cookie.
  - `csrf_token` (VARCHAR(64), NOT NULL): Timing-safe CSRF validation token transmitted in request bodies / headers.
  - `approver` (VARCHAR(128), NOT NULL): Authenticated operator identity bound from the pairing challenge.
  - `is_revoked` (BOOLEAN, NOT NULL, DEFAULT FALSE, INDEX): Revocation flag set upon explicit logout or administrative revocation.
  - `expires_at` (DATETIME, NOT NULL, INDEX): UTC expiration timestamp (default 1 hour from issuance).
  - `created_at` (DATETIME, NOT NULL, INDEX): UTC session creation timestamp.
  - `last_used_at` (DATETIME, NOT NULL): UTC timestamp of most recent authenticated action.

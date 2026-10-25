# PICO REST API Contract

## 1. Conventions & Standards

- **Base URL**: `/api` (Health check at `/health`)
- **Protocol**: HTTP/1.1 (JSON request and response bodies)
- **Time Representation**: All timestamps are formatted as ISO 8601 UTC strings (`YYYY-MM-DDTHH:MM:SSZ` or with offset `+00:00`).
- **Standard Error Format**:
  All client and server errors return a standard JSON envelope:
  ```json
  {
    "error": {
      "code": "NOT_FOUND",
      "message": "Resource with id conv_abc123 does not exist.",
      "details": {}
    }
  }
  ```
  Validation errors return HTTP 422 with field-level breakdown.

### 1.1 Client Error Taxonomy & Resilience Contract (Milestone P10.2)

The desktop client implements a structured error model mapping transport, timeout, and HTTP envelopes:
- **`NetworkError`**: Transport-level failure (DNS resolution, connection refused, offline host). `is_offline = true`.
- **`TimeoutError`**: Bounded client-side request timeout (15 seconds default via `AbortController`).
- **`AuthenticationError`**: HTTP 401 Unauthorized or HTTP 403 Forbidden. Operator session expired, revoked, or missing.
- **`ProviderUnavailableError`**: HTTP 503 with error codes `PROVIDER_CONNECTION_ERROR`, `PROVIDER_UNAVAILABLE`, or `SCHEDULER_QUEUE_FULL`. Indicates backend is healthy but inference runtime is unavailable.
- **`ApiError`**: General HTTP errors (400, 404, 409, 422, 500) carrying parsed server error envelopes.

#### Recovery & Ambiguity Rules:
1. **Zero Blind Retries on Mutating Endpoints**: Requests to mutating endpoints (`POST`, `PUT`, `DELETE`) resulting in network timeout or transport interruption are classified as **"Outcome unknown"**. The client strictly never automatically replays these mutations.
2. **Single-Flight Health Probing**: Health probes against `/health` are single-flighted, preventing redundant concurrent probes. Backoff begins at 3 seconds, scales by 1.5x, and caps at 30 seconds with jitter.
3. **Session Revalidation**: When backend connectivity recovers, the client immediately probes `GET /api/operator/status` before re-enabling consequential approval controls.

---

## 2. Active Endpoints (Phase 1, Slice 1)

### 2.1 Health Check
- **`GET /health`**
  - **Description**: Returns operational health of the application, database connectivity, and SQLite WAL status.
  - **Response (200 OK)**:
    ```json
    {
      "status": "healthy",
      "version": "0.1.0",
      "database": {
        "connected": true,
        "journal_mode": "wal",
        "foreign_keys": true
      },
      "timestamp": "2026-10-18T00:15:00Z"
    }
    ```

---

### 2.2 Conversations

- **`POST /api/conversations`**
  - **Description**: Create a new conversation thread.
  - **Request Body**:
    ```json
    {
      "title": "Quarterly Financial Analysis",
      "metadata": {
        "client": "desktop-web"
      }
    }
    ```
  - **Validation**: `title` must be between 1 and 255 characters.
  - **Response (201 Created)**:
    ```json
    {
      "id": "conv_01HXYZ1234567890",
      "title": "Quarterly Financial Analysis",
      "created_at": "2026-10-18T00:15:00Z",
      "updated_at": "2026-10-18T00:15:00Z",
      "metadata": {
        "client": "desktop-web"
      }
    }
    ```

- **`GET /api/conversations`**
  - **Description**: List all conversations ordered by `updated_at` descending.
  - **Query Parameters**:
    - `limit` (int, default: 50, max: 100)
    - `offset` (int, default: 0)
  - **Response (200 OK)**:
    ```json
    {
      "items": [
        {
          "id": "conv_01HXYZ1234567890",
          "title": "Quarterly Financial Analysis",
          "created_at": "2026-10-18T00:15:00Z",
          "updated_at": "2026-10-18T00:15:00Z",
          "metadata": {}
        }
      ],
      "total": 1,
      "limit": 50,
      "offset": 0
    }
    ```

- **`GET /api/conversations/{conversation_id}`**
  - **Description**: Retrieve a single conversation by its ID.
  - **Response (200 OK)**: Conversation object.
  - **Response (404 Not Found)**: When `conversation_id` does not exist.

---

### 2.3 Messages

- **`POST /api/conversations/{conversation_id}/messages`**
  - **Description**: Append a message to an existing conversation.
  - **Invariants**: Foreign key to `conversation_id` must be valid. Persisting a user message **does not** fabricate an assistant response in Slice 1.
  - **Request Body**:
    ```json
    {
      "role": "user",
      "content": "Please review the budget projections for Q3.",
      "data_classification": "INTERNAL",
      "metadata": {}
    }
    ```
  - **Validation**:
    - `role` must be one of: `user`, `assistant`, `system`, `tool`.
    - `content` must not be empty.
    - `data_classification` must be one of: `PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET` (default: `INTERNAL`).
  - **Response (201 Created)**:
    ```json
    {
      "id": "msg_01HXYZ9876543210",
      "conversation_id": "conv_01HXYZ1234567890",
      "role": "user",
      "content": "Please review the budget projections for Q3.",
      "data_classification": "INTERNAL",
      "token_count": null,
      "created_at": "2026-10-18T00:15:10Z",
      "metadata": {}
    }
    ```
  - **Response (404 Not Found)**: If `conversation_id` does not exist.

- **`GET /api/conversations/{conversation_id}/messages`**
  - **Description**: Retrieve all messages in a conversation ordered chronologically (`created_at ASC`).
  - **Query Parameters**:
    - `limit` (int, default: 100, max: 500)
    - `offset` (int, default: 0)
  - **Response (200 OK)**:
    ```json
    {
      "items": [
        {
          "id": "msg_01HXYZ9876543210",
          "conversation_id": "conv_01HXYZ1234567890",
          "role": "user",
          "content": "Please review the budget projections for Q3.",
          "data_classification": "INTERNAL",
          "token_count": null,
          "created_at": "2026-10-18T00:15:10Z",
          "metadata": {}
        }
      ],
      "total": 1,
      "limit": 100,
      "offset": 0
    }
    ```
  - **Response (404 Not Found)**: If `conversation_id` does not exist.

---

### 2.4 Projects

- **`POST /api/projects`**
  - **Description**: Create a new project workspace.
  - **Request Body**:
    ```json
    {
      "name": "Local Intelligence Platform",
      "description": "Foundational personal AI architecture",
      "status": "active",
      "metadata": {}
    }
    ```
  - **Validation**: `name` must be between 1 and 255 characters and unique among active projects.
  - **Response (201 Created)**: Project object.

- **`GET /api/projects`**
  - **Description**: List all projects.
  - **Query Parameters**:
    - `status` (optional filter: `active`, `archived`, `completed`)
    - `limit` (int, default: 50)
    - `offset` (int, default: 0)
  - **Response (200 OK)**: Paginated list of projects.

- **`GET /api/projects/{project_id}`**
  - **Description**: Retrieve a single project.
  - **Response (200 OK)**: Project object.
  - **Response (404 Not Found)**: If `project_id` does not exist.

- **`PATCH /api/projects/{project_id}`**
  - **Description**: Update project attributes (name, description, status, metadata).
  - **Request Body**:
    ```json
    {
      "status": "completed"
    }
    ```
  - **Response (200 OK)**: Updated Project object.
  - **Response (404 Not Found)**: If `project_id` does not exist.

---

### 2.5 Tasks

- **`POST /api/tasks`**
  - **Description**: Create a new task.
  - **Request Body**:
    ```json
    {
      "title": "Implement SQLite WAL migrations",
      "description": "Configure Alembic and foreign keys",
      "project_id": "proj_01HXYZ...",
      "conversation_id": null,
      "priority": "high",
      "due_date": "2026-10-25T18:00:00Z",
      "metadata": {}
    }
    ```
  - **Validation**:
    - `title` required (1-255 characters).
    - `project_id` if provided must exist.
    - `conversation_id` if provided must exist.
    - `priority` must be one of: `low`, `medium`, `high`, `urgent`.
  - **Response (201 Created)**: Task object with `status: "pending"`.
  - **Response (404 Not Found)**: If linked `project_id` or `conversation_id` does not exist.

- **`GET /api/tasks`**
  - **Description**: List tasks with optional filtering.
  - **Query Parameters**:
    - `project_id` (optional string)
    - `conversation_id` (optional string)
    - `status` (optional filter: `pending`, `in_progress`, `completed`, `cancelled`)
    - `priority` (optional filter)
    - `limit` (int, default: 50)
    - `offset` (int, default: 0)
  - **Response (200 OK)**: Paginated list of tasks.

- **`GET /api/tasks/{task_id}`**
  - **Description**: Retrieve a single task by ID.
  - **Response (200 OK)**: Task object.
  - **Response (404 Not Found)**: If `task_id` does not exist.

- **`PATCH /api/tasks/{task_id}`**
  - **Description**: Update task attributes, including status transitions (task completion).
  - **Invariants**:
    - Setting `status: "completed"` automatically sets `completed_at` to current UTC timestamp.
    - Changing `status` from `completed` to `pending` or `in_progress` clears `completed_at` to `null`.
    - If `project_id` is updated, the new project must exist.
  - **Request Body**:
    ```json
    {
      "status": "completed"
    }
    ```
  - **Response (200 OK)**: Updated Task object.
  - **Response (404 Not Found)**: If `task_id` or referenced entity does not exist.

---

### 2.6 Chat Streaming (Phase 1, Slice 2)

- **`POST /api/chat/stream`**
  - **Description**: Stream conversational AI inference using Server-Sent Events (SSE) with per-step deterministic model routing.
  - **Workflow**:
    1. Validates that the conversation exists.
    2. Persists the user message exactly once.
    3. Resolves the model profile using the deterministic routing policy.
    4. Streams SSE chunks (`meta`, `token`, `done`, `error`).
    5. Persists the completed assistant response only after successful generation.
    6. Persists the routing decision audit record.
  - **Request Body**:
    ```json
    {
      "conversation_id": "conv_01HXYZ1234567890",
      "message": "Analyze local persistence architecture.",
      "data_classification": "INTERNAL",
      "task_category": "general_chat",
      "temperature": 0.7,
      "max_tokens": 1024,
      "requested_profile": null
    }
    ```
  - **Response (200 OK — text/event-stream)**:
    - **Meta Event**:
      ```
      event: meta
      data: {"decision_id": "route_123", "provider": "mock", "model": "pico-mock-deterministic", "data_classification": "INTERNAL", "selected_profile": "mock", "decision_rule": "RULE_PRIVATE_LOCAL_PIN"}
      ```
    - **Token Event(s)**:
      ```
      event: token
      data: {"delta": "Local "}
      ```
    - **Done Event**:
      ```
      event: done
      data: {"message_id": "msg_987", "finish_reason": "stop", "prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36, "latency_ms": 142.5}
      ```
    - **Error Event** (emitted on failure without fabricating assistant message):
      ```
      event: error
      data: {"code": "PROVIDER_CONNECTION_ERROR", "message": "Ollama service unavailable at http://127.0.0.1:11434"}
      ```
  - **Response (404 Not Found)**: If `conversation_id` does not exist.
  - **Response (422 Unprocessable Entity)**: Validation error (e.g. empty message, or security policy violation: attempting to explicitly request or route a cloud profile on a conversation containing non-`PUBLIC` context).

---

### 2.5 Memory Subsystems (Implemented in Milestone P4)

- **`GET /api/memory/status`**
  - **Description**: Return status metrics across all 4 memory subsystems and local SQLite FTS5 index.
  - **Response (200 OK)**:
    ```json
    {
      "working_memory_active_items": 3,
      "episodic_memory_total_events": 12,
      "semantic_memory_total_facts": 24,
      "semantic_memory_verified_facts": 18,
      "procedural_memory_total": 5,
      "procedural_memory_active": 2,
      "storage_type": "sqlite_fts5"
    }
    ```

- **`POST /api/memory/working`**
  - **Description**: Put or update an active, bounded working memory item.
  - **Request Body**:
    ```json
    {
      "session_id": "conv_123",
      "key": "active_goal",
      "content": "Refactor SQLite persistence schema",
      "item_type": "working_state",
      "data_classification": "INTERNAL",
      "ttl_seconds": 3600
    }
    ```
  - **Response (201 Created)**: `WorkingMemoryResponse`

- **`GET /api/memory/working/{session_id}`**
  - **Description**: List active, non-expired working memory items for a session.
  - **Response (200 OK)**: List of `WorkingMemoryResponse`

- **`DELETE /api/memory/working/{session_id}`**
  - **Description**: Clear all active working memory entries for a session.
  - **Response (200 OK)**: `{"cleared_count": 3}`

- **`POST /api/memory/events`**
  - **Description**: Record an episodic event with deduplication key support.
  - **Request Body**:
    ```json
    {
      "event_type": "task_completed",
      "description": "Benchmark runs completed successfully",
      "source_provenance": "task:task_01",
      "data_classification": "INTERNAL",
      "is_inferred": false,
      "deduplication_key": "bench:qwen3.5:2b:run_1"
    }
    ```
  - **Response (201 Created)**: `EpisodicMemoryResponse`

- **`GET /api/memory/events`**
  - **Description**: List and filter episodic memory events by type, task, date, or inference flag.
  - **Response (200 OK)**: `EpisodicMemoryListResponse`

- **`POST /api/memory/facts`**
  - **Description**: Save or explicitly remember a factual preference/knowledge item.
  - **Request Body**:
    ```json
    {
      "subject": "User",
      "predicate": "preferred_editor",
      "content": "User prefers Neovim for Python coding.",
      "is_verified": true,
      "data_classification": "INTERNAL",
      "source_provenance": "user:explicit"
    }
    ```
  - **Response (201 Created)**: `SemanticMemoryResponse`

- **`GET /api/memory/facts/search`**
  - **Description**: Full-text search across semantic memories using SQLite FTS5 and BM25 ranking.
  - **Query Parameters**: `q` (string), `limit` (int), `entity_type` (optional), `project_id` (optional).
  - **Response (200 OK)**: List of `SemanticMemoryResponse`

- **`DELETE /api/memory/facts/{fact_id}`**
  - **Description**: User-directed forget operation: completely purges the fact from table and FTS index.
  - **Response (200 OK)**: `{"status": "forgotten", "id": "sem_123"}`

- **`POST /api/memory/procedures`**
  - **Description**: Create a procedural memory skill draft. Always initializes in `draft` status.
  - **Response (201 Created)**: `ProceduralMemoryResponse`

- **`POST /api/memory/procedures/{id}/review`**
  - **Description**: Transition procedure from `draft -> reviewed`.
  - **Response (200 OK)**: `ProceduralMemoryResponse`

- **`POST /api/memory/procedures/{id}/approve`**
  - **Description**: Explicit human approval (`reviewed -> approved`). Requires `approver` string.
  - **Response (200 OK)**: `ProceduralMemoryResponse`

- **`POST /api/memory/procedures/{id}/activate`**
  - **Description**: Activate approved procedure (`approved -> active`). Fails if not approved.
  - **Response (200 OK)**: `ProceduralMemoryResponse`

- **`POST /api/memory/procedures/{id}/retire`**
  - **Description**: Retire procedure (`* -> retired`).
  - **Response (200 OK)**: `ProceduralMemoryResponse`

---

### 2.7 Tool Execution Engine (Milestone P5)

- **`GET /api/tools/capabilities`**
  - **Description**: List all explicitly registered system capabilities with schemas, risk tiers, and permission scopes.
  - **Response (200 OK)**: List of `CapabilityResponse` objects.

- **`GET /api/tools/capabilities/{capability_id}`**
  - **Description**: Retrieve detailed capability definition and parameter requirements.
  - **Response (200 OK)**: `CapabilityResponse`
  - **Response (404 Not Found)**: Unknown capability ID.

- **`POST /api/tools/executions`**
  - **Description**: Submit a tool execution request with schema validation and idempotency checking. If the capability is consequential, execution enters `awaiting_approval`.
  - **Request Body**: `ToolExecutionCreate` (`capability_id`, `input_params`, optional `idempotency_key`, `task_id`, `timeout_seconds`).
  - **Response (201 Created)**: `ToolExecutionResponse`

- **`GET /api/tools/executions`**
  - **Description**: List historical tool executions with optional status, task, or capability filters.
  - **Query Parameters**: `status`, `capability_id`, `task_id`, `limit`, `offset`.
  - **Response (200 OK)**: `ToolExecutionListResponse`

- **`GET /api/tools/executions/{execution_id}`**
  - **Description**: Retrieve current execution status, timings, verification results, and failure details.
  - **Response (200 OK)**: `ToolExecutionResponse`

- **`POST /api/tools/executions/{execution_id}/execute`**
  - **Description**: Trigger execution of a queued or approved execution ticket. Runs handler, applies timeout bounds, and runs independent verifier.
  - **Response (200 OK)**: `ToolExecutionResponse`

- **`POST /api/tools/executions/{execution_id}/approve`**
  - **Description**: Explicit human approval for consequential actions. Validates trusted approver identity and secret token (`X-Pico-Approval-Token`).
  - **Request Body**: `ToolApprovalRequest` (`approver`, `notes`).
  - **Response (200 OK)**: `ToolExecutionResponse`
  - **Response (403 Forbidden)**: Untrusted approver or missing/invalid token.

- **`POST /api/tools/executions/{execution_id}/cancel`**
  - **Description**: Cooperative cancellation of queued or awaiting-approval execution.
  - **Response (200 OK)**: `ToolExecutionResponse`

- **`GET /api/tools/executions/{execution_id}/events`**
  - **Description**: Retrieve append-only audit event trace for a specific execution.
  - **Response (200 OK)**: `LedgerEventListResponse`

- **`POST /api/tools/procedures/{procedure_id}/validate`**
  - **Description**: Validate procedural memory steps against the Capability Registry. Rejects procedures with unknown, unauthorized, or retired capability actions.
  - **Response (200 OK)**: `ProcedureValidationResponse`

---

### 2.8 Multimodal Perception API (Milestone P6)

- **`GET /api/perception/status`**
  - **Description**: Returns operational readiness, active capture states, and local runtime availability across all 4 perception domains (`audio`, `vision`, `motion`, `environmental`).
  - **Response (200 OK)**: `PerceptionSubsystemStatus`

- **`POST /api/perception/audio/session/start`**
  - **Description**: Explicit push-to-talk microphone capture session start. Enforces mandatory user consent (`user_consent=True`). Microphone is NEVER activated automatically.
  - **Request Body**: `AudioSessionStartRequest` (`user_consent`, optional `source_device`, `metadata`).
  - **Response (201 Created)**: `AudioSessionStartResponse`

- **`POST /api/perception/audio/session/{session_id}/stop`**
  - **Description**: Terminates active push-to-talk recording, executes local provider-neutral speech-to-text transcription, emits a normalized perception event, and scrubs raw PCM audio buffers.
  - **Request Body**: `AudioSessionStopRequest` (optional `audio_base64`, `audio_format`, `client_duration_seconds`).
  - **Response (200 OK)**: `AudioSessionStopResponse`

- **`POST /api/perception/audio/session/{session_id}/cancel`**
  - **Description**: Cooperatively aborts an active audio recording session, immediately releasing all transient audio buffers.
  - **Response (200 OK)**: `AudioSessionStartResponse`

- **`POST /api/perception/audio/synthesize`**
  - **Description**: Local text-to-speech synthesis through provider-neutral TTS adapter.
  - **Request Body**: `AudioSynthesisRequest` (`text`, optional `voice_id`, `speed`).
  - **Response (200 OK)**: `AudioSynthesisResponse`

- **`POST /api/perception/vision/capture`**
  - **Description**: On-demand visual frame ingestion (user upload, explicit screenshot, or consensual camera snapshot). Validates payload size (max 4 MB) and dimensions (max 4096x4096). Discards raw pixel frames after feature/scene extraction.
  - **Request Body**: `VisionCaptureRequest` (`source_type`, `image_base64`, `image_format`, `user_consent`, optional `source_device`, `metadata`).
  - **Response (200 OK)**: `VisionCaptureResponse`

- **`POST /api/perception/motion/pir`**
  - **Description**: Ingest binary PIR sensor trigger (Tier 1 motion detection). Restricts inference to motion presence/clearance; strictly refuses to infer direction or identity without visual corroboration.
  - **Request Body**: `MotionIngestRequest` (`source_device`, optional `motion_detected`, `confidence`, `data_classification`).
  - **Response (201 Created)**: `NormalizedPerceptionEvent`

- **`POST /api/perception/motion/interpret`**
  - **Description**: Tier 2 movement interpretation using deterministic CPU frame-differencing between two short-lived frames. Computes delta pixel ratio, centroid displacement, direction (`stationary`, `approaching`, `retreating`, `left`, `right`, `up`, `down`), and magnitude.
  - **Request Body**: `MovementInterpretationRequest` (`frame_a_base64`, `frame_b_base64`, optional `source_device`, `sensitivity_threshold`).
  - **Response (200 OK)**: `MovementInterpretationResponse`

- **`POST /api/perception/motion/correlate`**
  - **Description**: Tier 3 cross-modal interpretation linking related perception events (e.g., PIR motion + door contact) across a configurable temporal window. Preserves underlying event provenance and uncertainty.
  - **Request Body**: `CrossModalCorrelationRequest` (`target_event_id`, optional `time_window_seconds`).
  - **Response (200 OK)**: `CrossModalCorrelationResponse`

- **`POST /api/perception/environmental/ingest`**
  - **Description**: Read-only environmental telemetry ingestion (temperature, humidity, ambient lux, contact state, AQI, battery). Validates bounds, enforces per-sensor deduplication cooldown, and asserts read-only constraints (no physical actuator triggers).
  - **Request Body**: `EnvironmentalIngestRequest`.
  - **Response (200 OK)**: `EnvironmentalIngestResponse`

- **`GET /api/perception/events`**
  - **Description**: Query recent normalized perception events with bounded limit and optional modality filter.
  - **Query Parameters**: `limit` (1-100, default 20), `modality` (optional).
---

### 2.8 Home Assistant Read-Only Awareness (Stage A)

**Security Exposure Boundary**:
- **Loopback Access**: Requests originating from local loopback (`127.0.0.1`, `::1`, `localhost`) are permitted locally.
- **Remote Access (Fail-Closed)**: Non-loopback remote callers MUST supply an `X-Pico-Approval-Token` header matching the server's `APPROVAL_SECRET_TOKEN`. If unconfigured, missing, or mismatched, requests are rejected with `403 Forbidden`.
- **Read-Only Invariant**: All endpoints and transport channels are strictly read-only. No service execution, device toggling, or mutation capabilities are exposed.
- **Verification Note**: Current verification uses deterministic mocked transports and RFC 6455 protocol tests. Physical device connectivity is explicitly not claimed (`physical_device_connectivity_claimed=false`).

- **`GET /api/homeassistant/status`**
  - **Description**: Inspect Home Assistant hub integration readiness and connectivity state (`disabled`, `missing_configuration`, `connection_unavailable`, `authentication_rejected`, `connected`, `read_failed`). Never claims physical device connectivity.
  - **Response (200 OK)**: `HomeAssistantStatus`

- **`GET /api/homeassistant/entities`**
  - **Description**: Discover available smart-home entities and their reported states without mutation. Attributes are strictly sanitized against an explicit allowlist (unknown attributes, nested payloads, non-finite numbers, credentials, and personal data dropped). Bounded by byte size (2 MB) before parsing and capped at `HOME_ASSISTANT_MAX_ENTITIES_LIMIT`.
  - **Query Parameters**: `domain` (optional string, e.g. `light`, `sensor`, `lock`).
  - **Response (200 OK)**: `EntityListOutput` (`total_count`, `domain_filter`, `entities`, `data_classification`).

- **`GET /api/homeassistant/entities/{entity_id}`**
  - **Description**: Read reported state and bounded attributes of a specific smart-home entity. Bounded by byte size (256 KB) before parsing.
  - **Validation**: `entity_id` must match `<domain>.<object_id>`.
  - **Response (200 OK)**: `EntityState`
  - **Response (404 Not Found)**: If entity does not exist.

- **`GET /api/homeassistant/overview`**
  - **Description**: Bounded summary overview of smart-home entities suitable for assistant situational awareness.
  - **Query Parameters**: `domains` (optional list of domains to highlight).
  - **Classification Invariant**: Effective data classification is computed across all normalized entities contributing to the summary, ensuring sensitive entities (locks, alarms, cameras) elevate the response classification to `SENSITIVE` even when truncated highlights contain only lights.
  - **Response (200 OK)**: `EntityOverviewOutput` (`total_entities`, `domain_counts`, `highlights`, `data_classification`).

---

### 2.9 Home Assistant Physical Actuation (Milestone P7.3)

**Operational Invariants & Safety Constraints**:
- **Strictly Single Test Light**: Physical actuation is strictly constrained to the single entity specified by `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID` (domain must be `light`). Arbitrary entities, caller-provided target overrides, and all other domains (locks, alarms, covers, garage doors, cameras, thermostats, scripts, scenes) are strictly prohibited.
- **Explicit Actuation Opt-In**: Actuation remains unavailable unless `HOME_ASSISTANT_ACTUATION_ENABLED=true`, `HOME_ASSISTANT_ENABLED=true`, access token is configured, and test light entity ID is valid.
- **Fail-Closed Authorization Boundary**: Physical actuation requests must fail closed when `APPROVAL_SECRET_TOKEN` is unset, missing, invalid, or mismatched. This applies to loopback callers as well as remote requests; local requests are not silently bypassed.
- **Mandatory Single-Use Approval Bound to Triple-Digest**: Every state mutation requires explicit human approval bound to the SHA-256 canonical digest. Immediately before dispatch, PICO requires agreement across (1) recomputed digest from persisted input, (2) stored input digest, and (3) approved execution digest. Approval tickets are single-use and cannot be replayed or re-executed.
- **Read-After-Write Independent Verification**: Service calls are acknowledged via authenticated REST POST (`/api/services/light/turn_on` or `turn_off`). Mutation acknowledgment alone is never treated as success. Bounded read-only polling (`client.get_state`) must verify reported state matches target state.
- **No Automatic Retries**: Timeouts, network errors, or verification failures never trigger repeated mutation attempts automatically. Uncertainty is reported truthfully.
- **Mock vs. Physical Device Separation**: Current automated tests execute against deterministic synthetic transports. Software verification does not claim physical device connectivity (`physical_device_connectivity_claimed=false`).

- **`POST /api/homeassistant/actuation/plan`**
  - **Description**: Inspects current reported state of the configured test light and creates a structured action plan without causing any physical action (strictly read-only).
  - **Request Body**: `TestLightPlanRequest` (`target_state: Literal["on", "off"]`, optional `target_entity_id`).
  - **Response (200 OK)**: `TestLightActuationPlan` (`plan_id`, `entity_id`, `requested_state`, `current_state`, `service`, `is_mutation_needed`, `preconditions`, `expected_postconditions`, `input_digest`, `expires_at`, `data_classification`).

- **`POST /api/homeassistant/actuation/propose`**
  - **Description**: Proposes an actuation execution for the configured test light. Creates a `ToolExecution` in `awaiting_approval` status bound to SHA-256 digest. Fails closed without valid `X-Pico-Approval-Token`. Never executes mutation automatically.
  - **Request Body**: `TestLightProposeRequest` (`target_state: Literal["on", "off"]`, optional `target_entity_id`, optional `reason`).
  - **Headers**: `X-Pico-Approval-Token` (mandatory).
  - **Response (201 Created)**: `ToolExecutionResponse` (status: `awaiting_approval`).

- **`GET /api/homeassistant/actuation/{execution_id}`**
  - **Description**: Inspect approval state, execution status, and independent verification outcome of an actuation execution.
  - **Response (200 OK)**: `ToolExecutionResponse`.

- **`GET /api/homeassistant/actuation/readiness`**
  - **Description**: Evaluates commissioning and physical action readiness for the single configured test light without executing mutations. Provides structured, actionable check items (`PASS`, `FAIL`, `NOT_CONFIGURED`, `NOT_CHECKED`). Live read-only connectivity inspection is performed strictly upon explicit operator request (`check_live=true`).
  - **Query Parameters**: `check_live: bool` (default: `false`).
  - **Security Invariant**: Never calls Home Assistant service endpoints, mutates device state, automatically enables actuation, exposes secrets, or claims physical connectivity unless explicitly observed via read-only check.
  - **Response (200 OK)**: `ActuationReadinessReport` (`ready_for_commissioning`, `target_entity_id`, `live_check_performed`, `live_passed`, `reported_state`, `actuation_enabled`, `checks`, `summary`).


### 2.10 OS & Desktop Application Automation Capabilities (Milestone P8.1)

OS automation capabilities are integrated into the core Tool Execution Engine (`/api/tools/*`) under dedicated authorization and approval controls:

**Security Exposure & Authorization Invariants**:
- **Dedicated Permissions**: `os:inspect` (for window enumeration) and `os:launch` (for application launching) are strictly decoupled from standard development permissions.
- **Fail-Closed Authorization Boundary**: Submissions and approvals fail closed with `403 Forbidden` unless authorized with a valid `X-Pico-Approval-Token` matching `APPROVAL_SECRET_TOKEN`.
- **Zero Real Desktop Invocations in CI**: Automated test runs use deterministic synthetic adapters (`FakeOSAutomationAdapter`).
- **Empty Allowlist by Default**: `OS_APPLICATION_ALLOWLIST` defaults to `{}`. Only explicitly configured aliases can be launched.
- **No Arbitrary Arguments or Shells**: `LaunchApplicationInput` forbids extra properties (`extra="forbid"`). Arbitrary paths, caller-supplied arguments, working directory overrides, and shell scripts are rejected.
- **Child Environment Sanitization**: Child processes inherit only essential OS variables (`SystemRoot`, `PATH`, `TEMP`); all PICO tokens, secrets, HA credentials, and cloud keys are wiped.

#### Capability: `os.inspect_windows`
- **Capability ID**: `os.inspect_windows` (version: `1.0.0`)
- **Risk Tier**: `READ_ONLY`
- **Required Permission**: `os:inspect`
- **Data Classification**: `INTERNAL`
- **Consequential**: `false` | **Idempotent**: `true` | **Timeout**: 5.0s
- **Input Schema**:
  ```json
  {
    "max_windows": 50,
    "max_title_length": 256
  }
  ```
- **Output Schema**:
  ```json
  {
    "status": "success",
    "platform": "windows",
    "windows": [
      {
        "handle": 65540,
        "title": "Document - Text Editor",
        "process_id": 4120,
        "is_visible": true,
        "is_minimized": false,
        "is_foreground": true
      }
    ],
    "total_windows_found": 1,
    "truncated": false,
    "message": null
  }
  ```

#### Capability: `os.launch_application`
- **Capability ID**: `os.launch_application` (version: `1.0.0`)
- **Risk Tier**: `HIGH`
- **Required Permission**: `os:launch`
- **Data Classification**: `INTERNAL`
- **Consequential**: `true` (Requires explicit human approval) | **Idempotent**: `false` | **Max Retries**: 0 | **Timeout**: 10.0s
- **Input Schema**:
  ```json
  {
    "alias": "text_editor",
    "launch_fingerprint": null
  }
  ```
  *Note*: `launch_fingerprint` is an optional server-computed SHA-256 fingerprint. The server automatically calculates and binds the fingerprint of the allowlisted profile (`alias`, `executable_path`, ordered `args`, `working_dir`) during submission. Callers cannot supply an arbitrary or mismatched fingerprint. If the server-side allowlist changes after approval, execution fails closed before dispatch.
- **Output Schema**:
  ```json
  {
    "alias": "text_editor",
    "status": "process_creation_confirmed",
    "executable_path": "C:\\Program Files\\Editor\\editor.exe",
    "process_id": 9840,
    "verification_status": "process_creation_confirmed",
    "verification_details": {
      "pid": 9840,
      "window_observed": false,
      "note": "Process creation confirmed; no top-level window reliably associated within observation window."
    },
    "message": null
  }
  ```
- **Distinct Launch Verification Statuses**:
  - `request_rejected`: Alias not present in allowlist or profile failed validation.
  - `awaiting_approval`: Initial submission waiting for human approval ticket.
  - `launch_dispatched`: Process creation initiated.
  - `process_creation_confirmed`: Process creation confirmed via PID; window association not confirmed.
  - `independent_window_observation_confirmed`: Process creation and top-level visible window independently observed.
  - `launch_uncertain`: Process creation or observation encountered an uncertain outcome (zero retries).
  - `launch_failed`: Process creation failed (e.g. executable missing, permission denied).

---

### 2.11 Filesystem Operations & Staged Diffs (Milestone P8.2)

**Operational Invariants & Path Safety Boundaries**:
- **Windows Handle Boundary & Directory Hierarchy Pinning**: During reads, mutations, deletions, and rollback, PICO pins every directory from the root down to the target parent via `pin_directory_hierarchy` using `CreateFileW` with `FILE_LIST_DIRECTORY`, `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, and `FILE_SHARE_READ | FILE_SHARE_WRITE` (omitting `FILE_SHARE_DELETE`). The Windows NT kernel rejects external process attempts to delete, rename, or swap parent directories with junctions (`WinError 32: ERROR_SHARING_VIOLATION`).
- **Handle-Based Operations & Rollback Deletion**: Reads and exclusive creations (`CREATE_NEW`) operate via verified file handles (`FILE_FLAG_OPEN_REPARSE_POINT`). Rollback deletions open target files with `DELETE | FILE_READ_DATA | FILE_FLAG_OPEN_REPARSE_POINT`, verify file identity and expected digest on the handle, and issue deletion via `SetFileInformationByHandle(FileDispositionInfo, True)`. Deletions are bound directly to the verified filesystem object, preventing redirected deletions outside the allowlisted root.
- **Fail-Closed Reparse Inspection**: `is_symlink_or_reparse_point` distinguishes `FileNotFoundError` (genuinely absent file during creation) from `PermissionError` and other unexpected `OSError`/`ValueError` states, which fail closed (`ValidationError`).
- **Honest Replacement Semantics**: On Windows NTFS, file replacement via `os.replace` / `MoveFileExW` is not an atomic kernel compare-and-swap on file content. Replacement is guarded by parent directory hierarchy pinning, directory-local temporary staging (`CREATE_NEW`), pre-replace digest check, and mandatory post-write verification. If concurrent modification occurs on the target file between verification and replacement, post-write verification detects the digest mismatch and fails closed (`ExecutionError`) with zero automated retries.
- **Fail-Closed Write Permissions**: Write operations (`filesystem:write`) strictly require a non-empty server-side `APPROVAL_SECRET_TOKEN` and constant-time match (`secrets.compare_digest`). Unset secret, missing token, or mismatched token fails closed with zero mutations (`403 Forbidden`).
- **Safe Temporary Cleanup**: `safe_unlink` verifies handles before unlinking, ensuring temporary staging files (`.pico_tmp_*`) are never unlinked if redirected outside the authorized root.

#### REST Endpoints: `/api/filesystem/proposals`

##### `POST /api/filesystem/proposals`
- **Description**: Stage a new text file diff proposal (`create` or `replace`). Reads the existing file (if any), computes original digest, normalizes new content, bounds text/diff sizes, generates unified diff, and records a staged proposal with `staged` status.
- **Request Body**:
  ```json
  {
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "proposed_content": "Initial notes content\n"
  }
  ```
- **Validation**:
  - `root_id`: Must match an active configured server root in `PICO_FILESYSTEM_ALLOWED_ROOTS`.
  - `relative_path`: Must be a safe relative path within the root. Rejects traversal (`..`), device paths (`CON`, `NUL`), alternate separators, and reparse points.
  - `operation`: Must be `"create"` or `"replace"`.
  - `proposed_content`: UTF-8 text only; rejects NUL bytes (`\x00`); max size bounded by `PICO_FILESYSTEM_MAX_FILE_BYTES`.
- **Response (201 Created)**:
  ```json
  {
    "proposal_id": "prop_01abc123",
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "expected_original_digest": "ABSENT",
    "proposed_content_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "unified_diff": "--- /dev/null\n+++ docs/notes.txt\n@@ -0,0 +1 @@\n+Initial notes content\n",
    "diff_digest": "7852b855e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b",
    "status": "staged",
    "created_at": "2026-10-21T21:00:00Z",
    "applied_at": null,
    "rolled_back_at": null,
    "verification_details": null
  }
  ```

##### `GET /api/filesystem/proposals`
- **Description**: List staged diff proposals with optional query parameters (`root_id`, `status`).
- **Response (200 OK)**: JSON array of `StagedDiffResponse` objects.

##### `GET /api/filesystem/proposals/{proposal_id}`
- **Description**: Retrieve a specific staged proposal and its inspectable unified diff by ID.
- **Response (200 OK)**: Single `StagedDiffResponse` object.

##### `POST /api/filesystem/proposals/{proposal_id}/discard`
- **Description**: Discard an unapplied staged diff proposal. Transitions status to `discarded`.
- **Response (200 OK)**: Single `StagedDiffResponse` object with `status: "discarded"`.

##### `POST /api/filesystem/proposals/{proposal_id}/apply`
- **Description**: Apply an approved staged diff proposal. Submits or executes `filesystem.apply_staged_diff` via the ToolExecutionEngine. Requires human approval ticket when consequential execution is enforced.
- **Headers**: `X-Pico-Approval-Token` matching a non-empty server-side `APPROVAL_SECRET_TOKEN` (verified via constant-time `secrets.compare_digest`). Unset secret, missing token, or mismatched token fails closed with `403 Forbidden` (`filesystem:write` permission denied).
- **Response (200 OK / 202 Accepted)**: ToolExecutionEngine result envelope with execution and verification details.

##### `POST /api/filesystem/proposals/{proposal_id}/rollback`
- **Description**: Roll back an applied staged diff proposal. Submits or executes `filesystem.rollback_staged_diff` via the ToolExecutionEngine. Requires human approval ticket. Fails closed if the destination file has been modified since application or replaced with a symlink/reparse point.
- **Headers**: `X-Pico-Approval-Token` matching a non-empty server-side `APPROVAL_SECRET_TOKEN` (verified via constant-time `secrets.compare_digest`). Unset secret, missing token, or mismatched token fails closed with `403 Forbidden` (`filesystem:write` permission denied).
- **Response (200 OK / 202 Accepted)**: ToolExecutionEngine result envelope with execution and verification details.

---

#### Capability: `filesystem.stage_diff`
- **Capability ID**: `filesystem.stage_diff` (version: `1.0.0`)
- **Risk Tier**: `READ_ONLY`
- **Required Permission**: `filesystem:read`
- **Data Classification**: `INTERNAL`
- **Consequential**: `false` | **Idempotent**: `true` | **Timeout**: 10.0s
- **Input Schema**:
  ```json
  {
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "proposed_content": "Initial notes content\n"
  }
  ```

#### Capability: `filesystem.inspect_diff`
- **Capability ID**: `filesystem.inspect_diff` (version: `1.0.0`)
- **Risk Tier**: `READ_ONLY`
- **Required Permission**: `filesystem:read`
- **Data Classification**: `INTERNAL`
- **Consequential**: `false` | **Idempotent**: `true` | **Timeout**: 5.0s
- **Input Schema**:
  ```json
  {
    "proposal_id": "prop_01abc123"
  }
  ```

#### Capability: `filesystem.apply_staged_diff`
- **Capability ID**: `filesystem.apply_staged_diff` (version: `1.0.0`)
- **Risk Tier**: `HIGH`
- **Required Permission**: `filesystem:write`
- **Data Classification**: `INTERNAL`
- **Consequential**: `true` (Requires explicit human approval) | **Idempotent**: `false` | **Max Retries**: 0 | **Timeout**: 15.0s
- **Input Schema**:
  ```json
  {
    "proposal_id": "prop_01abc123",
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "expected_original_digest": "ABSENT",
    "proposed_content_digest": "e3b0c442...",
    "diff_digest": "7852b855..."
  }
  ```
  *Note*: All digest fields are enriched and validated server-side by `prepare_input` at submission time to bind the exact disk snapshot, proposed content, and unified diff into the human approval ticket. Re-verified immediately before dispatch. For replacement operations on Windows NT, the approved target is locked at the kernel level (`FILE_SHARE_READ`, no write/delete sharing), staged to an open temporary file handle `h_temp` (`.pico_tmp_*`, `CREATE_NEW`, `FILE_SHARE_READ`, no write/delete sharing), the target is relocated to `.pico_backup_*` via `SetFileInformationByHandle(FileRenameInfo)`, and `h_temp` is renamed directly into place via `SetFileInformationByHandle(FileRenameInfo)` with `ReplaceIfExists = False`. Competing target creation or substitution causes `SetFileInformationByHandle` to fail closed (`ERROR_ALREADY_EXISTS`), marking `h_temp` for disposition, ensuring zero overwrite of competitor content, preserving the displaced original in backup, and failing closed with `ExecutionError`. The original backup file is preserved until independent post-write handle verification (`h_post`) passes; if verification or cleanup encounters a conflict, the backup is preserved on disk for recovery and the proposal remains `staged`. On non-Windows platforms, filesystem mutations fail closed (`ExecutionError`).
- **Output Schema**:
  ```json
  {
    "proposal_id": "prop_01abc123",
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "status": "applied",
    "verification_status": "verified",
    "verified_digest": "e3b0c442...",
    "verification_details": {
      "verified": true,
      "written_bytes": 22,
      "verified_digest": "e3b0c442..."
    },
    "message": null
  }
  ```

#### Capability: `filesystem.rollback_staged_diff`
- **Capability ID**: `filesystem.rollback_staged_diff` (version: `1.0.0`)
- **Risk Tier**: `HIGH`
- **Required Permission**: `filesystem:write`
- **Data Classification**: `INTERNAL`
- **Consequential**: `true` (Requires explicit human approval) | **Idempotent**: `false` | **Max Retries**: 0 | **Timeout**: 15.0s
- **Input Schema**:
  ```json
  {
    "proposal_id": "prop_01abc123",
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "operation": "create",
    "post_apply_digest": "e3b0c442...",
    "expected_restored_digest": "ABSENT"
  }
  ```
  *Note*: All digest fields are enriched and validated server-side by `prepare_input` at submission time. Fails closed with `AuthorizationError` if the destination file has been modified by an external editor since application.
- **Output Schema**:
  ```json
  {
    "proposal_id": "prop_01abc123",
    "root_id": "workspace",
    "relative_path": "docs/notes.txt",
    "status": "rolled_back",
    "verification_status": "verified",
    "restored_digest": "ABSENT",
    "verification_details": {
      "verified": true,
      "restored_digest": "ABSENT"
    },
    "message": null
  }
  ```

#### Capability: `browser.open_page`
- **Capability ID**: `browser.open_page` (version: `1.0.0`)
- **Risk Tier**: `READ_ONLY`
- **Required Permission**: `browser:open`
- **Data Classification**: `INTERNAL`
- **Consequential**: `false` | **Idempotent**: `true` | **Max Retries**: 0 | **Timeout**: 15.0s | **Max Output Bytes**: 65536
- **Configuration & Enablement**:
  - `BROWSER_AUTOMATION_ENABLED`: `false` by default. Must be set to `true` to enable browser operations.
  - `BROWSER_ALLOWED_ORIGINS`: `[]` by default. Unapproved origins fail closed with `status="rejected"`.
  - `BROWSER_NAVIGATION_TIMEOUT_SECONDS`: default `15.0`.
  - `BROWSER_MAX_OBSERVATION_BYTES`: default `65536`.
  - `BROWSER_MAX_LINKS`: default `50`.
  - `BROWSER_HEADLESS`: default `true`.
  - `BROWSER_BLOCK_THIRD_PARTY_SUBRESOURCES`: default `true`.
- **SSRF Mitigations**:
  - Loopback, private (RFC 1918 / RFC 4193), link-local, multicast, unspecified, and cloud metadata (`169.254.169.254`, `metadata.google.internal`) addresses and resolved hostnames are strictly rejected before navigation and on all redirects.
- **Untrusted Web Content & Privacy Boundaries**:
  - External web text is classified as `INTERNAL`.
  - `RULE_PRIVATE_LOCAL_PIN` strictly pins observations locally; cloud routing is forbidden.
  - Web text cannot elevate caller permissions, alter tool schemas, or bypass human approvals.
- **Input Schema**:
  ```json
  {
    "url": "https://approved.example.com/docs"
  }
  ```
- **Output Schema**:
  ```json
  {
    "status": "success",
    "requested_url": "https://approved.example.com/docs",
    "final_url": "https://approved.example.com/docs",
    "title": "Documentation",
    "text_content": "Extracted text content...",
    "links": [
      {
        "url": "https://approved.example.com/docs/guide",
        "text": "User Guide"
      }
    ],
    "truncated": false,
    "byte_size": 1024,
    "classification": "INTERNAL",
    "message": null
  }
  ```

---

### 2.15 Workflow Coordination & Task Delegation (Milestone P9.1)

Durable, dependency-aware workflow coordinator for multi-step task delegation executing over PICO's existing `ToolExecutionEngine` and `CapabilityRegistry`.

#### `POST /api/workflows`
- **Description**: Registers and persists a validated, bounded dependency workflow graph. Validates DAG invariants (no cycles, no self-dependencies, resolved prerequisites, valid capability schemas, graph bounds). Supports idempotent submissions.
- **Request Body**:
  ```json
  {
    "name": "Chained Computation Workflow",
    "description": "Performs multi-step task execution",
    "task_id": "task_12345",
    "session_id": "sess_abc",
    "correlation_id": "corr_xyz",
    "idempotency_key": "wf-unique-key-42",
    "data_classification": "INTERNAL",
    "metadata": {},
    "steps": [
      {
        "step_key": "step_1",
        "capability_id": "core.calculator",
        "capability_version": "1.0.0",
        "dependencies": [],
        "input_template": {
          "expression": "10 + 20"
        }
      },
      {
        "step_key": "step_2",
        "capability_id": "core.calculator",
        "capability_version": "1.0.0",
        "dependencies": ["step_1"],
        "input_template": {
          "expression": "30 * 2"
        }
      }
    ]
  }
  ```
- **Validation**:
  - `name`: 1-255 characters.
  - `steps`: 1-50 steps.
  - Graph depth: <= 20 levels.
  - Payload size: <= 1,048,576 bytes.
  - Dependencies must be acyclic and reference declared step keys.
  - Dependency references in inputs (`{"$from_step": "<step_key>", "$path": "<path>"}`) must declare upstream step in `dependencies`.
- **Response (201 Created)**: Returns `WorkflowRunResponse` with status `queued` and step summaries.

#### `GET /api/workflows`
- **Description**: Lists and filters workflow runs with pagination.
- **Query Parameters**:
  - `task_id` (optional): Filter by parent user task ID.
  - `status` (optional): Filter by workflow run status (`queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`).
  - `limit` (default: 50, max: 100).
  - `offset` (default: 0).
- **Response (200 OK)**:
  ```json
  {
    "items": [...],
    "total": 1,
    "limit": 50,
    "offset": 0
  }
  ```

#### `GET /api/workflows/{workflow_id}`
- **Description**: Retrieves full workflow run state, including composed step statuses, resolved inputs, outputs, error messages, and tool execution references.
- **Response (200 OK)**: `WorkflowRunResponse`.

#### `POST /api/workflows/{workflow_id}/advance`
- **Description**: Advances workflow coordinator, evaluating prerequisite readiness and sequentially executing eligible ready steps through `ToolExecutionEngine`. Steps awaiting approval remain paused until human approval is recorded.
- **Request Body**:
  ```json
  {
    "caller_permissions": ["tools:calculate", "test:execute"],
    "max_steps": 10
  }
  ```
- **Response (200 OK)**: Updated `WorkflowRunResponse`.

#### `POST /api/workflows/{workflow_id}/cancel`
- **Description**: Cancels an active or queued workflow run. Cancels pending/running steps and associated queued tool executions.
- **Request Body**:
  ```json
  {
    "reason": "Operator requested cancellation."
  }
  ```
- **Response (200 OK)**: Updated `WorkflowRunResponse` with status `cancelled`.

---

### 2.16 Single-Model Resource Scheduler & Logical Worker Multiplexing (Milestone P9.2)

Provider-neutral model execution scheduling layer enforcing single-model residency over target hardware (`RTX 3050 Laptop GPU, 4 GB VRAM`), fair priority queueing across logical worker roles, bounded backpressure, and strict cloud privacy controls.

#### `GET /api/scheduler/status`
- **Description**: Returns bounded, sanitized operational diagnostics of the resource scheduler, including current residency, queue depth, priority class distribution, and truthful memory telemetry. Does not leak prompts, context, or credentials.
- **Response (200 OK)**:
  ```json
  {
    "enabled": true,
    "active_local_model": "qwen3.5:2b",
    "max_active_local": 1,
    "current_active_local": 0,
    "max_active_cloud": 5,
    "current_active_cloud": 0,
    "queue_depth": 0,
    "max_queue_depth": 50,
    "queue_by_priority": {
      "interactive": 0,
      "workflow": 0,
      "background": 0
    },
    "telemetry": {
      "ram_total_mb": 16384,
      "ram_available_mb": 8192,
      "vram_total_mb": 4096,
      "vram_allocated_mb": 2437,
      "vram_free_mb": 1659,
      "is_estimated": false,
      "source": "psutil"
    },
    "recent_outcomes": []
  }
  ```

#### `GET /api/scheduler/metrics`
- **Description**: Returns operational counter summaries for the scheduler (completed, failed, timed out, cancelled, rejected).
- **Response (200 OK)**:
  ```json
  {
    "enabled": true,
    "total_dispatched": 42,
    "total_completed": 40,
    "total_failed": 1,
    "total_timeouts": 0,
    "total_cancelled": 1,
    "total_rejected": 0,
    "queue_depth": 0
  }
  ```

#### Capability: `model.infer`
- **Capability ID**: `model.infer` (version: `1.0.0`)
- **Risk Tier**: `READ_ONLY`
- **Required Permission**: `model:infer`
- **Data Classification**: `INTERNAL`
- **Consequential**: `false` | **Idempotent**: `true` | **Max Retries**: 0 | **Timeout**: 60.0s
- **Description**: Executes governed model inference for logical workers (planner, researcher, executor, summarizer, routine) scheduled through `ResourceScheduler`.
- **Input Schema**:
  ```json
  {
    "prompt": "Summarize the observation.",
    "system_prompt": "You are a helpful assistant.",
    "role": "summarizer",
    "priority": "workflow",
    "model_name": "qwen3.5:2b",
    "max_tokens": 512,
    "temperature": 0.2
  }
  ```
- **Output Schema**:
  ```json
  {
    "status": "success",
    "text": "Generated response content.",
    "model": "qwen3.5:2b",
    "provider": "ollama",
    "usage": {
      "prompt_tokens": 120,
      "completion_tokens": 45,
      "total_tokens": 165
    },
    "execution_time_ms": 450.2,
    "data_classification": "INTERNAL"
  }
  ```

---

### 2.17 Independent Verification & Bounded Compensation (Milestone P9.3)

Workflow-level verification engine gating the release of step outputs to downstream dependent steps, multi-stage evidence auditing (structural, execution evidence, domain semantic/postconditions, risk-sensitive policy decision), model-assisted review via `ResourceScheduler` (`WorkerRole.VERIFIER`), and controlled reverse-dependency compensation.

#### `GET /api/workflows/{workflow_id}/verifications`
- **Description**: Retrieves all durable verification decision records for the workflow run, detailing evaluation status, pipeline stage reached, method used, input/output digests, sanitized findings, and machine-readable reasons.
- **Response (200 OK)**:
  ```json
  [
    {
      "id": "wfver_abc123456789",
      "workflow_run_id": "wf_1234567890abcdef",
      "workflow_step_id": "step_abcdef123456",
      "tool_execution_id": "exec_fedcba987654",
      "policy_id": "evidence.default.v1",
      "policy_version": "1.0.0",
      "method": "deterministic_recomputation",
      "status": "passed",
      "stage": "stage_d_decision",
      "input_digest": "sha256:...",
      "output_digest": "sha256:...",
      "passed": true,
      "reason": "All verification stages passed: structural integrity, execution evidence, and semantic checks satisfied.",
      "findings": {
        "stage_a_structural": {"status": "passed"},
        "stage_b_evidence": {"status": "passed"},
        "stage_c_semantic": {"status": "passed"},
        "stage_d_decision": {"status": "accepted"}
      },
      "data_classification": "PUBLIC",
      "created_at": "2026-10-22T17:00:00Z",
      "completed_at": "2026-10-22T17:00:01Z"
    }
  ]
  ```

#### `GET /api/workflows/{workflow_id}/steps/{step_key}/verification`
- **Description**: Retrieves the latest durable verification decision record for a specific step in the workflow run.
- **Response (200 OK)**: `WorkflowVerificationResponse`.
- **Response (404 Not Found)**: When step or verification does not exist.

#### `POST /api/workflows/{workflow_id}/compensate`
- **Description**: Triggers reverse-dependency compensation for completed workflow steps having registered compensation definitions. Consequential compensations pause in `awaiting_approval` and require explicit human operator approval before execution.
- **Authorization Note (Milestone P10.1.1)**: `caller_permissions` in the request body is untrusted client input and is strictly ignored by the server. Effective permissions are authoritatively derived on the server from the operator session cookie (`pico_operator_session`) or the `X-Pico-Approval-Token` header.
- **Request Body**:
  ```json
  {
    "caller_permissions": [],
    "reason": "Operator requested compensation rollback."
  }
  ```
- **Response (200 OK)**: Updated `WorkflowRunResponse` with status `compensated` or `awaiting_approval`.

#### `GET /api/workflows/{workflow_id}/compensations`
- **Description**: Retrieves all registered and executed compensation records for the workflow run in reverse dependency order.
- **Response (200 OK)**:
  ```json
  [
    {
      "id": "wfcomp_123456789",
      "workflow_run_id": "wf_1234567890abcdef",
      "workflow_step_id": "step_abcdef123456",
      "original_tool_execution_id": "exec_1",
      "compensation_capability_id": "filesystem.rollback_staged_diff",
      "compensation_capability_version": "1.0.0",
      "compensation_tool_execution_id": "exec_2",
      "status": "completed",
      "input_digest": "sha256:...",
      "order_index": 1,
      "reason": "Registered compensation for step 'apply_diff'",
      "created_at": "2026-10-22T17:00:00Z",
      "updated_at": "2026-10-22T17:00:05Z",
      "completed_at": "2026-10-22T17:00:05Z"
    }
  ]
  ```

---

### 2.18 Operator Session & Pairing API (Milestones P10.1 & P10.1.1)

Local-first desktop operator authentication and consequential action approval boundary.

#### Design Invariants:
- **Loopback-Only Bound**: Endpoints are restricted strictly to loopback interfaces (`127.0.0.1`, `::1`). Non-loopback requests are rejected with `403 Forbidden`.
- **Cross-Process SQLite Persistence**: One-time 6-digit challenges are generated exclusively via the trusted local CLI (`python -m pico.cli.pair`) and persisted in SQLite (`operator_pairing_challenges`). The server validates incoming codes against this store.
- **No Self-Service Browser Minting**: The previous self-service code generation endpoint (`POST /api/operator/pairing-code`) has been removed to preserve the separate operator ceremony.
- **Single-Use & Rate-Limited**: Challenges expire in 300 seconds and are consumed atomically. Failed attempts increment an attempt counter; 5 failed attempts trigger lockout.
- **Bound Operator Identity**: Operator identity is bound to challenge issuance on the server; caller-supplied `approver` parameters cannot override it.
- **HttpOnly Cookies & CSRF Protection**: Upon successful pairing, the backend sets an `HttpOnly; SameSite=Strict; Path=/api` cookie (`pico_operator_session`) and returns a separate CSRF token in JSON. Mutating operations require matching `X-CSRF-Token` headers.

#### `POST /api/operator/pair`
- **Description**: Verifies a one-time 6-digit pairing code issued by `python -m pico.cli.pair` and establishes an authenticated operator session.
- **Request Body**:
  ```json
  {
    "pairing_code": "482910"
  }
  ```
- **Response (200 OK)**:
  ```json
  {
    "status": "paired",
    "approver": "local_operator",
    "csrf_token": "csrf_f4b7c890123456789abcdef...",
    "expires_at": "2026-10-25T19:00:00Z"
  }
  ```
- **Response Headers**: `Set-Cookie: pico_operator_session=...; HttpOnly; SameSite=Strict; Path=/api`
- **Response (400 Bad Request)**: Invalid code format (must be 6 digits).
- **Response (401 Unauthorized)**: Expired code, already consumed code, invalid code, or lockout threshold exceeded.
- **Response (403 Forbidden)**: Request originated from non-loopback address.

#### `GET /api/operator/status`
- **Description**: Inspects whether the calling browser context holds an active, unexpired operator session.
- **Response (200 OK)**:
  ```json
  {
    "paired": true,
    "approver": "local_operator",
    "expires_at": "2026-10-25T19:00:00Z"
  }
  ```

#### `POST /api/operator/logout`
- **Description**: Invalidates the active operator session in SQLite and clears the session cookie.
- **Response (200 OK)**:
  ```json
  {
    "status": "unpaired"
  }
  ```
- **Response Headers**: Clears `pico_operator_session` cookie.

---

## 3. Planned Endpoints (Future Phases)

The following endpoints are strictly planned and not active:

- `GET /api/evaluations/reports` — Evaluation harness metrics and test suite results (Phase 3)

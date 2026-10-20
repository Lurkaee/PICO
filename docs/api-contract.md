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

## 3. Planned Endpoints (Future Phases)

The following endpoints are strictly planned and not active:

- `POST /api/jobs` & `GET /api/jobs/{job_id}` — Multi-step execution coordinator jobs (DAG workflow coordinator)
- `GET /api/evaluations/reports` — Evaluation harness metrics and test suite results (Phase 3)

# PICO Model Gateway & Per-Step Router Design

## 1. Core Principles

The PICO Model Gateway is built on five core principles:
1. **Per-Step Model Selection**: Model selection occurs at the granularity of individual execution steps (classification, planning, tool generation, summarization), not as a static choice frozen at conversation creation.
2. **Deterministic Governance Over Heuristics**: Routing decisions are governed by transparent, deterministic rules that evaluate task category, data classification, and explicit hardware budgets.
3. **Hardware-Aware Local-First Execution**:
   - Primary development runtime: Intel Core i5-13420H, 16 GB RAM, NVIDIA RTX 3050 (4 GB VRAM).
   - Local model `local-small` (`qwen3.5:2b` via native Ollama API) is default for routine classification, extraction, and summarization.
   - Local model `local-mid` (`qwen3.5:4b`) remains disabled by default until benchmark validation passes.
   - Context budget is capped at 4096 tokens (test 8192 only when headroom is verified).
   - Strict single-model residency in memory.
4. **Data Privacy Invariants**:
   - `SECRET` classification: **Never** permitted to leave the local machine. Cloud routing is rejected unconditionally.
   - `SENSITIVE` classification: Stays local by default; requires explicit user interactive consent before any cloud transmission.
   - `INTERNAL` / `PUBLIC`: Permitted for cloud routing only if the user has configured and enabled a cloud provider.
5. **Inspectable Audit Trail**:
   Every routing decision is recorded with the decision rule, selected profile, provider, model name, prompt/completion tokens, latency in milliseconds, and estimated cost in cents.

---

## 2. Model Profiles

```json
{
  "profiles": {
    "local-small": {
      "provider": "ollama",
      "model_tag": "qwen3.5:2b",
      "context_window": 4096,
      "capabilities": ["JSON_SCHEMA", "TAGGING", "EXTRACTION", "SUMMARIZATION"],
      "enabled": true,
      "cost_per_million_input": 0.0,
      "cost_per_million_output": 0.0,
      "requires_cloud_approval": false,
      "max_data_classification": "SECRET"
    },
    "local-mid": {
      "provider": "ollama",
      "model_tag": "qwen3.5:4b",
      "context_window": 4096,
      "capabilities": ["JSON_SCHEMA", "TOOL_PROPOSALS", "MODERATE_REASONING"],
      "enabled": false,
      "benchmark_gate_passed": false,
      "cost_per_million_input": 0.0,
      "cost_per_million_output": 0.0,
      "requires_cloud_approval": false,
      "max_data_classification": "SECRET"
    },
    "cloud-strong": {
      "provider": "configurable_cloud",
      "model_tag": "claude-3-5-sonnet-20241022",
      "context_window": 8192,
      "capabilities": ["DEEP_REASONING", "COMPLEX_CODING", "MULTI_STEP_PLANNING", "TOOL_PROPOSALS"],
      "enabled": false,
      "cost_per_million_input": 3.0,
      "cost_per_million_output": 15.0,
      "requires_cloud_approval": true,
      "max_data_classification": "INTERNAL"
    },
    "mock": {
      "provider": "mock",
      "model_tag": "pico-mock-deterministic",
      "context_window": 4096,
      "capabilities": ["ALL"],
      "enabled": true,
      "cost_per_million_input": 0.0,
      "cost_per_million_output": 0.0,
      "requires_cloud_approval": false,
      "max_data_classification": "SECRET"
    }
  }
}
```

---

## 3. Router Interface Contract

```python
from enum import Enum
from pydantic import BaseModel, Field

class TaskCategory(str, Enum):
    INTENT_CLASSIFICATION = "intent_classification"
    ENTITY_EXTRACTION = "entity_extraction"
    SUMMARIZATION = "summarization"
    PLANNING = "planning"
    CODING = "coding"
    VERIFICATION = "verification"
    GENERAL_CHAT = "general_chat"

class DataClassification(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"
    SECRET = "SECRET"

class ModelCapability(str, Enum):
    STREAMING = "STREAMING"
    JSON_SCHEMA = "JSON_SCHEMA"
    TOOL_PROPOSALS = "TOOL_PROPOSALS"
    DEEP_REASONING = "DEEP_REASONING"

class StepRoutingRequest(BaseModel):
    task_category: TaskCategory
    data_classification: DataClassification
    required_capabilities: list[ModelCapability] = Field(default_factory=list)
    max_budget_cents: float | None = None
    cloud_approved: bool = False
    preferred_profile: str | None = None

class StepRoutingDecision(BaseModel):
    selected_profile: str
    provider: str
    model_name: str
    rule_matched: str
    reason: str
    cloud_approved: bool
    fallback_profiles: list[str] = Field(default_factory=list)
    estimated_cost_cents: float = 0.0
```

---

## 4. Deterministic Policy Rules

1. **Rule 1: Hard Secret Exclusion**
   - Condition: `data_classification == SECRET`
   - Action: `selected_profile` must be local (`local-small`, `local-mid`, or `mock`). Cloud providers are disqualified. If the local model lacks required capabilities for the task, the task is safely refused with an explanatory explanation.
2. **Rule 2: Sensitive Cloud Approval Gate**
   - Condition: `data_classification == SENSITIVE`
   - Action: Cannot route to `cloud-strong` unless `cloud_approved == True`. If approval is missing, routes to `local-small` or halts with an interactive approval request.
3. **Rule 3: Routine Task Optimization**
   - Condition: `task_category in [INTENT_CLASSIFICATION, ENTITY_EXTRACTION, SUMMARIZATION]`
   - Action: Routes to `local-small` if `benchmark_gate_passed == True`. Never waste cloud budget on basic classification or summarization.
4. **Rule 4: Complex Reasoning Routing**
   - Condition: `task_category in [PLANNING, CODING, VERIFICATION]`
   - Action: Routes to `cloud-strong` if configured and permitted by privacy policy. If cloud is unavailable or unconfigured, informs the user rather than silently hallucinating on an underpowered model.
5. **Rule 5: Fallback Hierarchy**
   - Order: `cloud-strong` -> `local-mid` (if cleared) -> `local-small` (if task permits) -> `mock` (in test/dev).
   - If fallback occurs, `fallback_occurred = True` and the reason must be logged in the database.

---

## 5. Phase 1, Slice 2 Implementation Status

The core model gateway and routing subsystems are **ACTIVE and FULLY IMPLEMENTED**:

- **Provider Abstraction** (`pico/providers/base.py`): Typed `ModelProvider` ABC supporting `stream_chat`, `complete_chat`, `check_health`, and explicit metadata.
- **Deterministic Mock Provider** (`pico/providers/mock.py`): 100% offline, deterministic tokens/metadata, test-controlled delays/simulated failures.
- **Native Ollama Provider** (`pico/providers/ollama.py`): Async HTTP streaming against `/api/chat` parsing NDJSON chunks without blocking event loop; explicit error handling for missing models and connection failures.
- **Configurable Cloud Provider** (`pico/providers/cloud.py`): OpenAI-compatible SSE client; strictly disabled by default; zero secret leakage.
- **Deterministic Router** (`pico/services/routing_service.py`): Enforces classification invariants (`SECRET` pinned locally, `INTERNAL` pinned locally, `PUBLIC` cloud-eligible only when enabled).
- **Routing Decision Audit Persistence** (`pico/models/routing_decision.py`): Persisted to SQLite WAL table `routing_decisions` via Alembic migration `849ac33a5a7a`.
- **Streaming Chat Endpoint** (`POST /api/chat/stream`): SSE endpoint emitting `meta`, `token`, `done`, and `error` events with database state guarantees.

---

## 6. Phase 1, Slice 3: Hardware Benchmarking & Evaluation Gates

The benchmark tool (`tools/bench/`) measures the local runtime on the target Windows host without downloading models or transmitting private data:

- **Hardware Telemetry Collector** (`tools/bench/telemetry.py`):
  - Uses Windows kernel32 `GlobalMemoryStatusEx` for host physical RAM and `K32GetProcessMemoryInfo` for process working set.
  - Queries `nvidia-smi` directly for dedicated GPU VRAM allocations.
  - Explicitly tags metrics as `Runner-measured`, `Runtime-reported`, or `unavailable`. Never fabricates values.
- **Preflight Availability Gate** (`tools/bench/runner.py`):
  - Queries `{base_url}/api/tags` to ensure Ollama is active.
  - Verifies presence of the target model (`qwen3.5:2b`).
  - If Ollama is offline or the model is missing, returns an informative `BLOCKED` status with manual installation instructions and refuses automatic downloads.
- **Auditable Benchmark Runs**:
  - Distinguishes initial warm-up run from measured repetitions.
  - Uses synthetic, non-sensitive prompts (`tools/bench/prompts.py`).
  - Generates timestamped JSON and Markdown reports in `bench_reports/`.
  - Supports configurable quality gates (`--min-tokens-per-sec`, `--max-ttft-ms`). Defaults to `BASELINE_RECORDED` until gates are calibrated.

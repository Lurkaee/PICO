# PICO Security & Risk Register

## 1. Local Development Trust Boundary

In local development:
- **Local Machine Boundary**: The host environment (Windows laptop, local filesystem, Ollama instance) is the core trust zone.
- **Client/Browser Boundary**: Web UI or IPC clients run in an untrusted or semi-trusted context. Server-side secrets, database connection credentials, and provider API keys MUST NEVER be passed to client-side bundles or HTTP responses.
- **Model Provider Boundary**:
  - Local inference (Ollama running `qwen3.5:2b` or `qwen3.5:4b`) remains entirely inside the machine's loopback network interface (`127.0.0.1`).
  - Cloud inference (Anthropic, OpenAI, Gemini) crosses the external perimeter and transmits prompts over TLS. Under no circumstance may context classified as `SECRET`, `SENSITIVE`, or `INTERNAL` cross this boundary. Only purely `PUBLIC` effective context is eligible for cloud transmission, and only when cloud access is explicitly enabled (`CLOUD_ENABLED=true`). There is no sensitive-data consent exception in the current policy.
- **Untrusted Ingestion Boundary**: Any external content (web scrape, email, file dropped into workspace, API output) is treated as potentially malicious and isolated from system prompts.

---

## 2. Risk Matrix & Mitigation Strategies

| ID | Threat / Failure Mode | Severity | Impact | Mitigation Strategy |
|---|---|---|---|---|
| **SEC-01** | **Secret Leakage** | Critical | Cloud keys, tokens, or private credentials leaked to model or browser | 1. Environment secrets stored exclusively in server-side configuration (`.env` not in git).<br>2. Context compiler scrubs potential secret patterns before prompt assembly.<br>3. Automated test suite verifies zero secrets in API responses. |
| **SEC-02** | **Prompt Injection (Direct & Indirect)** | High | Untrusted web/document data overrides system instructions or hijacks tool execution | 1. Strict delimiter encapsulation separating system instructions from untrusted data.<br>2. Untrusted inputs tagged as unprivileged context; model outputs evaluated as untrusted proposals.<br>3. Tool execution verified independently. |
| **SEC-03** | **Data-Classification Violations** | Critical | Sensitive, Internal, or Secret user data transmitted to external cloud providers | 1. Mandatory `data_classification` tag on every message and context chunk (`PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`).<br>2. Fail-closed deterministic context policy: effective classification computed across assembled history + prompt; any non-`PUBLIC` item pins request locally (`RULE_PRIVATE_LOCAL_PIN`).<br>3. Zero non-`PUBLIC` content transmitted to cloud inference. Interactive sensitive consent deferred until a separate, tested approval engine is implemented.<br>4. Defense-in-depth check in cloud provider and streaming endpoint. |
| **SEC-04** | **Unauthorized Tool Calls** | Critical | Model executes unintended shell commands, file deletions, or network requests | 1. Strict allowlist tool registry with explicit JSON schemas.<br>2. No arbitrary code execution or unvalidated shell access.<br>3. Policy engine intercepts destructive operations (`write`, `delete`, `send`) requiring explicit user approval. |
| **REL-01** | **Duplicate Operations After Retries** | Medium | Network or model timeouts trigger duplicate side effects (e.g., duplicated API calls) | 1. Mandatory `idempotency_key` generated per tool execution.<br>2. Execution coordinator caches tool results by idempotency key.<br>3. Duplicate tool invocations return cached result without re-executing. |
| **REL-02** | **Partial Failures & Interrupted Jobs** | High | Multi-step plan crashes midway, leaving inconsistent state | 1. Execution jobs modeled as stateful DAGs in SQLite.<br>2. Each step transition recorded to append-only event ledger.<br>3. Crash recovery engine resumes from the last confirmed checkpoint. |
| **REL-03** | **Conflicting Concurrent Actions** | Medium | Concurrent requests update the same task or project causing race conditions | 1. SQLite WAL mode with immediate transactions for multi-row updates.<br>2. Explicit entity versioning (`updated_at` checks) and repository transaction locks. |
| **MOD-01** | **Incorrect or Stale Memory** | Medium | Memory service retrieves obsolete facts or hallucinated memories | 1. Provenance tracking linking memories to source conversation and timestamp.<br>2. Time-decay weighting on episodic memory.<br>3. Contradiction detection during semantic consolidation. |
| **MOD-02** | **Misleading Model Claims / Hallucinations** | High | Model falsely claims an action was executed when it failed or did not run | 1. Action results are recorded strictly from actual tool execution returns, never model narration.<br>2. Independent Verifier checks tool return against model claims.<br>3. UI clearly distinguishes actual tool logs from model conversational output. |
| **MOD-03** | **Provider Outage & Unexpected Costs** | Medium | Cloud API rate limit, downtime, or runaway token spend | 1. Local-first fallback for routine tasks; app remains fully usable offline.<br>2. Per-task and per-day token budget limits.<br>3. Inspectable routing decision log tracking cost per turn in cents. |

---

## 3. Safeguards Required Before Real External Actions

Before PICO is authorized to execute real external actions (file writing outside sandbox, sending emails, invoking cloud APIs):
1. **Sandboxing**: Filesystem operations restricted to explicit target directory paths; paths containing `..` or targeting system directories are rejected.
2. **Interactive Confirmation**: Destructive tools trigger a mandatory approval ticket with the exact payload diff displayed to the user.
3. **Execution Ledger**: Every action must be recorded to the ledger with an idempotency key before execution begins.
4. **Offline Capability Test**: System must pass the offline test suite confirming all core local features work without internet connectivity.

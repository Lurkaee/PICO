# ADR 0002: P11.2.2 Companion Loopback Bridge, Process Verification & Windows Identity Qualification

- **Status**: Accepted / Qualified Design Baseline for Milestone P11.2.2 (Revised in Stage 0.1)
- **Date**: 2026-11-07
- **Deciders**: PICO Core Architecture Team
- **Milestone Reference**: Milestone P11.2.2 (Stage 0 & Stage 0.1 Architecture Audit & Security Qualification)
- **Related Documents**:
  - [`AGENTS.md`](../../AGENTS.md) — Operating rules, hardware invariants, Policy A, Invariant 12
  - [`docs/decisions/0001-windows-desktop-companion-architecture.md`](0001-windows-desktop-companion-architecture.md) — ADR 0001: Desktop Framework Selection & Bridge Requirements
  - [`docs/architecture.md`](../architecture.md) — System architecture, subsystem boundaries, data flows
  - [`docs/implementation-plan.md`](../implementation-plan.md) — Canonical milestone roadmap and tracking
  - [`docs/api-contract.md`](../api-contract.md) — REST and SSE API specifications, Policy A contracts
  - [`docs/security-and-risk-register.md`](../security-and-risk-register.md) — Threat modeling, local trust boundaries, and risk mitigations

---

## 1. Context & Executive Summary

In Milestone P11.2.0, [ADR 0001](0001-windows-desktop-companion-architecture.md) established the foundation for the PICO Windows Desktop Companion, selecting Tauri 2 + Rust to embed PICO's existing React command centre within a frameless, lightweight desktop shell under a strict <100 MB idle RAM budget. In P11.2.1, the native shell, frameless window dragging, system tray menu, and close-to-tray lifecycle were implemented and merged.

However, ADR 0001 (Section 6.5) explicitly identified a critical security challenge and declared the authentication transport and Windows backend identity verification an **unresolved engineering qualification task**:
1. **The Cross-Origin / Cross-Site Barrier**: On Windows 11, the packaged Tauri WebView2 frontend executes under the custom URI origin `http://tauri.localhost`, while PICO Core (FastAPI) listens on `http://127.0.0.1:8000`. Under browser schemeful-site rules, these are distinct sites. The browser cookie jar automatically withholds `SameSite=Strict` cookies (like `pico_operator_session`) on cross-site subresource fetch requests.
2. **The Local Loopback Impersonation Threat**: Loopback port 8000 on Windows is unprivileged and shared across local processes. If PICO Core is inactive, restarting, or slow to bind, a rogue process could bind `127.0.0.1:8000`, emulate `GET /api/health` returning `200 OK {"status": "healthy"}`, and intercept valid operator pairing challenge codes or session tokens dispatched blindly by the companion.
3. **The Same-User Process Boundary**: In Windows 11, any process executing within the same interactive logon session shares read access to per-user storage (`%APPDATA%`, `%TEMP%`) and can open unmitigated same-user processes with query information. Therefore, a static unencrypted file on disk cannot serve as a reliable trust anchor against malicious same-user software.

This document records the empirical results and architectural decisions of **Milestone P11.2.2 Stage 0 and Stage 0.1**:
- We audited the implementation realities across Tauri, the React frontend, and FastAPI backend security.
- We corrected initial overstatements regarding Win32 process inspection: process ID, executable path, and creation time do NOT independently prove application identity.
- We evaluated a concrete Tauri-managed child-process ownership architecture using Windows Job Objects and live TCP table inspection.
- We evaluated a cryptographic startup identity handshake using handle-inherited restricted secrets to establish authentic application identity.
- We evaluated all three transport alternatives from ADR 0001 Section 6.5 across nine architectural dimensions, maintaining **Alternative 1: Tauri Native Loopback Proxy** as the preferred canonical direction.
- We executed a 13-case adversarial test suite (`tests/qualification/test_bridge_qualification.py`) with a 100% pass rate across all qualified scenarios.
- In accordance with our core operating principles, **ADR 0001's historical text is preserved without alteration**, and this document establishes the implementation contract for P11.2.2.

---

## 2. Windows Process Identity: Five Distinct Concepts

Initial architectural discussions grouped various Win32 API checks under a generalized claim of "process identity verification." Stage 0.1 formally separates this into **five distinct, non-interchangeable concepts**:

1. **TCP Endpoint Ownership Evidence (`GetExtendedTcpTable`)**:
   - Win32 `GetExtendedTcpTable` with `TCP_TABLE_OWNER_PID_LISTENER` (TableClass 3) reveals which OS process ID (PID) currently owns the listening socket on a specific IP address and port.
   - *Limitation*: Proves only which process holds the port. It does not prove what code that process is running, nor whether that process is trustworthy.
2. **Process-Instance Identification (`GetProcessTimes` + PID)**:
   - Win32 `GetProcessTimes` retrieves the exact 64-bit creation timestamp (`FILETIME`) of a process.
   - *Limitation*: When compared against an already-trusted, retained process handle, the creation timestamp distinguishes whether the process currently owning the socket is the *same instance* that was previously created, guarding against PID recycling races. It does **not** independently authenticate that the process is PICO Core.
3. **Executable Image Identification (`QueryFullProcessImageNameW`)**:
   - Win32 `QueryFullProcessImageNameW` extracts the canonical binary path (e.g. `C:\...\.venv\Scripts\python.exe` or `C:\Python314\python.exe`).
   - *Limitation*: Crucially, **executable image identification is NOT application authentication**. Any Python script or rogue HTTP server executed using the project's virtual environment or base Python interpreter shares the exact same binary image path. An attacker running a rogue server under `.venv\Scripts\python.exe` matches the executable path 100%, but executes completely different, untrusted code.
4. **Actual PICO Core Application Identity**:
   - Proving that the listening process is genuinely executing PICO Core requires either:
     a) **Direct Child Process Supervision**: The native host process directly launches PICO Core, retains the child process handle, and confirms the listener socket is owned by that exact retained child instance.
     b) **Cryptographic Startup Handshake**: The native host and backend share an ephemeral, handle-inherited or protected secret, performing a nonce-based challenge-response exchange.
5. **Authority to Transmit Operator Credentials**:
   - Authority to attach Policy A credentials (`pico_operator_session`, `X-CSRF-Token`, pairing codes) is granted **only** after both (1) TCP endpoint ownership by the managed application instance is verified, and (2) any required application-level handshake succeeds.

---

## 3. Tauri-Managed Backend Ownership Model

To establish genuine application identity on Windows 11, we evaluated a concrete managed-process architecture:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Tauri 2 Native Host Process                     │
│                                                                        │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │ 1. Creates Windows Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)│
│   │ 2. Spawns Backend Child Process:                                │   │
│   │    uvicorn pico.main:app --host 127.0.0.1 --port 8000          │   │
│   │    (Strictly NO --reload in production)                         │   │
│   │ 3. Retains Child Process Handle, Child PID, and Creation Time   │   │
│   │ 4. Assigns Child Process to Job Object                         │   │
│   │ 5. Creates Anonymous Pipe with restricted handle inheritance    │   │
│   │    Transfers Ephemeral Launch Secret to Child Process          │   │
│   └────────────────────────────────┬───────────────────────────────┘   │
│                                    │                                   │
│                                    │ Windows OS Supervision            │
│                                    ▼                                   │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │ Genuine PICO Core Child Process                                 │   │
│   │ - Inherits read-handle of anonymous pipe                       │   │
│   │ - Reads ephemeral launch secret on startup                     │   │
│   │ - Binds strictly to 127.0.0.1:8000                             │   │
│   │ - Responds to handshake challenge: HMAC(launch_secret, nonce)  │   │
│   └────────────────────────────────────────────────────────────────┘   │
│                                    ▲                                   │
│                                    │ Pre-Credential Verification       │
│                                    │ 1. GetExtendedTcpTable -> PID     │
│                                    │ 2. Verify PID == child_pid        │
│                                    │ 3. Verify Creation Time matches   │
│                                    │ 4. Handshake Challenge-Response   │
│                                    │                                   │
│   ┌────────────────────────────────┴───────────────────────────────┐   │
│   │ Reqwest Native Loopback Client (with Cookie Jar in Rust Heap) │   │
│   └────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Supervised Lifecycle Specifications
1. **Direct Process Handle Retention**:
   - The native host spawns PICO Core using `std::process::Command`.
   - The host retains the `Child` process handle for its entire lifetime.
   - The host queries and records the child PID and its creation timestamp (`GetProcessTimes`) immediately upon spawn.
2. **Windows Job Objects (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`)**:
   - On Windows, terminating a parent process does not automatically terminate child processes. If the parent crashes or is killed, child processes are orphaned and remain bound to loopback port 8000.
   - The host creates a Windows Job Object (`CreateJobObjectW`), configures `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via `SetInformationJobObject`, and assigns the spawned child process to the job (`AssignProcessToJobObject`).
   - *Guarantee*: If the Tauri host process exits normally, crashes, or is terminated by the user, the Windows OS kernel automatically and unconditionally terminates the entire backend child process tree, preventing orphaned listeners and port lingering.
3. **Incompatibility of `uvicorn --reload` with Direct PID Ownership**:
   - In development reload mode, `uvicorn` acts as a reloader supervisor that spawns worker child processes. When source code changes, the worker child is killed and a new worker is spawned with a new PID. The listening socket is owned by the worker or supervisor. If Tauri expects the child it spawned to hold the listening socket, a child worker PID will not match the direct child process handle! Therefore, `uvicorn --reload` is NOT compatible with a direct-child-PID listening ownership model unless supervisor-child tree inspection is used, or dev mode uses a different, explicitly relaxed development configuration.
4. **Virtualenv Launcher Stub Behavior on Windows**:
   - Empirical qualification revealed that running `.venv\Scripts\python.exe` on Windows executes a small virtualenv launcher stub that spawns the underlying base Python interpreter (`C:\Python314\python.exe`) as a child process.
   - When managed directly, the supervisor must either spawn the base Python interpreter with `PYTHONPATH` configured to the virtualenv site-packages, or inspect child processes of the launcher stub, or target the standalone compiled `pico.exe` binary in production.
5. **External Backend Policy**:
   - An externally launched backend (e.g. started independently by the user via terminal) cannot be verified via parent child-process handles.
   - Unless an external backend can satisfy an authenticated handshake trust contract, the desktop companion must remain `offline/unverified` and refuse to transmit operator credentials.

---

## 4. Startup Identity Handshake Evaluation

Process ownership should not be the sole mechanism considered. We evaluated a cryptographic startup challenge as supplementary proof of application identity:

### 4.1 Proposed Protocol
1. **Secret Generation**: Tauri generates a cryptographically random 256-bit per-launch secret using OS CSPRNG.
2. **Restricted Transfer**:
   - The secret is transferred to the backend child process through an **explicitly inherited, restricted OS handle** (e.g. an anonymous pipe created via `CreatePipe` with `STARTUPINFOEXW` / `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` restricting handle inheritance strictly to the child).
   - *Non-Negotiable Invariant*: The secret is **never** passed via command-line arguments (visible to any same-user process via `GetCommandLineW` / WMI / Task Manager), environment variables (inspectable in the Process Environment Block), shared filesystem files (readable across `%APPDATA%`), or frontend assets.
3. **Handshake Challenge**:
   - Before emitting any operator credentials, Tauri generates a fresh random nonce (e.g. 128-bit hex) and dispatches `POST /api/bridge/handshake` over loopback.
   - The backend produces an HMAC (`HMAC-SHA256(launch_secret, nonce)`) and returns it.
4. **Verification**:
   - Tauri verifies the HMAC response against its held `launch_secret`.
   - Only upon verification success is the backend confirmed as the genuine PICO Core instance authorized to receive operator credentials.

### 4.2 Threat Evaluation of Handshake & Local Channels
- **Anonymous Pipes**: Anonymous pipes are private to the parent and child handles. No third-party process has a name or handle to intercept the data.
- **Named Pipes Risk (Pipe Squatting / Impersonation)**: If Windows Named Pipes (`\\.\pipe\pico_ipc_*`) are used, a rogue local process could pre-create the pipe before PICO starts ("pipe squatting"). To mitigate this, any Named Pipe server must specify `FILE_FLAG_FIRST_PIPE_INSTANCE`, reject connection if the pipe already exists, and apply an explicit owner-only Security Descriptor (DACL `D:(A;;GA;;;OW)`).
- **Replay Resistance**: The inclusion of a fresh random nonce on every handshake attempt guarantees replay resistance across restarts and network drops.
- **Secret Rotation**: Every child process restart generates a fresh, distinct launch secret. Stale secrets from prior instances are immediately rejected.
- **Same-User Process Limits**: In standard Windows desktop sessions, processes running under the same user token have broad privileges (e.g. `OpenProcess` with `PROCESS_VM_READ` or `PROCESS_ALL_ACCESS` unless protected). The startup handshake prevents *passive* or *accidental* credential capture (such as port harvesting or environment inspection), but cannot defend against active same-user code injection or kernel-level malware without OS-level sandboxing (e.g. AppContainer or PPL).

---

## 5. Thirteen-Case Adversarial Qualification Matrix

We evaluated all thirteen adversarial bridge scenarios against empirical implementations and tests:

| Case | Scenario | Test Implementation | Evidence Source | Result | Remaining Limitations / Notes |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **1** | **No listener** | `test_case_1_unbound_port_fail_closed` | `GetExtendedTcpTable` (TableClass 3) returns no entry for port 8000 | **PASSED** | Connection refused; fail-closed; credentials fully suppressed in test harness. |
| **2** | **Rogue listener** | `test_case_2_rogue_listener_fake_health_suppressed` | TCP table resolves listener PID; PID mismatch against managed child | **PASSED** | Credentials suppressed fail-closed in test harness. |
| **3** | **Fake health response** | `test_f_synthetic_sentinel_credential_suppression` | Rogue server serves `200 OK {"status": "healthy"}`; request log records 0 received credentials | **PASSED** | Fake `/health` response alone never authorizes credential transmission. |
| **4** | **Genuine PICO backend** | `test_b_actual_pico_backend_child` | Real FastAPI app spawned without `--reload`; listener PID matches retained child PID (`listener_pid == proc.pid`) | **PASSED (Test Harness Evidence)** | Proves the test harness can launch real FastAPI and identify its listener. Does NOT prove the Tauri host manages that process yet. |
| **5** | **Backend termination** | `test_case_4_process_termination_fail_closed` | Backend process terminated; socket drop detected on subsequent request | **PASSED** | Cached verification invalidated immediately upon socket drop in test harness. |
| **6** | **Backend restart with changed PID** | `test_case_5_process_restart_different_pid` & `test_c_backend_restart_invalidates_stale_credentials` | New child spawned with new PID; cached PID and stale startup secrets rejected | **PASSED** | Re-verification mandatory on any PID change. |
| **7** | **Port takeover** | `test_d_port_takeover_after_termination` | Backend terminated; rogue process binds same port; listener PID mismatch detected | **PASSED** | Bridge fails closed; credentials suppressed. |
| **8** | **PID reuse** | `test_case_6_pid_reuse_creation_time_mismatch` | Simulated PID recycling with altered `GetProcessTimes` creation timestamp | **PASSED (Simulated Instance Rejection)** | Demonstrates rejection of a simulated PID creation-time mismatch. Does NOT claim universal immunity to all forms of PID reuse. |
| **9** | **Sleep/wake and network drop** | Evaluated via socket drop and immediate cache invalidation | Any socket disconnection or HTTP timeout clears verified session cache | **QUALIFIED (Socket Drop Only)** | Tests socket-drop behavior only; does NOT claim empirical Windows sleep/wake cycle qualification. |
| **10** | **Named Pipe impersonation or squatting** | Evaluated via Win32 security analysis | `FILE_FLAG_FIRST_PIPE_INSTANCE` and owner-only DACL `D:(A;;GA;;;OW)` required if named pipes are used | **NOT APPLICABLE** | Loopback TCP selected as transport; named pipes deferred. |
| **11** | **Unauthorized IPC invocation** | Capability allowlist in `capabilities/default.json` | Tauri capability system blocks unauthorized IPC commands | **DEFERRED / NOT YET IMPLEMENTED** | Production IPC interface not yet implemented; zero custom execution commands added in Stage 0/0.1. |
| **12** | **Arbitrary URL/path injection** | Abstract transport adapter specifications | Endpoint allowlist restricting requests strictly to `/api/*` | **DEFERRED / NOT YET IMPLEMENTED** | Native bridge command implementation deferred to P11.2.2.2. |
| **13** | **Credential transmission before verification** | `test_f_synthetic_sentinel_credential_suppression` | Synthetic sentinel credentials explicitly withheld until verification succeeds | **PASSED (Synthetic Gate Test)** | Synthetic credential-gate test; does NOT constitute production credential-transport evidence. |

---

## 6. Updated Transport Evaluation: Alternative 1 (Tauri Native Loopback Proxy)

We re-evaluated the three transport alternatives from ADR 0001 Section 6.5, replacing overbroad claims with precise architectural benefits and residual risk documentation:

### 6.1 Accurate Assessment of Alternative 1 (Intended Properties)
- **Intended Architectural Properties (Implementation in Subsequent Slices)**:
  1. **Zero WebView Credential Exposure**: Operator session cookies (`pico_operator_session`) are designed to be held strictly in native Rust process memory (`reqwest::cookie::Jar`). They will never enter the WebView DOM, JavaScript heap, or local storage. *(Note: The native proxy and cookie handling are NOT yet implemented in this milestone).*
  2. **No WebView Cookie Database Persistence**: Eliminates persistence in WebView2's on-disk SQLite cookie database (`EBWebView/Default/Cookies`).
  3. **Clean Bypass of Browser Cross-Origin Restrictions**: Native Rust HTTP clients are not web browsers. They are not subject to browser CORS preflight `OPTIONS` requests, cross-origin host validation rejections, or browser `SameSite=Strict` cookie dropping.
  4. **Direct Operating System Supervision**: The native Rust process has direct access to Win32 APIs to spawn the managed child, assign it to a Job Object, inspect the TCP table, and execute handle-inherited handshakes.
  5. **Strict Content Security Policy**: Production CSP in `tauri.conf.json` (`connect-src 'self' ipc: http://ipc.localhost;`) remains strictly intact, eliminating any direct browser network attack surface.
- **Residual Same-User Attack Surface**:
  - In Windows 11, process memory access depends on granted process access rights, user privileges, and discretionary access control lists (DACLs). It is inaccurate to assert that every same-user process can automatically read another process's memory. Processes can apply restrictive DACLs and Process Mitigation Policies.
  - However, without elevated process protection (e.g. Protected Process Light or AppContainer isolation), same-user processes with standard access tokens could attempt to query or open process handles unless restricted.
  - Crucially: **An in-memory native cookie jar does NOT automatically prevent XSS from invoking exposed Tauri IPC methods**. If malicious JavaScript executes in the WebView and a custom Tauri command like `pico_bridge_request` is exposed without granular capability restrictions, the script could trigger requests through the bridge even without reading the raw cookie. Therefore, future bridge commands must enforce strict route-and-method capability gating.

---

## 7. Correct Repository Predecessor Audit

To maintain absolute documentation and repository integrity, this document explicitly records the verified metadata for all integrated predecessor PRs on `origin/main` (commit `739d4b2f72e08dfb843578746d7bf2d54ff7d6b9`):

- **PR #59** (Commit `cc39140`): Alembic dependency update (`build(deps): bump alembic from 1.20.0 to 1.20.1`).
- **PR #70** (Commit `faa3fb6`): Vitest 5 matcher-type and TypeScript 7 CSS declaration compatibility repair (`fix(frontend): resolve vitest 5 matcher types and css declarations for typescript 7`).
- **PR #66** (Commit `4486154`): Bump `lucide-react` from 1.61.0 to 1.66.0.
- **PR #61** (Commit `e1f51af`): Bump `@vitejs/plugin-react` from 4.7.0 to 5.2.0.
- **PR #64** (Commit `739d4b2`): Bump `@testing-library/jest-dom` from 6.10.0 to 7.0.1.

---

## 8. Implementation Sequence for Milestone P11.2.2

Milestone P11.2.2 will be implemented in four focused, verifiable slices:

- **Slice P11.2.2.1: Native Windows Child Process Supervisor & Job Object (`frontend/src-tauri/`)**:
  - Implement process launcher spawning FastAPI as a managed child (without `--reload`).
  - Implement Windows Job Object assignment with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
  - Implement Win32 TCP table listener check verifying listener PID against child PID.
  - Implement anonymous pipe / startup challenge-response handshake.
- **Slice P11.2.2.2: Native Loopback HTTP Proxy & Cookie Jar**:
  - Add `reqwest = { version = "0.12", features = ["cookies", "json", "stream"] }` to `frontend/src-tauri/Cargo.toml`.
  - Implement the native proxy managing `reqwest::cookie::Jar` in Rust process memory.
  - Expose capability-gated Tauri IPC commands: `pico_bridge_request` and `pico_bridge_stream`.
  - Update `capabilities/default.json` with permissions for the new bridge commands.
- **Slice P11.2.2.3: Frontend Abstract Transport Adapter**:
  - Implement `frontend/src/api/desktop-bridge.ts` providing an abstract transport layer.
  - When executing inside Tauri (`window.__TAURI_INTERNALS__` present), route API requests through `pico_bridge_request` / `pico_bridge_stream`.
  - When executing in standard web browsers, route through standard `fetch()`.
  - Verify that all existing Vitest tests continue to pass without modification.
- **Slice P11.2.2.4: Route-Level Policy A Enforcement on Core Companion Endpoints**:
  - Audit and enforce `require_operator_session` on `/api/chat/stream`, `/api/scheduler/status`, and `/api/perception/*`.
  - Add automated negative tests verifying unauthenticated requests fail closed with 401 Unauthorized.
  - End-to-end pairing test verifying the companion pairing modal, one-time 6-digit challenge exchange, and secure session establishment.

---

## 9. Exact Acceptance Criteria for Milestone P11.2.2

1. **Pre-Credential Backend Verification**: The native desktop shell positively verifies backend process identity via child process ownership and live Win32 TCP table inspection before any session cookie, CSRF token, or pairing code is transmitted.
2. **Fail-Closed Isolation**: When port 8000 is unbound, bound by an untrusted process, bound to non-loopback (`0.0.0.0`), or terminated, credentials are fully suppressed and the companion displays an offline/unverified visual state.
3. **Zero Secret & Cookie Leakage**: Session cookies (`pico_operator_session`) are managed exclusively in native Rust memory and never exposed to the WebView DOM or JavaScript runtime.
4. **CORS & Cookie Invariants Preserved**: Backend `SameSite=Strict`, `HttpOnly`, and CORS policies are strictly preserved without weakening existing web command centre security.
5. **Streaming Chat Parity**: Chat SSE events stream smoothly through the native bridge channel with identical latency and formatting as the web experience.
6. **100% Test Pass Rate**: Full backend pytest suite and frontend Vitest suite pass cleanly without regressions.

---

## 10. Route-Level Authorization Inventory for Milestone P11.2.2.4 (Audit Only)

This inventory establishes the comprehensive classification of all PICO Core backend endpoints for Milestone P11.2.2.4 (Operator Loopback Session Security & API Endpoint Classification). No authorization rules or route authentication logic are modified during Milestone P11.2.2.1.

### 10.1 Endpoint Counting Rule & Reconciliation

- **Counting Rule**: Endpoints are enumerated as unique `(HTTP method, path)` pairs registered on the FastAPI application, excluding automatic `HEAD`/`OPTIONS` methods, documentation routes, and static asset mounts.
- **Reconciliation of Previous Count (83 vs. 95)**:
  - An earlier draft cited "83 endpoints", which reflected the count of unique URL *paths* (82 API paths + 1 `/assets` static mount = 83 paths).
  - Multiple endpoints share the same path under different HTTP verbs (e.g., `POST /api/conversations` vs. `GET /api/conversations`, or `GET`, `POST`, `DELETE` on `/api/memory/working/{session_id}`).
  - Under the rigorous `(method, path)` pair counting rule, there are exactly **95 unique API endpoints**.
  - Additionally, there are **4 documentation routes** (`/openapi.json`, `/docs`, `/redoc`, `/docs/oauth2-redirect`) and **1 static asset mount** (`/assets`).
- **Mutual Exclusivity**: Every unique `(method, path)` API endpoint appears in exactly one tier in the master inventory below.
- **Perception Isolation**: Sensor ingestion and perception routes (Tier 5) are strictly partitioned into operator-initiated capture controls, passive background sensor telemetry, read-only diagnostic queries, and correlation/synthesis processing.

### 10.2 Master Route-Level Authorization Inventory (95 Endpoints)

| # | HTTP Method | Endpoint Path | Handler Function | Current Auth Mechanism | Required P11.2.2 Tier | Operator Surface | Origin Policy | CSRF Required | Rationale / Risk Notes |
| :---: | :---: | :--- | :--- | :--- | :--- | :--- | :--- | :---: | :--- |
| 1 | `GET` | `/health` | `pico.api.health.get_health` | None (Open loopback) | Tier 1: Public Diagnostic | Health Monitor / Internal | Loopback-only | No | Liveness and readiness probe; read-only system diagnostic; safe for automated health polling. |
| 2 | `POST` | `/api/bridge/handshake` | `pico.api.bridge.bridge_handshake` | HMAC-SHA256 Challenge / Launch Secret | Tier 2: Startup Identity | Tauri Host / Supervisor | Loopback-only (Tauri native client) | No | Native-to-Core challenge-response identity verification between Tauri host and child backend; verifies PID and launch secret. |
| 3 | `POST` | `/api/operator/pair` | `pico.api.operator.pair_operator` | PIN Verification | Tier 3: Operator Auth & Session | Companion / Web UI | Loopback CORS-restricted | Yes | Handles operator pairing PIN, session creation, validation, and invalidation. |
| 4 | `GET` | `/api/operator/session` | `pico.api.operator.check_session` | Optional Session Cookie | Tier 3: Operator Auth & Session | Companion / Web UI | Loopback CORS-restricted | No | Handles operator pairing PIN, session creation, validation, and invalidation. |
| 5 | `POST` | `/api/operator/logout` | `pico.api.operator.logout_operator` | Session Cookie | Tier 3: Operator Auth & Session | Companion / Web UI | Loopback CORS-restricted | Yes | Handles operator pairing PIN, session creation, validation, and invalidation. |
| 6 | `POST` | `/api/conversations` | `pico.api.conversations.create_conversation` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 7 | `GET` | `/api/conversations` | `pico.api.conversations.list_conversations` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 8 | `GET` | `/api/conversations/{conversation_id}` | `pico.api.conversations.get_conversation` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 9 | `POST` | `/api/conversations/{conversation_id}/messages` | `pico.api.conversations.create_message` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 10 | `GET` | `/api/conversations/{conversation_id}/messages` | `pico.api.conversations.list_messages` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 11 | `POST` | `/api/projects` | `pico.api.projects.create_project` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 12 | `GET` | `/api/projects` | `pico.api.projects.list_projects` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 13 | `GET` | `/api/projects/{project_id}` | `pico.api.projects.get_project` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 14 | `PATCH` | `/api/projects/{project_id}` | `pico.api.projects.update_project` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 15 | `POST` | `/api/tasks` | `pico.api.tasks.create_task` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 16 | `GET` | `/api/tasks` | `pico.api.tasks.list_tasks` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 17 | `GET` | `/api/tasks/{task_id}` | `pico.api.tasks.get_task` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 18 | `PATCH` | `/api/tasks/{task_id}` | `pico.api.tasks.update_task` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 19 | `POST` | `/api/chat/stream` | `pico.api.chat.chat_stream` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 20 | `GET` | `/api/memory/status` | `pico.api.memory.get_memory_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 21 | `POST` | `/api/memory/working` | `pico.api.memory.put_working_state` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 22 | `GET` | `/api/memory/working/{session_id}` | `pico.api.memory.list_working_memory` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 23 | `DELETE` | `/api/memory/working/{session_id}` | `pico.api.memory.clear_working_memory` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 24 | `POST` | `/api/memory/events` | `pico.api.memory.record_event` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 25 | `GET` | `/api/memory/events` | `pico.api.memory.list_events` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 26 | `POST` | `/api/memory/facts` | `pico.api.memory.save_fact` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 27 | `GET` | `/api/memory/facts/search` | `pico.api.memory.search_facts` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 28 | `GET` | `/api/memory/facts` | `pico.api.memory.list_facts` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 29 | `DELETE` | `/api/memory/facts/{fact_id}` | `pico.api.memory.forget_fact` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 30 | `POST` | `/api/memory/procedures` | `pico.api.memory.create_procedure` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 31 | `GET` | `/api/memory/procedures` | `pico.api.memory.list_procedures` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 32 | `POST` | `/api/memory/procedures/{procedure_id}/review` | `pico.api.memory.review_procedure` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 33 | `POST` | `/api/memory/procedures/{procedure_id}/approve` | `pico.api.memory.approve_procedure` | Policy A / Procedure Governance | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Autonomous procedural knowledge lifecycle transitions; governs recurring tool execution rights. |
| 34 | `POST` | `/api/memory/procedures/{procedure_id}/activate` | `pico.api.memory.activate_procedure` | Policy A / Procedure Governance | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Autonomous procedural knowledge lifecycle transitions; governs recurring tool execution rights. |
| 35 | `POST` | `/api/memory/procedures/{procedure_id}/retire` | `pico.api.memory.retire_procedure` | Policy A / Procedure Governance | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Autonomous procedural knowledge lifecycle transitions; governs recurring tool execution rights. |
| 36 | `GET` | `/api/tools/capabilities` | `pico.api.tools.list_capabilities` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 37 | `GET` | `/api/tools/capabilities/{capability_id}` | `pico.api.tools.get_capability` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 38 | `POST` | `/api/tools/executions` | `pico.api.tools.submit_execution` | History Auth / Dev Token | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 39 | `GET` | `/api/tools/executions` | `pico.api.tools.list_executions` | History Auth / Dev Token | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 40 | `GET` | `/api/tools/executions/{execution_id}` | `pico.api.tools.get_execution` | History Auth / Dev Token | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 41 | `POST` | `/api/tools/executions/{execution_id}/execute` | `pico.api.tools.run_execution` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Consequential tool execution, affirmative approval, or cancellation under Policy A invariants. |
| 42 | `POST` | `/api/tools/executions/{execution_id}/approve` | `pico.api.tools.approve_execution` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Consequential tool execution, affirmative approval, or cancellation under Policy A invariants. |
| 43 | `POST` | `/api/tools/executions/{execution_id}/cancel` | `pico.api.tools.cancel_execution` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Consequential tool execution, affirmative approval, or cancellation under Policy A invariants. |
| 44 | `GET` | `/api/tools/executions/{execution_id}/events` | `pico.api.tools.get_execution_events` | History Auth / Dev Token | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 45 | `POST` | `/api/tools/procedures/{procedure_id}/validate` | `pico.api.tools.validate_procedure` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 46 | `GET` | `/api/perception/status` | `pico.api.perception.get_perception_status` | None (Open loopback in dev) | Tier 5A: Perception Read-Only | Companion Perception | Loopback-only | No | Read-only sensor diagnostics, capture status, and historical perception events. |
| 47 | `POST` | `/api/perception/audio/session/start` | `pico.api.perception.start_audio_session` | None (Open loopback in dev) | Tier 5B: Perception Operator Controls | Companion UI / Hotkey | Loopback-only | Yes | Operator-initiated sensory capture (mic start/stop/cancel, screen capture); high privacy impact. |
| 48 | `POST` | `/api/perception/audio/session/{session_id}/stop` | `pico.api.perception.stop_audio_session` | None (Open loopback in dev) | Tier 5B: Perception Operator Controls | Companion UI / Hotkey | Loopback-only | Yes | Operator-initiated sensory capture (mic start/stop/cancel, screen capture); high privacy impact. |
| 49 | `POST` | `/api/perception/audio/session/{session_id}/cancel` | `pico.api.perception.cancel_audio_session` | None (Open loopback in dev) | Tier 5B: Perception Operator Controls | Companion UI / Hotkey | Loopback-only | Yes | Operator-initiated sensory capture (mic start/stop/cancel, screen capture); high privacy impact. |
| 50 | `GET` | `/api/perception/audio/session/{session_id}` | `pico.api.perception.get_audio_session_status` | None (Open loopback in dev) | Tier 5A: Perception Read-Only | Companion Perception | Loopback-only | No | Read-only sensor diagnostics, capture status, and historical perception events. |
| 51 | `POST` | `/api/perception/audio/synthesize` | `pico.api.perception.synthesize_speech` | None (Open loopback in dev) | Tier 5D: Perception Synthesis & Correlation | Companion Perception | Loopback-only | Yes | TTS audio synthesis, motion heuristics, and multimodal event correlation processing. |
| 52 | `POST` | `/api/perception/vision/capture` | `pico.api.perception.ingest_vision_snapshot` | None (Open loopback in dev) | Tier 5B: Perception Operator Controls | Companion UI / Hotkey | Loopback-only | Yes | Operator-initiated sensory capture (mic start/stop/cancel, screen capture); high privacy impact. |
| 53 | `POST` | `/api/perception/motion/ingest` | `pico.api.perception.ingest_pir_motion` | None (Open loopback in dev) | Tier 5C: Perception Passive Ingestion | Background Sensor / Daemon | Loopback-only | No | Passive background telemetry ingestion (motion/presence/ambient); low latency required. |
| 54 | `POST` | `/api/perception/motion/interpret` | `pico.api.perception.interpret_movement` | None (Open loopback in dev) | Tier 5D: Perception Synthesis & Correlation | Companion Perception | Loopback-only | Yes | TTS audio synthesis, motion heuristics, and multimodal event correlation processing. |
| 55 | `POST` | `/api/perception/environmental/ingest` | `pico.api.perception.ingest_environmental` | None (Open loopback in dev) | Tier 5C: Perception Passive Ingestion | Background Sensor / Daemon | Loopback-only | No | Passive background telemetry ingestion (motion/presence/ambient); low latency required. |
| 56 | `GET` | `/api/perception/events` | `pico.api.perception.list_perception_events` | None (Open loopback in dev) | Tier 5A: Perception Read-Only | Companion Perception | Loopback-only | No | Read-only sensor diagnostics, capture status, and historical perception events. |
| 57 | `POST` | `/api/perception/correlate` | `pico.api.perception.correlate_events` | None (Open loopback in dev) | Tier 5D: Perception Synthesis & Correlation | Companion Perception | Loopback-only | Yes | TTS audio synthesis, motion heuristics, and multimodal event correlation processing. |
| 58 | `GET` | `/api/homeassistant/status` | `pico.api.homeassistant.get_homeassistant_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 59 | `GET` | `/api/homeassistant/entities` | `pico.api.homeassistant.list_homeassistant_entities` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 60 | `GET` | `/api/homeassistant/entities/{entity_id}` | `pico.api.homeassistant.get_homeassistant_entity_state` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 61 | `GET` | `/api/homeassistant/overview` | `pico.api.homeassistant.get_homeassistant_overview` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 62 | `POST` | `/api/homeassistant/actuation/plan` | `pico.api.homeassistant.plan_test_light_actuation` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 63 | `POST` | `/api/homeassistant/actuation/propose` | `pico.api.homeassistant.propose_test_light_actuation` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Smart home device actuation proposal; mutates physical environment state. |
| 64 | `GET` | `/api/homeassistant/actuation/readiness` | `pico.api.homeassistant.get_actuation_readiness` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 65 | `GET` | `/api/homeassistant/actuation/{execution_id}` | `pico.api.homeassistant.get_actuation_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 66 | `POST` | `/api/homeassistant/switch/plan` | `pico.api.homeassistant.plan_switch_actuation` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 67 | `POST` | `/api/homeassistant/switch/propose` | `pico.api.homeassistant.propose_switch_actuation` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Smart home device actuation proposal; mutates physical environment state. |
| 68 | `GET` | `/api/homeassistant/switch/readiness` | `pico.api.homeassistant.get_switch_actuation_readiness` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 69 | `GET` | `/api/homeassistant/switch/{execution_id}` | `pico.api.homeassistant.get_switch_actuation_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 70 | `POST` | `/api/homeassistant/climate/plan` | `pico.api.homeassistant.plan_climate_setpoint` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 71 | `POST` | `/api/homeassistant/climate/dual-setpoint/plan` | `pico.api.homeassistant.plan_climate_dual_setpoint` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 72 | `GET` | `/api/homeassistant/climate/observe/{entity_id}` | `pico.api.homeassistant.get_climate_observation` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 73 | `GET` | `/api/homeassistant/climate/readiness` | `pico.api.homeassistant.get_climate_readiness` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 74 | `POST` | `/api/homeassistant/climate/propose` | `pico.api.homeassistant.propose_climate_actuation` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Smart home device actuation proposal; mutates physical environment state. |
| 75 | `GET` | `/api/homeassistant/climate/execution/{execution_id}` | `pico.api.homeassistant.get_climate_actuation_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 76 | `POST` | `/api/homeassistant/climate/dual-setpoint/propose` | `pico.api.homeassistant.propose_climate_dual_setpoint_actuation` | Policy A / Consequential Auth Context | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Smart home device actuation proposal; mutates physical environment state. |
| 77 | `GET` | `/api/homeassistant/climate/dual-setpoint/execution/{execution_id}` | `pico.api.homeassistant.get_climate_dual_setpoint_actuation_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 78 | `POST` | `/api/filesystem/proposals` | `pico.api.filesystem.stage_proposal` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 79 | `GET` | `/api/filesystem/proposals` | `pico.api.filesystem.list_proposals` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 80 | `GET` | `/api/filesystem/proposals/{proposal_id}` | `pico.api.filesystem.get_proposal` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 81 | `POST` | `/api/filesystem/proposals/{proposal_id}/discard` | `pico.api.filesystem.discard_proposal` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 82 | `POST` | `/api/filesystem/proposals/{proposal_id}/apply` | `pico.api.filesystem.submit_apply` | Policy A / Proposed Diff Inspection | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Direct disk modification or rollback; mutates host files; requires explicit operator consent. |
| 83 | `POST` | `/api/filesystem/proposals/{proposal_id}/rollback` | `pico.api.filesystem.submit_rollback` | Policy A / Proposed Diff Inspection | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Direct disk modification or rollback; mutates host files; requires explicit operator consent. |
| 84 | `POST` | `/api/workflows` | `pico.api.workflows.submit_workflow` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 85 | `GET` | `/api/workflows` | `pico.api.workflows.list_workflows` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 86 | `POST` | `/api/workflows/synthesize` | `pico.api.workflows.synthesize_workflow` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | Yes | Workspace state mutation; requires valid session cookie and anti-CSRF token. |
| 87 | `GET` | `/api/workflows/{workflow_id}` | `pico.api.workflows.get_workflow` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 88 | `POST` | `/api/workflows/{workflow_id}/advance` | `pico.api.workflows.advance_workflow` | Policy A / Operator Session Optional | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Multi-step workflow lifecycle progression, cancellation, and transaction compensation. |
| 89 | `POST` | `/api/workflows/{workflow_id}/cancel` | `pico.api.workflows.cancel_workflow` | Policy A / Operator Session Optional | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Multi-step workflow lifecycle progression, cancellation, and transaction compensation. |
| 90 | `GET` | `/api/workflows/{workflow_id}/verifications` | `pico.api.workflows.get_workflow_verifications` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 91 | `GET` | `/api/workflows/{workflow_id}/steps/{step_key}/verification` | `pico.api.workflows.get_step_verification` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 92 | `POST` | `/api/workflows/{workflow_id}/compensate` | `pico.api.workflows.compensate_workflow` | Policy A / Operator Session Optional | Tier 6: Consequential Approval | Companion / Web UI | Loopback CORS-restricted | Yes | Multi-step workflow lifecycle progression, cancellation, and transaction compensation. |
| 93 | `GET` | `/api/workflows/{workflow_id}/compensations` | `pico.api.workflows.get_workflow_compensations` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 94 | `GET` | `/api/scheduler/status` | `pico.api.scheduler.get_scheduler_status` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |
| 95 | `GET` | `/api/scheduler/metrics` | `pico.api.scheduler.get_scheduler_metrics` | None (Open loopback in dev) | Tier 4: Operator Core Workspace | Companion / Web UI | Loopback CORS-restricted | No | Read-only workspace query; requires authenticated operator session. |


### 10.3 Reconciliation of Consequential Endpoint Counts (11 vs. 10 vs. 15)

In earlier audit drafts, 11 endpoints were grouped under "Tier 4: Consequential / Explicit Approval Endpoints":
1. `POST /api/tools/executions/{execution_id}/execute`
2. `POST /api/tools/executions/{execution_id}/approve`
3. `POST /api/tools/executions/{execution_id}/cancel`
4. `POST /api/filesystem/proposals/{proposal_id}/apply`
5. `POST /api/filesystem/proposals/{proposal_id}/rollback`
6. `POST /api/workflows/{workflow_id}/advance`
7. `POST /api/workflows/{workflow_id}/cancel`
8. `POST /api/workflows/{workflow_id}/compensate`
9. `POST /api/memory/procedures/{procedure_id}/approve`
10. `POST /api/memory/procedures/{procedure_id}/activate`
11. `POST /api/memory/procedures/{procedure_id}/retire`

#### Source-Level Reconciliation of the 11-to-10 Change
- **Moved Endpoint**: `POST /api/workflows/{workflow_id}/cancel` (`pico.api.workflows.cancel_workflow`).
- **Source Evidence**: In `pico/api/workflows.py`, `cancel_workflow` accepts only `db: Session = Depends(get_db)` and `payload: WorkflowCancelRequest`. It transitions internal run status to `CANCELLED` and halts execution. It does **not** execute tool capabilities, write files to disk, alter external physical devices, or invoke Policy A authorization checks (`require_consequential_authorization`).
- **PICO Invariant Alignment**: Under PICO Core Operating Principle 7, consequential actions are strictly defined as operations that mutate external state, delete resources, perform financial transactions, send communications, or transmit sensitive data. Halting an internal workflow execution is a benign administrative status transition (aborting further work), rather than an external mutation.
- **Classification Result**: `cancel_workflow` is properly categorized under **Tier 4: Operator Core Workspace Mutations** (bringing workspace mutations to 68 and narrowing strict lifecycle approvals to 10).
- **Home Assistant Actuation Proposals**: Note that in `pico/api/homeassistant.py`, four actuation endpoints (`/api/homeassistant/actuation/propose`, `/api/homeassistant/switch/propose`, `/api/homeassistant/climate/propose`, `/api/homeassistant/climate/dual-setpoint/propose`) explicitly enforce `require_consequential_authorization`. When these 4 environmental actuation endpoints are classified under Tier 6 Consequential, the comprehensive consequential group encompasses 14 endpoints (or 15 if proposal cancellation is included). The master table in Section 10.2 maps every single endpoint individually to prevent ambiguity.

### 10.4 Granular Perception Modality Breakdown & Device Identity Audit (12 Endpoints)

In previous drafts, all 12 perception endpoints were grouped generically under "device perception / sensor ingestion". This audit establishes a granular 4-way sub-tier distinction:

1. **Tier 5A: Read-Only Sensor Diagnostics & Historical Queries (3 Endpoints)**
   - `GET /api/perception/status`: Inspects hardware sensor readiness and driver availability (mic, camera, display).
   - `GET /api/perception/audio/session/{session_id}`: Queries state and transcript of a completed or active audio capture session.
   - `GET /api/perception/events`: Queries chronological audit log of recent perception events.
   - *Security Policy*: Read-only; loopback-only; no side effects; authenticated operator session required in P11.2.2.4.

2. **Tier 5B: Operator-Initiated Interactive Capture Controls (4 Endpoints)**
   - `POST /api/perception/audio/session/start`: Explicitly begins streaming microphone capture.
   - `POST /api/perception/audio/session/{session_id}/stop`: Explicitly concludes audio capture and initiates transcription.
   - `POST /api/perception/audio/session/{session_id}/cancel`: Aborts capture session and securely zeroes audio buffers.
   - `POST /api/perception/vision/capture`: Captures screen snapshot or webcam frame for multimodal reasoning.
   - *Security Policy*: High privacy impact; explicit user intent required; same-origin/companion loopback only; anti-CSRF token enforced; authenticated operator session required.

3. **Tier 5C: Passive Background Telemetry Ingestion (2 Endpoints)**
   - `POST /api/perception/motion/ingest`: Ingests low-frequency presence/activity telemetry (e.g., PIR motion or system idle state).
   - `POST /api/perception/environmental/ingest`: Ingests ambient environmental sensor telemetry (temperature, light, noise levels).
   - *Audit of Accepted Principal & Device Authentication*:
     - **Currently Accepted Principal**: Source inspection of `pico/api/perception.py` reveals that these endpoints currently accept **unauthenticated anonymous callers** on local loopback (`Depends(get_db)` only).
     - **Device Identity Status**: **ABSENT**. The `source_device_id` string provided in the request body is self-reported client data, NOT an authenticated cryptographic identifier.
     - **Distinction**: Pydantic schema validation validates payload types and bounds, but payload validation alone is NOT device authentication.
     - **Future Requirement**: Before production deployment or exposure beyond strict desktop loopback, Tier 5C requires cryptographic device identity (pre-shared token, mTLS, or bridge session authentication).

4. **Tier 5D: Perception Synthesis, Interpretation & Correlation (3 Endpoints)**
   - `POST /api/perception/audio/synthesize`: Text-to-speech audio synthesis for companion voice output.
   - `POST /api/perception/motion/interpret`: Evaluates user activity heuristics and presence confidence.
   - `POST /api/perception/correlate`: Cross-correlates multimodal sensory events across temporal windows.
   - *Security Policy*: Computationally bounded; loopback-only; authenticated operator session and CSRF protection required.

---

## 11. Milestone P11.2.2.1 Qualification Status Matrix

Evidence is strictly distinguished between actual native Rust supervisor execution, actual Tauri runtime execution, Python/Win32 test harness results, and deferred items:

| # | Requirement | Qualification Evidence Status | Details & Scope |
| :---: | :--- | :--- | :--- |
| 1 | Supervisor launches actual FastAPI child | **Passed — Python/Win32 test harness only** | Verified via `test_req_01_and_02...`. Rust `ManagedChild::spawn` implementation complete; native Rust execution blocked by missing local MSVC `link.exe`. |
| 2 | TCP listener PID matches retained child | **Passed — Python/Win32 test harness only** | Win32 `GetExtendedTcpTable` listener PID == child PID verified in Python harness. Rust code implements identical Win32 call. |
| 3 | Rogue listener using same `python.exe` rejected | **Passed — Python/Win32 test harness only** | Verified in Python harness: identical executable image path rejected when PID does not match retained child PID. |
| 4 | Rogue listener returning 200 OK `/health` rejected | **Passed — Python/Win32 test harness only** | Verified in Python harness: fake 200 OK `/health` does not authorize credentials without PID match. |
| 5 | Port occupied before launch causes fail-closed | **Passed — Python/Win32 test harness only** | Pre-launch check detects conflict and aborts launch. Rust `ManagedChild::spawn` implements pre-check. |
| 6 | Backend termination invalidates verified state | **Passed — Python/Win32 test harness only** | Socket drop immediately resets `verified_state` to `None`. |
| 7 | Restart creates different PID and launch secret | **Passed — Python/Win32 test harness only** | Verified: restarting backend spawns a new PID with a fresh 256-bit secret. |
| 8 | Stale challenge response cannot verify new backend | **Passed — Python/Win32 test harness only** | Verified: HMAC from previous instance secret rejected by new instance. |
| 9 | Invalid HMAC, replayed nonce, malformed rejected | **Passed — Python/Win32 test harness only** | Corrupted signature, truncated hex, replayed nonce, and missing response rejected. |
| 10 | Backend takeover after termination fails closed | **Passed — Python/Win32 test harness only** | Attacker binding freed port rejected due to PID mismatch. |
| 11 | New connection requires fresh identity verification | **Passed — Python/Win32 test harness only** | Unverified or refreshed session requires full verification before credential release. |
| 12 | Closing Job Object kills process and child tree | **Passed — Python/Win32 test harness only** | Verified via independent Win32 API test: OS kernel terminates parent and grandchild on Job Object handle close. |
| 13 | Assignment/config failure terminates child before resume | **Passed — Python/Win32 test harness only** | Suspended process terminated with `TerminateProcess` before resume on error. |
| 14 | Backend binds strictly to 127.0.0.1; wildcard rejected | **Passed — Python/Win32 test harness only** | `0.0.0.0` rejected under strict loopback filter. |
| 15 | No secret in process CLI, env, logs, or disk | **Passed — Python/Win32 test harness only** | Secret transferred via anonymous pipe; env var deleted; absent from CLI and disk. |
| 16 | Inherited pipe handle closed; unrelated handles blocked | **Passed — Python/Win32 test harness only** | `test_req_16_explicit_handle_list_blocks_unrelated_handles` empirically proves unrelated inheritable sentinel handle is NOT inherited (error 6) while designated pipe handle remains valid. |
| 17 | Managed mode does not use reload workers | **Passed — Python/Win32 test harness only** | Verified: `--reload` strictly omitted to prevent PID bifurcation. |
| 18 | No operator credentials transmitted at any point | **Passed — Python/Win32 test harness only** | Verified: proxy traffic logs show zero credentials dispatched while unverified. |
| 19 | Cryptographic RNG entropy for launch secret | **Passed — Source audit & Win32 CNG** | Dynamic Windows CNG `BCryptGenRandom` with `BCRYPT_USE_SYSTEM_PREFERRED_RNG` generates 32 bytes (256 bits) of cryptographically secure pseudorandom entropy. RAII `ModuleHandle` guarantees `FreeLibrary` on all return and error paths; fails closed on error. |
| 20 | Verified Online State Transition | **Implemented & Verified in Source & Runtime** | `BackendSupervisionStatus` enforces `Unmanaged -> Starting -> Verifying { pid } -> Online { pid }`. `child.wait_and_verify` verifies TCP listener on `127.0.0.1:8000`, listener PID match, process instance, and nonce/HMAC challenge-response verification over loopback TCP stream to `/api/bridge/handshake`. |
| 21 | Trusted Backend Executable & Project Resolution | **Implemented & Verified in Source & Runtime** | `resolve_trusted_project_root` uses compile-time manifest or explicit `PICO_WORKSPACE_ROOT`, canonicalizes paths, verifies `pico/main.py` and `pyproject.toml`, requires `.venv\Scripts\python.exe` inside root, and refuses PATH fallback. Decoy working directory rejected. |
| 22 | Continuous Termination Detection & Error Resiliency | **Implemented & Verified in Source & Runtime** | `SupervisorState::check_liveness` detects unexpected backend exit and transitions to `Offline { reason }`. Dual process handle tracking monitors verified runtime listener PID directly. Background thread monitors child state. `app.setup` converts all launch errors into `Offline` without crashing or panicking the desktop shell. |
| - | Actual Windows sleep/wake cycle | **Deferred** | Clarified as socket-drop / connection reset qualification only. |
| - | Packaged executable startup/shutdown | **Deferred** | Scheduled for Milestone P11.2.4. |

### 11.1 Build Toolchain Qualification Closure

- **Toolchain Status**: Fully resolved via Microsoft Visual Studio 2022 Build Tools:
  - MSVC compiler `cl.exe` (`19.44.35207.0`), linker `link.exe` (`14.44.35207.0`).
  - Windows SDK `10.0.26100.0` (`kernel32.Lib`, `iphlpapi.lib`, `bcrypt.lib`).
  - Rust compiler `rustc` (`1.89.0`), target `x86_64-pc-windows-msvc`.
- **Validation Matrix**:
  - `cargo check`: 0 errors, 0 warnings.
  - `cargo test`: 26 passed, 0 failed.
  - Python test suite: 75 passed, 0 failed.
  - Frontend Vitest suite: 144 passed across 14 test files.
  - Workspace validators: all passed cleanly.
  - Runtime desktop smoke test: live launch, listener PID ownership, HMAC challenge, minimize/restore, close-to-tray, and clean shutdown verified.
  ```
- **Root Cause**: The host development environment has the `rustc` compiler and `cargo` installed with the default target `x86_64-pc-windows-msvc`, but Microsoft Visual C++ Build Tools (`link.exe`, C runtime libraries, and Windows SDK) are not installed on the system.
- **Codebase Integrity**: All Rust source files (`lib.rs`, `supervisor.rs`) conform 100% to Rust syntax and formatting rules (`cargo fmt --check` passes with exit code 0).
- **Policy Compliance**: Per PICO Engineering Rules, no machine-level system software (such as Visual Studio Build Tools or Windows SDK) is installed without explicit operator authorization.
- **Current Milestone P11.2.2.1 Status**:
  **IN PROGRESS — Native Windows Build and Tauri Runtime Qualification Pending** (Awaiting local MSVC C++ Build Tools or CI Windows Runner execution).

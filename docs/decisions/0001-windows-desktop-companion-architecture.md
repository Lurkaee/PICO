# ADR 0001: Windows Desktop Companion Shell Technology & Secure Local Bridge Architecture

- **Status**: Accepted (Canonical Architecture for Milestone P11.2)
- **Date**: 2026-11-05
- **Deciders**: PICO Core Architecture Team
- **Milestone Reference**: Milestone P11.2 (P11.2.0 Architectural Decision Record)
- **Related Documents**:
  - [`AGENTS.md`](../../AGENTS.md) — Operating rules, hardware invariants, and engineering principles
  - [`docs/architecture.md`](../architecture.md) — System architecture, subsystem boundaries, data flows
  - [`docs/implementation-plan.md`](../implementation-plan.md) — Phased delivery plan and acceptance criteria
  - [`docs/api-contract.md`](../api-contract.md) — Authoritative REST and SSE API specifications

---

## 1. Context & Problem Statement

PICO (Personal Intelligence Coordinator & Operator) is a local-first, privacy-first personal intelligence system designed to autonomously coordinate knowledge, context, memory, multi-step tasks, and verifiable tool execution. In Phase 10, PICO delivered a polished browser-based desktop command centre (`frontend/` React/TypeScript/Vite SPA) providing streaming chat, interactive workflow DAG timelines, four-part memory inspection, and operator approval management.

Milestone P11 expands PICO's physical and interaction presence onto the Windows desktop through a lightweight, expressive, and always-accessible desktop companion shell. As defined in [`AGENTS.md`](../../AGENTS.md) (Invariant 12) and [`docs/architecture.md`](../architecture.md):

> **Desktop Companion is an Interaction Surface, Not an Independent Assistant**
> The Windows desktop companion is strictly an expressive presentation and perception-input surface for the existing PICO personal intelligence system. It must never implement an independent LLM reasoning loop, duplicate memory stores, private tool execution paths, or bypass Policy A authorization. All companion actions and perceptions route through PICO Core APIs over authenticated local loopback connections.

We must formally select the desktop framework technology, establish the secure loopback bridge architecture, define the visual state machine for the animated companion presence, enforce non-negotiable memory budgets, and establish strict milestone boundaries before implementing companion code.

---

## 2. Decision Drivers & Constraints

The desktop companion architecture must satisfy six non-negotiable constraints:

1. **Hardware Memory Budget (<100 MB RAM Idle for Entire Companion Process Tree)**:
   - Target Host: Intel Core i5-13420H, 16 GB DDR5 RAM, NVIDIA GeForce RTX 3050 Laptop GPU (4 GB VRAM), Windows 11.
   - Host RAM is heavily allocated between the Windows OS (~4–6 GB), local Ollama inference (`qwen3.5:2b` resident footprint ~2–3 GB RAM/VRAM), background PICO FastAPI services, and user productivity applications.
   - The entire companion process tree (native Rust host process + WebView2 browser process + WebView2 renderer processes + crashpad handler + utility processes) must consume **strictly less than 100 MB RAM** at idle.
   - **Crucial Distinction**: All memory figures cited during framework evaluation are architectural projections and runtime estimates based on published framework benchmarks; they are NOT yet empirical measurements of the PICO desktop companion. Actual qualification of the complete companion process tree on target hardware is mandatory in P11.2.4 before milestone completion.

2. **Windowing, Transparency & Presentation Ergonomics**:
   - Compact, frameless, floating desktop widget with reliable transparency or styled translucent background.
   - Smooth mouse-drag repositioning without visual stutter or rendering artifacts.
   - Reliable minimize-to-system-tray and context-menu controls.
   - Compact flyout conversational interface providing quick chat, verification status, and one-click access to pending approvals.

3. **Maximum UI Code & Test Reuse**:
   - PICO already maintains a comprehensive React 18, TypeScript, Tailwind CSS, and Vite frontend with **139 verified Vitest tests** covering connectivity, SSE streaming, chat, voice, approvals, workflow DAG visualization, and synthesis reviews.
   - The desktop shell must reuse these existing components, types, and API client abstractions without fracturing the codebase into a duplicate UI implementation.

4. **Security, Least Privilege & Policy A Integration**:
   - The desktop shell must communicate with FastAPI exclusively via loopback (`127.0.0.1` / `localhost`).
   - The companion must authenticate via Policy A verified operator sessions without exposing secrets or weakening existing web security.
   - The bridge must verify backend identity before transmitting any credentials, preventing credential replay to rogue loopback listeners.
   - The companion frontend and native shell are untrusted presentation layers: zero direct capability execution, zero embedded API secrets, and zero access to physical actuation tokens (`APPROVAL_SECRET_TOKEN`).

5. **Visual State Truthfulness**:
   - The companion's expressive presence (two-eye animated face) must mirror **authoritative backend state** (Idle, Listening, Thinking, Speaking, Executing, Awaiting Approval, Offline, Error).
   - Absolute prohibition on fabricated animations (e.g. claiming PICO is executing or thinking when no backend lease is active).
   - All presence states must be derived from verified backend evidence, not client-side speculation.

6. **Scope Discipline & Milestone Independence**:
   - P11.2 must deliver only the desktop companion shell, system tray, secure bridge, and compact review UI.
   - Local Whisper STT (P11.3), local TTS (P11.3), wake-word detection (P11.3), voice-to-action execution (P11.4), and screen understanding (P11.5) must remain cleanly decoupled.

---

## 3. Technology Evaluation Matrix

We systematically evaluated four candidate Windows desktop technologies against the six decision drivers. All memory figures represent idle baseline process-tree architectural estimates based on published framework runtime profiles and benchmarks, not pre-claimed empirical measurements:

| Evaluation Criterion | Option 1: Tauri 2 + Rust | Option 2: C# / .NET + WebView2 | Option 3: PySide6 (Qt for Python) | Option 4: Electron |
| :--- | :--- | :--- | :--- | :--- |
| **Idle Memory Footprint** | **Estimated ~30–60 MB RAM** (Architectural projection: native Rust shell + pre-installed Windows 11 WebView2; qualification pending actual Windows 11 measurements in P11.2.4) | **Estimated ~70–110 MB RAM** (Architectural estimate: .NET CLR/GC runtime working set baseline + WebView2 process tree) | **Estimated ~150–250 MB RAM** (Architectural estimate: QtWebEngine embeds full Chromium framework; QML alternative requires total rewrite) | **Estimated ~180–300+ MB RAM** (Architectural estimate: Bundles dedicated Chromium browser + Node.js runtime processes) |
| **Meets <100 MB Budget?** | **PROJECTED YES** (Estimated headroom; qualification pending empirical Windows 11 measurement in P11.2.4) | **MARGINAL / RISKY** (Estimated baseline approaches or exceeds 100 MB before application state) | **NO** (Estimated baseline exceeds 100 MB budget) | **NO** (Estimated baseline significantly violates 100 MB budget) |
| **UI Code Reuse** | **100%** (Embeds existing React 18 + Vite frontend directly) | **100%** (Embeds existing React build via WebView2) | **0–20%** (Requires QML rewrite or heavyweight Qt WebEngine) | **100%** (Embeds React frontend in Chromium renderer) |
| **Windows 11 Integration** | **Native** (Tauri system-tray plugin, frameless transparency, Windows API bindings) | **Native** (First-class Windows Forms / WPF / WinUI integration) | **Native** (QSystemTrayIcon, frameless windows, Qt platform abstraction) | **Good** (Tray and frameless windows, but known transparent window drag issues) |
| **Security & Sandboxing** | **High** (Fine-grained capability allowlist; zero native privilege leakage) | **Medium** (Full .NET process permissions; requires manual isolation) | **Medium** (Direct Python interpreter access; no web sandbox isolation) | **Medium-Low** (Node.js integration risks; large attack surface) |
| **Distribution / Installer Size** | **~10–15 MB installer** (WebView2 is pre-installed on Windows 11) | **~50–80 MB installer** (Self-contained or framework-dependent .NET) | **~60–100 MB installer** (Bundled Python runtime and Qt shared libraries) | **~120–180 MB installer** (Full Chromium and Node.js bundled) |
| **Developer Ergonomics** | **High** (Vite HMR dev server, unified TypeScript models, small Rust layer) | **Medium** (Requires Visual Studio / .NET SDK alongside npm/Vite) | **Medium** (Dual Python backend + Python GUI; separate from React UI) | **High** (Standard web development tooling, rapid prototyping) |

> [!NOTE]
> Comparative framework memory figures are architectural estimates derived from standard baseline distributions and runtime overheads:
> - Electron: Chromium renderer process + browser main process + Node runtime overhead typically establish an idle baseline of 180–300 MB for simple desktop shells.
> - PySide6 with QtWebEngine: Embeds a full Chromium content module, incurring 150–250 MB idle baseline; rewriting in pure QML would eliminate Chromium overhead but forfeit 100% of PICO's verified React components and tests.
> - C# / .NET + WebView2: .NET CoreCLR GC heap baseline (~30–50 MB) plus WebView2 runtime processes (~40–60 MB) results in an estimated 70–110 MB working set.
> - Tauri 2 + Rust: Reuses the system-level Windows 11 Evergreen WebView2 runtime without bundling Chromium or Node, projecting ~30–60 MB idle across all processes. Final qualification requires empirical measurement in P11.2.4.

---

## 4. Architectural Evaluation of Alternatives

### Option 1: Tauri 2 + Rust (Recommended & Selected)
- **Strengths**:
  - Leverages Windows 11's pre-installed `Microsoft.Web.WebView2` runtime, eliminating the need to bundle a separate Chromium browser engine.
  - The native shell is written in Rust, compiling to a compact binary (<15 MB) that has an estimated baseline memory footprint of ~15–25 MB for the native host process and ~25–35 MB for the WebView2 renderer, totaling an **estimated ~40–60 MB idle RAM**. Note: This is an architectural projection based on published WebView2 / Tauri 2 runtime profiles, NOT an empirical measurement of the PICO desktop companion. Actual qualification of the complete companion process tree on target hardware is mandatory in P11.2.4 before milestone completion.
  - Out-of-the-box support for system tray icons, context menus, frameless transparent windows, and window drag ergonomics.
  - Native permissions system (Tauri Capabilities) enforces zero IPC privilege exposure to the web view.
- **Trade-offs**:
  - Requires the Rust toolchain (`cargo`, `rustc`) for building the desktop shell binary.
  - Requires careful window transparency configuration under Windows 11 (DWM composition, `window_effects` like Mica/Acrylic or transparent background).
  - Cross-origin authentication transport between the packaged Tauri origin (`http://tauri.localhost` on Windows) and FastAPI loopback (`http://127.0.0.1:8000`) must be explicitly qualified and implemented in P11.2.2.

### Option 2: C# / .NET + WebView2
- **Strengths**:
  - Excellent integration with Windows native APIs, WPF/WinUI window management, and Windows notification center.
  - Uses the same Windows 11 WebView2 component as Tauri.
- **Trade-offs**:
  - Estimated idle working set for a .NET desktop application hosting WebView2 typically ranges from 70–110 MB (accounting for CoreCLR GC heaps and WebView2 runtime processes), leaving dangerously narrow headroom against the 100 MB budget.
  - Introduces a third language and runtime (.NET) alongside Python (PICO Core) and TypeScript (Frontend), increasing maintenance overhead and build pipeline complexity.

### Option 3: PySide6 (Qt 6 for Python)
- **Strengths**:
  - Python-native; could theoretically integrate with PICO's virtual environment.
  - Mature system tray and window styling capabilities.
- **Trade-offs**:
  - Hosting web content requires `QtWebEngine`, which embeds an entire private Chromium build whose baseline memory is estimated at 150–250+ MB RAM.
  - Alternatively, rewriting the UI in QML or Qt Widgets would abandon PICO's verified React frontend (139 tests, Tailwind styling, Markdown rendering, SVG workflow DAGs), requiring hundreds of hours of duplicate UI development.

### Option 4: Electron
- **Strengths**:
  - Complete React compatibility and broad desktop ecosystem.
- **Trade-offs**:
  - Ships an independent Chromium browser and Node.js runtime, adding 150+ MB to package size and consuming an **estimated 180–300+ MB RAM idle** based on standard Electron desktop applications. On our 16 GB hardware budget running concurrent local LLM inference, this overhead violates PICO's hardware invariants.
  - Transparent, click-through, frameless windows have well-documented visual glitching and resizing constraints on Windows DWM.

---

## 5. Decision Outcome

**Decision**: Select **Option 1: Tauri 2 + Rust** as the canonical desktop framework for PICO Milestone P11.2.

### Architectural Blueprint:
```
┌────────────────────────────────────────────────────────────────────────┐
│                      Windows 11 Desktop Workspace                      │
│                                                                        │
│  ┌───────────────────────┐              ┌───────────────────────────┐  │
│  │   System Tray Icon    │              │  Floating Companion Shell │  │
│  │   - Status Badge      │              │  (Frameless, Draggable)   │  │
│  │   - Open Full UI      │              │  ┌─────────────────────┐  │  │
│  │   - Pause Perception  │              │  │ Expressive Eyes UI  │  │  │
│  │   - Settings          │              │  └─────────────────────┘  │  │
│  │   - Quit              │              │  ┌─────────────────────┐  │  │
│  └───────────┬───────────┘              │  │ Compact Chat Panel  │  │  │
│              │                          │  │ & Workflow Review   │  │  │
│              │                          │  └─────────────────────┘  │  │
│              └──────────────┐           └─────────────┬─────────────┘  │
│                             │                         │                │
│             Tauri 2 Rust Host Process (Est. <20 MB)   │                │
│             Windows WebView2 Renderer (Est. <40 MB)   │                │
│             Total Process Tree Target: <100 MB Idle   │                │
│             (Empirical measurement in P11.2.4)        │                │
└─────────────────────────────┼─────────────────────────┼────────────────┘
                              │                         │
                              │ Authenticated Loopback  │
                              │ HTTP / SSE Transports   │
                              │ (127.0.0.1:8000 only)   │
                              ▼                         ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        PICO Core (FastAPI Backend)                     │
│                                                                        │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │  Policy A & Operator Security Barrier                          │   │
│   │  - Backend identity verification before credential issuance    │   │
│   │  - Validates operator session on companion bridge requests     │   │
│   │  - Validates `X-CSRF-Token` header on ALL mutating requests    │   │
│   │  - Fails closed (401/403) for unauthenticated / remote calls   │   │
│   └────────────────────────────────┬───────────────────────────────┘   │
│                                    │                                   │
│   ┌────────────────────────────────┴───────────────────────────────┐   │
│   │  Core Services: Memory · Model Gateway · Context Compiler      │   │
│   │  Routing · P9 Workflow Engine · Execution Engine · Verifiers   │   │
│   └────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Secure Local Bridge Architecture & Implementation Contract

A rigorous audit of the current PICO codebase reveals that integrating a separately packaged desktop shell requires specific architectural bridge contracts. This section defines the current implementation reality, the cross-origin challenge, backend identity verification requirements, companion operator-session pairing lifecycle, and unresolved authentication transport engineering alternatives for Milestone P11.2.2.

### 6.1 Audit of Existing Security Baseline

1. **CORS Configuration (`pico/main.py`)**:
   - `CORSMiddleware` currently permits only Vite development server origins:
     `allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]`.
   - Packaged desktop origins (`http://tauri.localhost` on Windows) and Tauri dev origins (`http://localhost:1420`, `http://127.0.0.1:1420`) are not yet permitted.
2. **Operator Session Origin & Host Validation (`pico/services/operator_session.py`)**:
   - `validate_origin_and_host()` enforces that `Host` and `Origin` / `Referer` match `ALLOWED_HOSTS` (`localhost`, `127.0.0.1`, `testserver`).
   - Hostnames outside this tuple (such as `tauri.localhost`) are currently rejected as untrusted.
3. **Session Cookie Security (`pico/api/operator.py`)**:
   - The session cookie `pico_operator_session` is set with `httponly=True`, `samesite="strict"`, `path="/api"`.
   - `SameSite="strict"` instructs browsers and WebViews to withhold the cookie on any cross-site or cross-origin HTTP request.
4. **Frontend API Client (`frontend/src/api/client.ts`)**:
   - The centralized `request<T>()` helper specifies `credentials: 'same-origin'`.
   - Cross-origin requests from a webview running on `http://tauri.localhost` to `http://127.0.0.1:8000` will not transmit cookies under `same-origin`.
5. **Endpoint Authorization Scope**:
   - Today, `require_operator_session` and `require_consequential_authorization` protect Policy A approval endpoints (`/api/operator/*`, `/api/workflows/{id}/advance`, `/api/workflows/{id}/compensate`).
   - Endpoints such as `/api/chat/stream`, `/api/conversations`, and `/api/workflows/synthesize` are loopback-accessible but do not currently enforce the operator-session dependency.

### 6.2 The Tauri 2 Windows Cross-Origin Reality

On Windows 11, Tauri 2 production frontend assets are loaded via the WebView2 custom URI scheme as `http://tauri.localhost` (or `http://localhost:1420` in development). The FastAPI backend listens on `http://127.0.0.1:8000`.

This creates three immediate transport barriers:
1. **CORS Policy Failure**: FastAPI will reject preflight `OPTIONS` and cross-origin requests from `http://tauri.localhost`.
2. **Host/Origin Validation Failure**: `OperatorSessionService` will treat `http://tauri.localhost` as an untrusted origin and fail closed.
3. **Cookie Dropping**: `credentials: 'same-origin'` in `frontend/src/api/client.ts` prevents cookies from being attached. Furthermore, `SameSite="strict"` on the cookie prevents transmission across differing origins even if credentials are set to `include`.

### 6.3 Backend Identity Verification (Mandatory Precondition)

Before transmitting any operator credentials (`pico_operator_session` cookie, session headers, CSRF tokens, or pairing challenge codes) to `http://127.0.0.1:8000`, the companion must establish authoritative proof that the loopback listener is genuine PICO Core:

1. **Threat Model & Port Occupation**:
   - On Windows, loopback port `8000` is shared across local processes. If PICO Core is stopped, crashed, restarting, or slow to bind, another local process or service could bind to `127.0.0.1:8000`.
   - If the companion transmits `pico_operator_session` or pairing codes blindly upon seeing an open port, the rogue listener captures valid operator credentials and can replay them to PICO Core once it restarts.
2. **Non-Sufficiency of `/health`**:
   - A standard `200 OK` response from `/api/health` returning `{"status": "healthy"}` is trivially emulated by any rogue HTTP server and does **not** constitute proof of backend identity.
3. **Strict Verification Invariants**:
   - **Pre-Credential Verification**: The companion shell must positively verify the authoritative identity of the PICO Core backend before any session cookie, session header, CSRF token, or pairing code is transmitted.
   - **Fail Closed**: If backend identity cannot be established, the companion immediately fails closed:
     - Suppresses all credential transmission.
     - Transitions visual state to `offline` / `unverified`.
     - Suspends outgoing user queries and commands.
   - **Mandatory Re-Verification**: Backend identity verification must be re-executed:
     - On initial companion startup before first API communication.
     - Immediately following any network disconnection, socket drop, or HTTP connection error.
     - Following backend process restarts or machine sleep/wake cycles.
     - After any timeout or ambiguous connection failure.
4. **Implementation Contract for P11.2.2**:
   - P11.2.2 must define and implement an authoritative local trust anchor without relying on circular network assumptions.
   - Candidate mechanisms to be qualified in P11.2.2 include:
     - A local runtime instance token or nonce generated by PICO Core on startup, written to protected per-user local runtime storage (e.g. SQLite database or protected runtime file `%APPDATA%\PICO\runtime\instance.json`) accessible exclusively by the local operator user process tree, validated by the Tauri native Rust layer prior to socket communication.
     - A mutual local challenge-response handshake over loopback.
   - In P11.2.0, the invariant is formally non-negotiable: **no credentials may be emitted to an unverified listener**.

### 6.4 Companion Pairing Lifecycle & Credential State Machine

The companion participates in the Policy A operator pairing lifecycle through a secure, auditable state machine:

1. **Challenge Generation**:
   - The operator initiates pairing via the PICO Core CLI (`python -m pico.cli.operator pair`) or via the paired browser command centre.
   - PICO Core generates a cryptographically random 6-digit one-time code (`secrets.randbelow(900000) + 100000`), persisted as a SHA-256 salted hash in `OperatorPairingChallenge` with a 300-second TTL and max 5 attempts.
2. **Companion Pairing Presentation & Input**:
   - When the companion detects an unpaired state (or missing session), it renders a dedicated pairing modal prompting the operator to enter the 6-digit code.
   - The companion does NOT auto-generate or fabricate pairing challenges; the human operator acts as the authoritative physical trust anchor.
3. **Submission & Session Creation**:
   - Once backend identity is verified, the companion issues `POST /api/operator/pair` over loopback containing `{"code": "<6-digit-code>"}`.
   - PICO Core verifies the code against the database challenge, marks the challenge consumed (`consumed_at = now`), creates an active `OperatorSessionModel`, and returns:
     ```json
     {
       "status": "paired",
       "csrf_token": "<urlsafe-token>",
       "expires_at": "<utc-iso-timestamp>",
       "approver": "local_operator"
     }
     ```
   - In the same response, the backend issues the `Set-Cookie: pico_operator_session=<session_id>; HttpOnly; Path=/api; Max-Age=3600; ...` header.
4. **Credential Isolation & Scoping**:
   - **Session Cookie**: Scoped strictly to `Path=/api`. Stored in the native WebView2 cookie jar (isolated to the companion's private user data profile directory, inaccessible to external browsers or untrusted scripts).
   - **CSRF Token**: Stored strictly in client memory (`setCsrfToken(csrf_token)` in `client.ts`). Never persisted to `localStorage`, `sessionStorage`, or disk files.
   - **Zero Secret Exposure**: The `APPROVAL_SECRET_TOKEN` is never sent to or stored in the companion. The companion holds only session-scoped credentials.
5. **Session Expiry & Lifecycle Monitoring**:
   - Sessions expire authoritatively on the backend after 3600 seconds.
   - The companion periodically validates session state via `GET /api/operator/session`.
   - If the backend returns `paired: false`, 401 Unauthorized, or 403 Forbidden, the companion immediately transitions to `offline` / `unpaired`, purges the in-memory CSRF token, and prompts the operator to re-pair.
6. **Explicit Logout & Revocation**:
   - Initiating logout (`POST /api/operator/logout`) invalidates the session record in SQLite (`is_active = False`), issues a cookie deletion header (`Max-Age=0`), and purges client memory.
7. **Reconnection & Crash Recovery**:
   - Upon connection loss, the companion halts all requests.
   - Upon socket re-establishment, the companion re-verifies backend identity first.
   - Once identity is verified, the companion issues `GET /api/operator/session` to confirm whether the existing session survived the interruption. If valid, normal operation resumes; if invalid, it prompts for pairing.
8. **Browser Command Centre Compatibility**:
   - The existing browser frontend (`http://localhost:5173`) remains 100% compatible and unchanged, using standard same-origin pairing.

### 6.5 Re-Evaluation of Authentication Transport Alternatives

In prior discussions, `SameSite=Lax + credentials: include` was proposed as a potential cross-origin bridge solution. A rigorous security review reveals this assumption is flawed:
- **Flaw in `SameSite=Lax` for Subresource Mutations**: Under the IETF 6265bis and W3C Fetch specifications, `SameSite=Lax` cookies are explicitly withheld on cross-site subresource requests. While top-level browser navigations using safe HTTP methods (GET) include Lax cookies, cross-origin `fetch()` requests—especially mutating `POST`, `PUT`, `DELETE` operations (such as `/api/operator/pair`, `/api/chat/stream`, `/api/workflows/...`)—are classified as cross-site subresources and will **not** transmit `SameSite=Lax` cookies.
- **Flaw in `SameSite=None`**: Setting `SameSite="none"` requires the `Secure` attribute, which mandates HTTPS. Setting up self-signed HTTPS on `127.0.0.1` introduces root CA installation requirements or certificate rejection hurdles in WebView2 on Windows.

Therefore, the authentication transport cannot rely on a presumed `SameSite=Lax` cross-origin cookie flow. Milestone P11.2.2 must qualify one of the following architectural alternatives through empirical verification on Windows 11:

1. **Alternative 1: Tauri Native Loopback Proxy (Recommended Architecture)**:
   - The Tauri Rust host acts as a local loopback proxy client.
   - The WebView makes requests to a custom Tauri protocol (`tauri://` or native IPC), or the Rust host acts as a local proxy.
   - The native Rust client (e.g. using `reqwest`) executes direct loopback HTTP calls to FastAPI at `http://127.0.0.1:8000`.
   - *Advantages*:
     - Native Rust HTTP clients are not subject to browser-level CORS preflight, origin mismatch, or cookie SameSite restrictions.
     - Backend identity verification (nonce/runtime challenge) is performed natively in Rust before any HTTP connection is opened.
     - Session cookies and CSRF tokens are managed in secure Rust process memory, completely eliminating cross-origin cookie dropping.
     - WebView2 JavaScript never directly touches raw network sockets.
2. **Alternative 2: Same-Site Virtual Host Mapping**:
   - Configuring WebView2's `SetVirtualHostNameToFolderMapping` alongside a local loopback reverse proxy or host mapping (e.g. mapping `pico.localhost` to both static assets and API routes).
   - *Advantages*: Renders requests same-site, allowing `SameSite=Strict` cookies to operate normally.
   - *Challenges*: Requires proxy routing in the native shell or port multiplexing.
3. **Alternative 3: Authenticated Loopback Header Protocol (`X-Pico-Operator-Session`)**:
   - The backend accepts the session identifier via an explicit request header (`X-Pico-Operator-Session`) on loopback connections, accompanied by mandatory CSRF token verification and backend identity verification.
   - *Advantages*: Eliminates reliance on browser cookie transmission mechanisms entirely while preserving constant-time session lookup and CSRF defense.
   - *Challenges*: Requires extending backend session extraction dependencies in `pico/api/operator.py`.

**Decision Status**: In Milestone P11.2.0, the architectural requirements (backend identity verification, pairing lifecycle, zero secret leakage, loopback binding) are non-negotiable. The exact transport implementation (Alternative 1, 2, or 3) is formally documented as an **unresolved engineering qualification task** that must be empirically proven on Windows 11 in Milestone P11.2.2 prior to code implementation.

### 6.6 Tauri IPC Isolation & Minimal Capability Surface

- The Rust shell exposes **zero custom execution commands** (`invoke(...)`) for system mutation or shell execution.
- Tauri native APIs are limited strictly to window management (drag, minimize, hide, show, reposition) and system tray event routing.
- The Tauri security capabilities configuration restricts permissions to window controls only.

---

## 7. Expressive State Machine & Authoritative Visual Identity

The companion's primary visual presence is an expressive two-eye face widget displaying eight authoritative states:

| Visual State | Visual Representation | Grounding Backend Evidence | Invariant / Anti-Hallucination Rule |
| :--- | :--- | :--- | :--- |
| **`idle`** | Calm ambient scanning, periodic natural blinking | `/api/health` reports healthy, operator session active, zero active scheduler leases, zero running workflow steps | No movement when backend is unreachable or unpaired |
| **`listening`** | Vibrant cyan pulse, audio waveform oscillation | Audio capture session active on local microphone (`AudioCaptureState.CAPTURING`) | **Prohibited** unless microphone hardware capture is actively streaming |
| **`thinking`** | Concentric rotating purple/blue rings | Active Model Gateway inference lease in progress (scheduler lock acquired or SSE `meta` active without `done`) | **Prohibited** during idle network or cached data delivery |
| **`speaking`** | Rhythmic acoustic mouth/eye dilation | Audio output playback actively rendering through system speakers (local TTS in P11.3; browser speech in P11.2) | **Prohibited** if audio output is muted, finished, or absent |
| **`executing`** | Focused scanning cadence, directional progress indicator | Workflow step execution in flight (`StepState.RUNNING`) or tool capability actively executing in `ToolExecutionEngine` | **Prohibited** during planning or pending approval states |
| **`awaiting_approval`** | Attention-directing pulsing amber glow | One or more execution tickets pending at Policy A approval gate (`ApprovalRequest.PENDING`) | Must display until operator approves or rejects the ticket |
| **`offline`** | Calm dim slate/gray indicator, sleepy eyes | Loopback backend unreachable, network disconnected, or session expired | Must truthfully signal disconnection; no simulated interactivity |
| **`error`** | Distinct red alert badge, furrowed indicator | Subsystem error, failed gate, or hardware exception | Cleared only when error condition resolves or is acknowledged |

### State Synchronization Contract (Specification for P11.2.3)

- **Audit of Existing SSE Stream**: The existing chat stream (`pico/api/chat.py`) implements SSE with four specific turn-level event types: `meta`, `token`, `done`, and `error`. It is **not** a general-purpose presence or system-status stream.
- **Contract Requirement for P11.2.3**: Milestone P11.2.3 must establish the authoritative state synchronization mechanism to drive these eight states.
- Every state transition must be backed by verified server-side evidence:
  - Polling `/api/health` and `/api/operator/session` for `offline` and `idle` states.
  - Polling `/api/scheduler/status` for scheduler lease state (`current_active_local`) and tracking chat SSE events (`meta` through `done`) for `thinking`.
  - Polling or streaming approval inbox status for `awaiting_approval`.
  - Tracking workflow execution step state for `executing`.
- **Absolute Rule**: Zero simulated or fabricated animations. The companion must never animate "thinking" or "listening" without authoritative server-side evidence.

---

## 8. Milestone Boundaries & Decoupled Architecture

To ensure incremental, verifiable delivery and prevent scope creep, clear boundaries are established between P11.2 and subsequent milestones:

### In Scope for P11.2 (Companion Shell & Secure Bridge):
- Minimal floating frameless window with drag positioning and styled transparent background.
- System tray icon with status badge and context menu (Status, Open Full UI, Pause Perception, Settings, Quit).
- Loopback HTTP/SSE bridge client authenticating via Policy A paired operator session and session-bound CSRF token.
- Authoritative eight-state expressive eye component and state synchronization engine.
- Compact conversational flyout panel embedding existing chat message stream, workflow review proposal card, and approval inbox.
- Windows 11 packaging, lifecycle management, and empirical memory measurement (<100 MB RAM idle for complete process tree).

### Explicitly Out of Scope for P11.2 (Deferred to Canonical Milestones):
- **Embedded Whisper STT Runtime**: Deferred to **P11.3**. P11.2 reuses existing browser/client-side audio capture and mock perception adapters.
- **Local Neural / SAPI TTS Runtime**: Deferred to **P11.3**.
- **On-Device Wake-Word Engine ("Hey PICO")**: Deferred to **P11.3**. P11.2 uses explicit click or hotkey activation.
- **Voice-to-Action Autonomous Execution**: Deferred to **P11.4**.
- **Contextual Screen Understanding & OCR Capture**: Deferred to **P11.5**.
- **Environmental Motion & Sensor Ingestion**: Governed by P6 hardware integration.

---

## 9. Hardware Qualification Strategy

In accordance with PICO's core operating principles:
1. **P11.1 Status**: The software implementation of P11.1 (Conversational Task Decomposition & Workflow Synthesis) is **merged into `main`**, fully tested (72 synthesis tests, 108 workflow tests, 795 pytest suite, 139 frontend tests), and **strictly gated behind hardware qualification** (`WORKFLOW_SYNTHESIS_ENABLED = False`).
2. **Post-P11.2 Clean Qualification**: Workstation hardware qualification for P11.1 will be executed against a clean post-P11.2 `main` commit on actual target hardware (Intel Core i5-13420H, 16 GB RAM, RTX 3050 Laptop GPU 4 GB VRAM) using real local Ollama (`qwen3.5:2b`), measuring genuine RAM/VRAM telemetry across all six benchmark prompts (`WS-VALID-01` through `WS-ADVERSARIAL-06`), clearing all ten mandatory quality gates.
3. This prevents qualifying an intermediate commit that would be invalidated upon subsequent feature merges, while preserving the fail-closed runtime provenance invariants implemented in PR #50.

---

## 10. Phased Implementation Sequence (P11.2.0 – P11.2.4)

Development will proceed in small, reviewable vertical slices:

- **P11.2.0 — Architecture Decision Record & Documentation Reconciliation** *(Current Slice)*:
  - Complete this ADR.
  - Reconcile `README.md` lifecycle step 4 and roadmap status table.
  - Formally document the desktop framework evaluation, backend identity verification requirements, companion pairing lifecycle, and unresolved transport alternatives.
  - Zero desktop code implementation in this slice.
- **P11.2.1 — Native Desktop Shell & System Tray Foundation**:
  - Initialize Tauri 2 project configuration wrapping the existing `frontend/` Vite build.
  - Configure frameless, transparent floating window with Windows 11 drag ergonomics.
  - Implement native system tray with context menu and minimize/hide/show lifecycle.
  - Configure minimal Tauri IPC capability permissions (window management only).
  - Explicitly defer API authentication, state synchronization, and voice/screen capabilities.
- **P11.2.2 — Secure Local Bridge & Policy A Loopback Client**:
  - Implement backend identity verification before emitting any credentials.
  - Qualify and implement the selected loopback transport alternative (Alternative 1, 2, or 3) on Windows 11.
  - Update CORS allowed origins and `validate_origin_and_host()` for `http://tauri.localhost` on verified loopback connections.
  - Implement the companion pairing flow, HttpOnly cookie storage, and CSRF token propagation.
  - Verify remote connection rejection, unauthorized mutation blocking, and safe offline degradation.
- **P11.2.3 — Authoritative Visual State Machine & Compact Panel**:
  - Implement the two-eye expressive presence component.
  - Establish authoritative backend state synchronization contract for the eight presence states.
  - Mount compact flyout panel providing quick chat, proposal review card, and approval inbox.
- **P11.2.4 — Packaging, Memory Verification & Milestone Qualification**:
  - Generate Windows 11 MSI / NSIS installer bundle.
  - Empirically measure actual idle process-tree memory consumption (<100 MB RAM target across all companion processes).
  - Verify clean startup, tray minimization, reconnect resilience, and graceful shutdown on Windows 11.

---

## 11. CodeRabbit Review Disposition & Audit Record

In accordance with PICO's review and verification standards, this record documents the disposition of all CodeRabbit findings on Milestone P11.2.0 (PR #51):

| Finding / Topic | Initial Status | Final Disposition | Resolution Commit / Verification |
| :--- | :--- | :--- | :--- |
| **Trailing Whitespace (Line 21)** | Major | **Fixed** | Trailing whitespace removed at line 21 in commit `33e8aa3`. Verified with `git diff --check origin/main...HEAD` (exited 0). |
| **Windows Tauri Origin in CORS** | Minor | **Fixed** | Updated Section 6 in commit `33e8aa3` to include `http://tauri.localhost` (Windows) and development origins (`http://localhost:1420`). CodeRabbit confirmed addressed. |
| **Grounding for `thinking` State** | Minor | **Fixed** | Updated Section 7 in commit `cc0bfdd` to explicitly name `/api/scheduler/status` polling (`current_active_local`) alongside chat SSE events. CodeRabbit confirmed addressed. |
| **Backend Identity Verification** | Major | **Architecturally Resolved in ADR** | Added Section 6.3 specifying mandatory pre-credential backend identity verification, fail-closed behavior, and re-verification on reconnect. Implementation task assigned to P11.2.2. |
| **Companion Pairing & Credential Lifecycle** | Minor | **Architecturally Resolved in ADR** | Added Section 6.4 detailing full 8-step companion pairing lifecycle, challenge submission, HttpOnly cookie scoping, in-memory CSRF token handoff, session expiry, and browser compatibility. |
| **Transport Pattern A Flaw (`SameSite=Lax`)** | Major | **Architecturally Resolved in ADR** | Added Section 6.5 formally retracting `SameSite=Lax` as a presumed working solution for cross-origin subresource fetch mutations. Reassessed Alternatives 1, 2, and 3 as open qualification tasks for P11.2.2. |
| **Process-Tree Memory Measurement** | Invariant | **Implementation-Stage Verification** | Section 2, 3, 4 updated to clarify all figures are architectural estimates; empirical qualification of complete process tree (<100 MB RAM idle) formally assigned to P11.2.4. |

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

1. **Hardware Memory Budget (<100 MB RAM Idle)**:
   - Target Host: Intel Core i5-13420H, 16 GB DDR5 RAM, NVIDIA GeForce RTX 3050 Laptop GPU (4 GB VRAM), Windows 11.
   - Host RAM is heavily allocated between the Windows OS (~4–6 GB), local Ollama inference (`qwen3.5:2b` resident footprint ~2–3 GB RAM/VRAM), background PICO FastAPI services, and user productivity applications.
   - The companion shell process tree (main shell process + web view renderers + helper utilities) must consume **strictly less than 100 MB RAM** at idle.

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
   - Every bridge request (including read-only status and conversation discovery) requires an active Policy A paired operator session (`pico_operator_session` cookie).
   - Every mutating request requires session-bound CSRF validation (`X-CSRF-Token`).
   - The companion frontend and native shell are untrusted presentation layers: zero direct capability execution, zero embedded API secrets, and zero access to physical actuation tokens (`APPROVAL_SECRET_TOKEN`).

5. **Visual State Truthfulness**:
   - The companion's expressive presence (two-eye animated face) must mirror **authoritative backend state** (Idle, Listening, Thinking, Speaking, Executing, Awaiting Approval, Offline, Error).
   - Absolute prohibition on fabricated animations (e.g. claiming PICO is executing or thinking when no backend lease is active).

6. **Scope Discipline & Milestone Independence**:
   - P11.2 must deliver only the desktop companion shell, system tray, secure bridge, and compact review UI.
   - Local Whisper STT (P11.3), local TTS (P11.3), wake-word detection (P11.3), voice-to-action execution (P11.4), and screen understanding (P11.5) must remain cleanly decoupled.

---

## 3. Technology Evaluation Matrix

We systematically evaluated four candidate Windows desktop technologies against the six decision drivers:

| Evaluation Criterion | Option 1: Tauri 2 + Rust | Option 2: C# / .NET + WebView2 | Option 3: PySide6 (Qt for Python) | Option 4: Electron |
| :--- | :--- | :--- | :--- | :--- |
| **Idle Memory Footprint** | **~30–60 MB RAM** (Uses existing Windows 11 WebView2; tiny Rust binary) | **~70–110 MB RAM** (.NET runtime overhead + WebView2 process tree) | **~150–250 MB RAM** (Qt WebEngine bundles Chromium; QML requires rewrite) | **~180–300+ MB RAM** (Bundles dedicated Chromium + Node.js runtimes) |
| **Meets <100 MB Budget?** | **YES (Measured headroom)** | **MARGINAL (Risks budget breach)** | **NO (Exceeds budget)** | **NO (Violates budget)** |
| **UI Code Reuse** | **100%** (Embeds existing React 18 + Vite frontend directly) | **100%** (Embeds existing React build via WebView2) | **0–20%** (Requires QML rewrite or heavyweight Qt WebEngine) | **100%** (Embeds React frontend in Chromium renderer) |
| **Windows 11 Integration** | **Native** (Tauri system-tray plugin, frameless transparency, Windows API bindings) | **Native** (First-class Windows Forms / WPF / WinUI integration) | **Native** (QSystemTrayIcon, frameless windows, Qt platform abstraction) | **Good** (Tray and frameless windows, but known transparent window drag issues) |
| **Security & Sandboxing** | **High** (Fine-grained capability allowlist; zero native privilege leakage) | **Medium** (Full .NET process permissions; requires manual isolation) | **Medium** (Direct Python interpreter access; no web sandbox isolation) | **Medium-Low** (Node.js integration risks; large attack surface) |
| **Distribution / Installer Size** | **~10–15 MB installer** (WebView2 is pre-installed on Windows 11) | **~50–80 MB installer** (Self-contained or framework-dependent .NET) | **~60–100 MB installer** (Bundled Python runtime and Qt shared libraries) | **~120–180 MB installer** (Full Chromium and Node.js bundled) |
| **Developer Ergonomics** | **High** (Vite HMR dev server, unified TypeScript models, small Rust layer) | **Medium** (Requires Visual Studio / .NET SDK alongside npm/Vite) | **Medium** (Dual Python backend + Python GUI; separate from React UI) | **High** (Standard web development tooling, rapid prototyping) |

---

## 4. Architectural Evaluation of Alternatives

### Option 1: Tauri 2 + Rust (Recommended & Selected)
- **Strengths**:
  - Leverages Windows 11's pre-installed `Microsoft.Web.WebView2` runtime, eliminating the need to bundle a separate Chromium browser engine.
  - The native shell is written in Rust, compiling to a compact binary (<15 MB) that consumes minimal baseline memory (~15–25 MB for the native host process, ~25–35 MB for WebView2 renderer, totaling **~40–60 MB idle RAM**).
  - Out-of-the-box support for system tray icons, context menus, frameless transparent windows, and window drag ergonomics.
  - Native permissions system (Tauri Capabilities) enforces zero IPC privilege exposure to the web view.
- **Trade-offs**:
  - Requires the Rust toolchain (`cargo`, `rustc`) for building the desktop shell binary.
  - Requires careful window transparency configuration under Windows 11 (DWM composition, `window_effects` like Mica/Acrylic or transparent background).

### Option 2: C# / .NET + WebView2
- **Strengths**:
  - Excellent integration with Windows native APIs, WPF/WinUI window management, and Windows notification center.
  - Uses the same Windows 11 WebView2 component as Tauri.
- **Trade-offs**:
  - Requires the .NET SDK and runtime; idle working set typically exceeds 75–100 MB before application state is loaded.
  - Introduces a third language and runtime (.NET) alongside Python (PICO Core) and TypeScript (Frontend), increasing maintenance overhead and build pipeline complexity.

### Option 3: PySide6 (Qt 6 for Python)
- **Strengths**:
  - Python-native; could theoretically integrate with PICO's virtual environment.
  - Mature system tray and window styling capabilities.
- **Trade-offs**:
  - Hosting web content requires `QtWebEngine`, which embeds an entire private Chromium build consuming 150–250+ MB RAM.
  - Alternatively, rewriting the UI in QML or Qt Widgets would abandon PICO's verified React frontend (139 tests, Tailwind styling, Markdown rendering, SVG workflow DAGs), requiring hundreds of hours of duplicate UI development.

### Option 4: Electron
- **Strengths**:
  - Complete React compatibility and broad desktop ecosystem.
- **Trade-offs**:
  - Ships an independent Chromium browser and Node.js runtime, adding 150+ MB to package size and consuming **180–300+ MB RAM idle**. On our 16 GB hardware budget running concurrent local LLM inference, this overhead violates PICO's hardware invariants.
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
│             Tauri 2 Rust Host Process (<20 MB RAM)    │                │
│             Windows WebView2 Renderer (<40 MB RAM)    │                │
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
│   │  - Validates `pico_operator_session` cookie on ALL requests    │   │
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

## 6. Secure Local Bridge Architecture & Policy A Enforcement

1. **Loopback-Only Network Binding**:
   - The desktop companion communicates with PICO Core strictly over IPv4 loopback (`http://127.0.0.1:8000`).
   - Remote, LAN, or non-loopback connections are rejected at the transport and server level.
   - Cross-Origin Resource Sharing (CORS) enforces strict allowed origins matching local companion assets (`tauri://localhost` or `http://localhost:1420`).

2. **Policy A Operator Session Revalidation**:
   - The companion shell does **not** bypass authentication or establish a private backchannel.
   - All HTTP queries and SSE streaming subscriptions require an active, paired operator session cookie (`pico_operator_session`).
   - If the operator is unpaired, the companion locks into the `offline` / `unpaired` state, displaying a clear prompt to complete loopback pairing via the CLI challenge.
   - Every state-mutating request (sending chat messages, triggering workflow synthesis, submitting workflows, approving execution tickets) strictly requires the session-bound CSRF token (`X-CSRF-Token`).

3. **Untrusted Presentation & Zero Secret Storage**:
   - The companion frontend and Tauri native layer are treated as untrusted presentation surfaces.
   - **No Secrets in Assets**: Neither `APPROVAL_SECRET_TOKEN`, cloud API keys, nor system credentials are baked into Tauri bundles, configuration files, or client storage.
   - **No Direct Execution**: The companion cannot execute tools or filesystem actions directly. It proposes interactions to PICO Core, which evaluates permissions, risk tiers, and approval requirements.

4. **Tauri IPC Isolation & Minimal Surface**:
   - The Rust shell exposes **zero custom execution commands** (`invoke(...)`) for system mutation or shell execution.
   - Tauri native APIs are limited strictly to window management (drag, minimize, hide, show, reposition) and system tray event routing.
   - The Tauri security capabilities configuration restricts permissions to window controls only.

---

## 7. Expressive State Machine & Authoritative Visual Identity

The companion's primary visual presence is an expressive two-eye face widget displaying eight authoritative states:

| Visual State | Visual Representation | Triggering Backend Condition | Invariant / Anti-Hallucination Rule |
| :--- | :--- | :--- | :--- |
| **`idle`** | Calm ambient scanning, periodic natural blinking | Backend healthy, operator paired, zero active inference or execution leases | No movement when backend is unreachable |
| **`listening`** | Vibrant cyan pulse, audio waveform oscillation | Push-to-Talk active or audio capture session in progress (`AudioCaptureState.CAPTURING`) | **Prohibited** unless microphone hardware capture is actively streaming |
| **`thinking`** | Concentric rotating purple/blue rings | Active Model Gateway inference lease or scheduler lock acquired | **Prohibited** during idle network or cached data delivery |
| **`speaking`** | Rhythmic acoustic mouth/eye dilation | Local TTS audio playback actively rendering through system speakers | **Prohibited** if audio output is muted, finished, or absent |
| **`executing`** | Focused scanning cadence, directional progress indicator | Workflow step execution in flight (`StepState.RUNNING`) or tool adapter active | **Prohibited** during planning or pending approval states |
| **`awaiting_approval`** | Attention-directing pulsing amber glow | A consequential action has halted at Policy A approval gate (`ApprovalRequest.PENDING`) | Must display until operator approves or rejects the ticket |
| **`offline`** | Calm dim slate/gray indicator, sleepy eyes | Loopback backend unreachable, network disconnected, or session expired | Must truthfully signal disconnection; no simulated interactivity |
| **`error`** | Distinct red alert badge, furrowed indicator | Subsystem error, failed gate, or hardware exception | Cleared only when error condition resolves or is acknowledged |

**Truthful Presentation Invariant**: State transitions are driven 100% by backend Server-Sent Events (SSE) and `/api/health` polling. The companion will never simulate "thinking" or "listening" animations without an authoritative backend event.

---

## 8. Milestone Boundaries & Decoupled Architecture

To ensure incremental, verifiable delivery and prevent scope creep, clear boundaries are established between P11.2 and subsequent milestones:

### In Scope for P11.2 (Companion Shell & Secure Bridge):
- Minimal floating frameless window with drag positioning and styled transparent background.
- System tray icon with status badge and context menu (Status, Open Full UI, Pause Perception, Settings, Quit).
- Loopback HTTP/SSE bridge client authenticating via Policy A paired operator session and session-bound CSRF token.
- Authoritative eight-state expressive eye component and state synchronization engine.
- Compact conversational flyout panel embedding existing chat message stream, workflow review proposal card, and approval inbox.
- Windows 11 packaging, lifecycle management, and empirical memory measurement (<100 MB RAM idle).

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
  - Establish verification baselines.
- **P11.2.1 — Native Desktop Shell & System Tray Foundation**:
  - Initialize Tauri 2 project configuration wrapping the existing `frontend/` Vite build.
  - Configure frameless, transparent floating window with Windows 11 drag ergonomics.
  - Implement native system tray with context menu and minimize/hide/show lifecycle.
- **P11.2.2 — Secure Local Bridge & Policy A Loopback Client**:
  - Implement authenticated loopback client layer within the companion shell.
  - Enforce paired operator session revalidation and CSRF token propagation.
  - Verify remote connection rejection, unauthorized mutation blocking, and safe offline degradation.
- **P11.2.3 — Authoritative Visual State Machine & Compact Panel**:
  - Implement the two-eye expressive presence component.
  - Bind visual state machine to authoritative backend SSE events.
  - Mount compact flyout panel providing quick chat, proposal review card, and approval inbox.
- **P11.2.4 — Packaging, Memory Verification & Milestone Qualification**:
  - Generate Windows 11 MSI / NSIS installer bundle.
  - Measure actual idle process-tree memory consumption (<100 MB RAM target).
  - Verify clean startup, tray minimization, reconnect resilience, and graceful shutdown.

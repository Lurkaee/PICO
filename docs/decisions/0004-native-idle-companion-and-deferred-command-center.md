# 0004. Native Idle Companion Surface & Deferred Command Center

Date: 2026-11-10
Status: Proposed
Deciders: PICO Core Team
Consulted: AGENTS.md, ADR 0001, ADR 0002, ADR 0003

---

## 1. Context & Problem Statement

Milestone P11.2.1 and P11.2.3 established a Windows desktop companion using Tauri 2 (Rust) hosting a React 18 + Vite frontend via Microsoft Edge WebView2.
ADR 0001 defined a non-negotiable architectural invariant:
> **The entire Windows desktop companion process tree must consume less than 100 MB idle RAM.**
> The Python Core is measured and budgeted separately.

During Milestone P11.2.4 physical qualification on Windows 11 target hardware (16 GB RAM, RTX 3050 Laptop GPU, Intel Core i5-13420H), empirical measurements revealed that even with a minimalist React DOM, zero polling loops, and static SVG graphics, Microsoft Edge WebView2 creates a multi-process Chromium hierarchy:
- `PICO.exe` (Native Host): **42.45 MiB Working Set** (11.54 MiB Private Bytes)
- `msedgewebview2.exe` (Browser Main): **118.82 MiB Working Set** (34.80 MiB Private Bytes)
- `msedgewebview2.exe` (GPU Process): **87.31 MiB Working Set** (73.64 MiB Private Bytes)
- `msedgewebview2.exe` (Network Utility): **35.68 MiB Working Set** (11.77 MiB Private Bytes)
- `msedgewebview2.exe` (Storage Utility): **19.01 MiB Working Set** (7.43 MiB Private Bytes)
- `msedgewebview2.exe` (Crashpad Handler): **9.49 MiB Working Set** (2.60 MiB Private Bytes)
- `msedgewebview2.exe` (V8 Renderers): **46.39 MiB Working Set** (20.74 MiB Private Bytes)

**Total Idle Companion Process Tree:**
- **Median Working Set**: **359.15 MiB** (violates <100 MB budget by +259.15 MiB)
- **Median Committed Private Bytes**: **162.52 MiB** (violates budget by +62.52 MiB)

Chromium's mandatory multi-process architecture on Windows 11 allocates ~300 MiB of system memory simply to initialize Direct3D11 swap chains and sandbox IPC brokers before executing a single line of application code. Therefore, an **always-on WebView2 runtime cannot satisfy the <100 MB idle budget**.

---

## 2. Decision Drivers

1. **Strict ADR 0001 Budget Compliance**: The idle companion surface must operate under **100 MB Working Set** without relaxing invariants or altering measurements.
2. **Preserve Existing React Command Center**: The extensive React 18 frontend (workflow graphs, chat streams, approval inbox, memory inspector, accessibility) must be 100% preserved.
3. **Single Authority & Process Supervision**: The system must maintain exactly one supervisor, one Python Core inside a kernel Job Object, and one authenticated bridge with HMAC startup and anti-replay verification.
4. **No Fabricated Companion Behavior**: The native companion surface must mirror authoritative backend state (Idle, Thinking, Executing, Awaiting Approval, Offline, Error).
5. **Clean Deferred Lifecycle**: WebView2 processes must exist **only** while the user has explicitly opened the full Command Center, releasing all memory upon dismissal.

---

## 3. Considered Architectural Options

### Option A: Retain Always-On WebView2 and Relax ADR 0001 Budget
- **Description**: Adjust the ADR 0001 budget to <400 MB Working Set to accommodate Chromium runtime overhead.
- **Outcome**: **REJECTED**. Violates AGENTS.md Section 2 (Hardware Constraints) and Core Principle 10 (Truthful Reporting). Consuming ~360 MB constantly leaves insufficient RAM headroom for concurrent local LLM inference on 16 GB machines.

### Option B: Native Idle Companion Surface with On-Demand React Command Center (Selected)
- **Description**:
  - The always-running desktop companion surface is implemented in **pure native Rust using lightweight Windows APIs** (Win32 frameless layered window `WS_EX_LAYERED`, Direct2D / GDI vector rendering for animated eyes, system tray integration, drag/reposition support, and compact status menu).
  - Estimated idle footprint of the native host: **~15–30 MB Working Set** / **~8–12 MB Private Bytes**.
  - **Zero WebView2 processes** run while the companion is in ambient idle mode.
  - When the operator clicks "Open Command Center" (or selects it from tray/companion menu), Tauri lazily instantiates the WebView2 window (`com.pico.desktop`).
  - When the Command Center is closed, the window and its associated WebView2 environment are disposed, terminating Chromium child processes and returning the footprint to ~20 MB.
- **Outcome**: **ACCEPTED (Proposed)**.

### Option C: Complete Native Desktop Rewrite
- **Description**: Abandon Tauri and WebView2 entirely, rewriting all React components, DAG visualizations, and chat panels in native Rust (e.g., Slint, iced, or egui).
- **Outcome**: **REJECTED**. High implementation cost and risks abandoning verified accessibility and tested web components.

---

## 4. Technical Specification: Native Surface + Deferred Command Center

```
┌────────────────────────────────────────────────────────────────────────┐
│                      Windows 11 Desktop Workspace                      │
│                                                                        │
│  ┌───────────────────────┐              ┌───────────────────────────┐  │
│  │   System Tray Icon    │              │   Native Companion Window │  │
│  │   - Status Badge      │              │   (Win32 WS_EX_LAYERED)   │  │
│  │   - Open UI           │              │   ┌─────────────────────┐ │  │
│  │   - Settings / Quit   │              │   │ Direct2D / GDI Eyes │ │  │
│  └───────────┬───────────┘              │   └─────────────────────┘ │  │
│              │                          └─────────────┬─────────────┘  │
│              │                                        │                │
│              └───────────────────┬────────────────────┘                │
│                                  │                                     │
│                PICO.exe Native Host (Rust Supervisor)                  │
│                Idle Memory: ~15–30 MB Working Set                      │
│                                  │                                     │
│                     [User clicks "Open UI"]                            │
│                                  ▼                                     │
│                 ┌─────────────────────────────────┐                    │
│                 │  On-Demand WebView2 Window     │                    │
│                 │  (React 18 Command Center)      │                    │
│                 │  Active Memory: ~350 MiB        │                    │
│                 │  [Disposed completely on close] │                    │
│                 └─────────────────────────────────┘                    │
│                                  │                                     │
│                                  ▼                                     │
│                    Authenticated Loopback Bridge                       │
│                        (127.0.0.1:8000 only)                           │
│                                  ▼                                     │
│                     Python Core (Job Object)                           │
└────────────────────────────────────────────────────────────────────────┘
```

### A. Lifecycle Phases
1. **Cold Start**:
   - `PICO.exe` launches.
   - Named mutex `Local\PicoDesktopSingleInstanceMutex` is acquired.
   - Native supervisor spawns Python Core inside Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
   - Mutual HMAC-SHA256 handshake verification completes.
   - Native Win32 companion window and system tray icon are created.
   - **Tauri WebView2 window is NOT instantiated** (`visible: false` or deferred creation).
   - **Memory: ~20 MiB native host + ~101 MiB Python Core**. WebView2 processes: **0**.
2. **Open Command Center**:
   - Operator selects "Open PICO" from companion window or tray icon.
   - Tauri builds and shows the Command Center webview window.
   - React application connects via verified loopback bridge.
   - Memory temporarily expands to accommodate Chromium GPU/DOM processes.
3. **Close Command Center**:
   - Operator closes or minimizes the Command Center.
   - The webview window is closed/destroyed.
   - Chromium terminates child worker processes (GPU, renderers).
   - Process tree returns to native idle baseline.
4. **Shutdown**:
   - Operator selects "Quit PICO" from tray.
   - Native companion closes; Job Object terminates Python Core cleanly.

---

## 5. Security & Architectural Invariants

1. **One Supervisor, One Authority**:
   - All desktop interactions route strictly through the single native supervisor.
   - The native companion surface does not possess independent LLM reasoning loops, tool execution engines, or unverified storage.
2. **State Derivation**:
   - The native eyes mirror authoritative backend health (`BackendSupervisionStatus::Online`, `Offline`, `Verifying`) and active state from loopback polling/SSE, preventing fabricated animations.
3. **Integrity of ADR 0001, 0002, 0003**:
   - ADR 0001 (<100 MB companion budget), ADR 0002 (peer verification & Job Object), and ADR 0003 (native pairing & v2 HMAC proof) remain fully authoritative and unchanged.

---

## 6. Consequences & Qualification Roadmap

- **Positive**:
  - Directly achieves the <100 MB idle companion memory budget on target hardware.
  - Zero webview overhead during ambient background operation.
  - Preserves 100% of the verified React frontend and test suites.
- **Negative / Complexity**:
  - Requires implementing a small native Win32/Direct2D window procedure in Rust for the compact eye widget.
  - Requires managing dynamic window lifecycle in Tauri 2 without restarting the supervisor or Python Core.

---

## 7. Measured Implementation Evidence (P11.2.4.1C & P11.2.4.1D)

- **Native Renderer**: Double-buffered Windows GDI+ (`gdiplus.dll`) vector eyes rendered onto the Tauri-managed 240×120 companion HWND via `SetWindowSubclass` / `DefSubclassProc` / `RemoveWindowSubclass` with RAII resource management (`companion_render.rs`). Zero Chromium, Canvas, or WebGL at idle.
- **Measured Release Idle Footprint (`PICO.exe`, 3s–4s steady & blink-window samples)**:
  - **Working Set**: `32.52 MiB` (`< 100 MiB` ADR 0001 limit)
  - **Private Working Set**: `4.95–4.96 MiB`
  - **Private Bytes**: `6.43–6.46 MiB`
  - **CPU Usage**: `0.00%` system CPU (`0 ms` delta across 3s steady idle); `0.26%` (`125 ms` delta across 4s window containing blink/paint activity)
  - **WebView2 Processes (Idle / Post-Close)**: `0`
- **Deferred Command Center Lifecycle**:
  - Dedicated non-UI-thread `WebviewWindowBuilder::build()` / `destroy()` (`CommandCenterLifecycle`) spawns `6` `msedgewebview2.exe` processes when open and returns cleanly to `0` on close while preserving Python Core PIDs and native companion eyes.
- **ADR 0004 Status**: Remains `Proposed` pending P11.2.4.2 packaging/installer completion and formal milestone acceptance.

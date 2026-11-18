# PICO — Native Expressive Eyes: Phase 1 Implementation Audit

**Milestone:** P11.2.4.3 Native Expressive Companion Eyes
**Target:** Win32 GDI+ Subclass Companion Surface (`companion_render.rs`)
**Reference Model:** EMO Robot Eye Animation (Satya Badiger / LivingAI Emo)
**Date:** 2026-11-17

---

## 1. Executive Summary

An independent audit of `origin/main` (`35fb574044146a81b7e0892224b89e6727282cb2`) and `frontend/src-tauri/src/companion_render.rs` was conducted to establish baseline capabilities, architectural constraints, and implementation boundaries before evolving PICO's companion eyes into an EMO-inspired character system.

The audit confirms that the existing Win32 GDI+ subclassing architecture on the native HWND (`SetWindowSubclass` with ID `0x5049_434F_4559`) is solid, leak-free, zero-Chromium at idle, and meets the <100 MB idle memory budget (measured 32.52 MiB Working Set).

However, current eye geometry is constrained to rigid pill capsules (`create_rounded_rect_path`), lacking the organic eyelid curvature, dynamic aperture, Bézier contours, expressive saccades, smiling cheek cuts, and micro-expressions that give EMO its lifelike character presence.

---

## 2. Detailed Capability Breakdown

| Capability Category | Existing Implementation | Missing / Needed Evolution | Architectural Layer |
|---|---|---|---|
| **Window & Subclassing** | Win32 `SetWindowSubclass`, `DefSubclassProc`, `RemoveWindowSubclass`, HWND tracking | Complete; retain without modifications | Presentation / Win32 Host |
| **GDI+ Vector Pipeline** | Anti-aliased double-buffered rendering, offscreen bitmap cache, path clipping, radial gradient glow | Add `GdipAddPathBezier` and `GdipAddPathLine` to extern block for arbitrary organic Bézier spline contours | Presentation / GDI+ |
| **Eye Contour Geometry** | Rigid rounded rectangle capsule with fixed corner arcs (`create_rounded_rect_path`) | Dynamic squircle geometry with upper eyelid curvature, lower eyelid cheek lift, brow tilt, and subtle asymmetry | Presentation / Geometry |
| **Blinking Mechanics** | Linear vertical scale compression (`visible_h = eye_h * openness`) across 6 frames | Curved upper-eyelid sweep down, realistic closure waterline, elastic opening overshoot, occasional natural double-blink | Presentation / Animation |
| **Gaze & Saccades** | 6-step static offset array (`-0.11, +0.11`) | Natural saccades: calm center, alert forward, thoughtful up-left, curious tilt, smooth micro-wanderings | Presentation / Animation |
| **Interactive Feedback** | Click opens Command Center with zero visual response on companion surface | Instant `Happy` crescent squint / catchlight gleam on mouse-down, providing tactile companion feedback | Presentation / Interaction |
| **Authoritative State Mapping** | Maps 5 states: `Unmanaged`, `Starting`, `Verifying`, `Online`, `Offline` | Mapped truthfully: `Starting` -> `WakingUp`, `Verifying` -> `Focused`, `Online` -> `Neutral` + idle micro-expressions, `Offline` -> `Resting` / `Concerned` | Presentation / Authority Bridge |
| **Real Voice/Cognitive States** | None (P11.3 Stage 1 voice work was docs-only; no STT/TTS engine active) | **Do NOT fabricate** `Listening`, `Thinking`, `Speaking`, or `Executing` in backend supervisor | Authoritative Backend (Unmodified) |

---

## 3. Explicit Boundaries: Backend Authority vs Presentation Layer

### A. Authoritative Backend Supervision (Unchanged)
The backend supervisor (`SupervisorState` in `frontend/src-tauri/src/lib.rs` and `supervisor.rs`) manages process lifecycle exclusively:
- `Unmanaged`: Subsystem not started.
- `Starting`: Child process spawned; awaiting handshake.
- `Verifying`: Child process running; mutual HMAC verification and health checks active.
- `Online`: Verified operational.
- `Offline`: Terminated (cleanly or via failure).

No fake states (`Listening`, `Thinking`, `Speaking`, `Executing`, `Awaiting Approval`) shall be injected into `BackendSupervisionStatus`. Doing so violates Core Principle 10 (Truthful Reporting).

### B. Presentation-Layer Expression Engine
The presentation layer in `companion_render.rs` translates authoritative states into rich visual expressions:
- **`Resting`**: Peaceful closed eyelids with soft resting arc.
- **`WakingUp`**: Gradual transition from resting slit to alert squircle eyes.
- **`Neutral`**: Relaxed, open squircle eyes with subtle ambient breathing and gentle gaze.
- **`Attentive`**: Slightly widened aperture, enlarged iris, forward focus.
- **`Focused`**: Determined horizontal narrowing with inward-slanted brow and precision pulse.
- **`Curious`**: Characteristic EMO asymmetry (one eye cocked slightly higher, gaze shifted).
- **`Thinking`**: Upward-tilted glance with thoughtful brow compression.
- **`Happy`**: EMO smiling crescent eye with lower eyelid/cheek pushing up into the iris; triggers on companion interaction.
- **`Confused`**: Inquisitive asymmetric brow slant.
- **`Concerned` / `Error`**: Drooping outer eyelid contour, amber warning glow.

All expression rendering remains **event-driven via Win32 timers (zero continuous 60 FPS polling)**, suspended when minimized or hidden, and strictly deterministic.

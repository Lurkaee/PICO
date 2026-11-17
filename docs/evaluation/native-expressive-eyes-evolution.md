# PICO — Native Expressive Eyes: EMO-Inspired Visual Evolution (P11.2.4.3)

**Date:** 2026-11-17
**Status:** Completed & Verified
**Component:** Native Desktop Companion (`frontend/src-tauri/src/companion_render.rs`)
**Visual Reference:** [EMO Robot Eye Animation (Satya Badiger / LivingAI Emo)](https://youtu.be/pQeTcOIVzHc?si=ijwcRWu1DcZue1qz)
**Parent Decision:** [ADR 0004: Native Idle Companion & Deferred Command Center](../decisions/0004-native-idle-companion-and-deferred-command-center.md)

---

## 1. Executive Summary

Milestone P11.2.4.3 evolves PICO's existing native Windows desktop companion eyes from rigid capsule status indicators into an expressive, character-like animated eye system inspired by EMO robotics.

The implementation preserves 100% of PICO's architecture:
1. **Zero Chromium Overhead**: Pure native Win32 GDI+ double-buffered vector rendering on the native companion HWND (`SetWindowSubclass` `0x5049_434F_4559`).
2. **Strict Hardware Budget**: Preserves the `< 100 MB` idle Working Set budget (measured `32.52 MiB` idle). Zero GPU or heavy model dependencies.
3. **Zero Continuous 60 FPS Polling**: Purely event-driven with discrete one-shot Win32 timers, completely suspended when minimized or hidden.
4. **Authoritative State Truthfulness**: Backend supervisor lifecycle states (`Unmanaged`, `Starting`, `Verifying`, `Online`, `Offline`) remain authoritative. No fabricated cognitive modes (`Listening`, `Thinking`, `Speaking`, `Executing`) are injected into the supervisor.
5. **Presentation Expression Engine**: Character moods (`Neutral`, `Attentive`, `Focused`, `Thinking`, `Curious`, `Happy`, `Confused`, `Concerned`, `Resting`, `WakingUp`) are mapped deterministically from real lifecycle states, ambient idle micro-expressions, and direct interactive touch events.

---

## 2. Visual & Geometric Evolution

### A. Eyelid Curvature & Dynamic Squircle Contour
Previously, PICO's eyes were drawn as rigid rounded rectangles (`create_rounded_rect_path`).
With `GdipAddPathBezier` and `GdipAddPathLine` integrated into the native GDI+ pipeline, eye aperture geometry is computed as a dynamically tensioned continuous vector spline:
- **Upper Eyelid Dome**: Smooth Bézier arch varying from relaxed dome (`upper_arch: +0.12`), wide curious curve (`+0.20`), to determined focus compression (`-0.06`).
- **Lower Eyelid Cheek Lift**: Lower eyelid curves upward into the eye aperture (`cheek_lift: +0.60`), creating EMO's signature smiling crescent eyes in `Happy` mode.
- **Side Span & Corner Radius Constraining**: Corner radius `cr` is bounded by `side_span * 0.44` accounting for cheek lift and brow slant, preventing intersecting arcs or backward-running edges across all DPI scaling factors.
- **Brow Slant**: Inward angle for determined concentration (`Focused`, `brow_slant: +0.16`), outer droop for distress/warning (`Concerned`, `brow_slant: -0.20`).
- **Subtle Asymmetry**: In `Curious` mode, the left eye widens and raises its brow while the right eye slightly squinches, producing a lifelike inquisitive head-tilt expression. In `Confused` mode, the eyebrows adopt opposing angles.

### B. Natural Blinking & Double-Blink Mechanics
- **Curved Sweep**: Eyelids descend with natural ease-in toward a realistic waterline just below center.
- **Elastic Rebound**: Eyelids reopen with a slight elastic overshoot before settling to resting aperture.
- **Natural Double-Blinking**: Every 4th blink cycle automatically triggers a natural double-blink (primary closure followed by an immediate lighter secondary flutter), mimicking authentic biological and robotic cadence.
- **Timer Reconcile On Release**: Releasing a click or losing capture immediately reconciles animation timers, ensuring blinks cannot freeze at intermediate frames.

### C. Natural Saccades & Idle Variations
Rather than static positions, `Online` state ambient gaze cycles across 8 discrete micro-positions (`CALM_GAZE_OFFSETS`):
- Calm center (`(0.0, 0.0)`)
- Gentle glance right (`(0.12, -0.02)`)
- Attentive forward (`(0.0, -0.03)`)
- Thoughtful glance up-left (`(-0.14, -0.10)`)
- Curious glance up-right (`(0.15, -0.08)`)

### D. Tactile Interactive Feedback
On mouse-down (`WM_LBUTTONDOWN`) on the companion window:
- The presentation layer immediately enters `Happy` mood: eyes light up into joyful smiling crescents with an additional third starburst catchlight glint.
- Synchronous `UpdateWindow(hwnd)` guarantees the happy reaction paints immediately before subsequent messages can clear it, with zero re-entrant aliasing of window context.
- On mouse release (`WM_LBUTTONUP`), reaction clears, timers reconcile, and the Command Center is opened or focused.

---

## 3. Visual Gallery & Evidence

All visual evidence below is rendered offscreen via native Windows GDI+ vector routines (`win32::render_companion_to_bmp`) and verified through automated test suites:

| Mood / State | Rendered Preview | Description |
|---|---|---|
| **01. Neutral (Idle Online)** | ![Neutral](screenshots/01_neutral.png) | Calm, relaxed squircle aperture with balanced cyan rim and dual specular catchlights. |
| **02. Natural Blink** | ![Blink](screenshots/02_blink.png) | Curved eyelid sweep closing down across the iris toward the natural waterline. |
| **03. Focused (Verifying)** | ![Focused](screenshots/03_focused_verifying.png) | Narrowed aperture with inward brow slant, determined gaze, and rhythmic precision ring. |
| **04. Waking Up (Starting)** | ![Waking Up](screenshots/04_waking_up_starting.png) | Drowsy slit smoothly opening into alert eyes across process startup steps. |
| **05. Curious** | ![Curious](screenshots/05_curious.png) | Playful asymmetry: raised arched left eye, slightly squinched right eye, glance shifted up-right. |
| **06. Attentive** | ![Attentive](screenshots/06_attentive.png) | Expanded aperture (+10% height), forward gaze, luminous catchlight sparkle. |
| **07. Happy (Interactive)** | ![Happy](screenshots/07_happy.png) | Smiling crescent eyes with cheek pushed up into iris + extra cheerful starburst glint. |
| **08. Confused** | ![Confused](screenshots/08_confused.png) | Opposing brow tilts communicating an inquisitive quizzical glance. |
| **09. Concerned (Error)** | ![Concerned](screenshots/09_concerned_error.png) | Drooping outer eyelid contour with warm amber warning palette (`0xF59E0B`). |
| **10. Resting (Sleeping)** | ![Resting](screenshots/10_resting.png) | Peaceful closed downward-curved sleeping eyelids with subdued resting cyan glow. |
| **11. Thinking** | ![Thinking](screenshots/11_thinking.png) | Slightly compressed aperture with inquisitive upward-left thoughtful gaze shift. |

> [!NOTE]
> The screenshots above represent geometry-level vector evidence rendered through Windows GDI+ offscreen buffers at exact companion canvas dimensions (240×120 @ 96 DPI). They demonstrate mathematical spline correctness, catchlight alignment, and palette rendering.

---

## 4. Review Findings & CI Resolutions

Following independent CodeRabbit review of PR #91, all seven findings were resolved:

1. **Blink Timer Lifecycle (Finding A)**: Reconciled animation timers immediately on mouse-up (`WM_LBUTTONUP`), drag breakout (`WM_MOUSEMOVE`), and capture loss (`WM_CAPTURECHANGED`), preventing blinks from remaining frozen at an intermediate closure frame.
2. **Happy Eye Geometry (Finding B)**: Constrained the corner radius calculation `cr` by `side_span * 0.44` accounting for cheek lift and brow slant, preventing intersecting arcs or backward-running straight edges across all DPI scales.
3. **Authoritative Status Indicator (Finding C)**: Selected the status-dot color strictly from authoritative `ctx.animation.expression` rather than presentation `eff_expr`, ensuring an interactive `Happy` click on an offline/error companion does not turn the status indicator green.
4. **Mouse-Down Visual Feedback (Finding D)**: Bound `UpdateWindow` in `user32` and triggered synchronous painting on `WM_LBUTTONDOWN` after scoping context mutation, guaranteeing the joyful frame is painted before a queued mouse-up without re-entrant borrowing conflicts.
5. **Screenshot Export Failures (Finding E)**: Handled and reported errors with output paths for `fs::create_dir_all` and `fs::write` when `PICO_EXPORT_SCREENSHOTS` is set, asserting file existence.
6. **Documentation Link (Finding F)**: Replaced machine-specific `file:///c:/Users/...` link with repository-relative `../decisions/0004-native-idle-companion-and-deferred-command-center.md`.
7. **Missing Thinking Evidence (Finding G)**: Added genuine `11_thinking.png` preview to the test suite and visual gallery table, accurately documenting 10 character expressions plus blink transition (11 total previews).

CI repairs:
- Ran `cargo fmt --all` on `companion_render.rs`.
- Removed all trailing whitespace across documentation files to pass `git diff --check`.

---

## 5. Automated Verification Results

All 14 unit tests passed with 0 warnings:
```
running 14 tests
test companion_render::tests::test_all_expression_palettes_valid_and_distinct ... ok
test companion_render::tests::test_blink_intervals_within_bounds_and_non_identical ... ok
test companion_render::tests::test_blink_timeline_and_timer_suspension ... ok
test companion_render::tests::test_resting_and_error_states_schedule_zero_timers ... ok
test companion_render::tests::test_dpi_scaling_and_symmetrical_eye_geometry_bounds ... ok
test companion_render::tests::test_effective_expression_mapping_and_interaction ... ok
test companion_render::tests::test_emo_double_blink_lifecycle ... ok
test companion_render::tests::test_expression_mapping_preserves_truthful_authoritative_state ... ok
test companion_render::tests::test_gaze_saccade_bounds_and_coverage ... ok
test companion_render::tests::test_palette_and_coordinator_lifecycle_guards ... ok
test companion_render::tests::test_emo_contour_parameters_and_asymmetry ... ok
test companion_render::tests::test_starting_transition_stops_ambient_timer_after_opening ... ok
test companion_render::tests::test_status_dot_reflects_authoritative_state_not_interactive_reaction ... ok
test companion_render::tests::test_render_and_export_all_expression_screenshots ... ok

test result: ok. 14 passed; 0 failed; 0 ignored; 0 measured; 60 filtered out; finished in 0.32s
```

//! Native Expressive Companion Surface Renderer (P11.2.4.1D).
//!
//! Renders PICO's symmetrical anti-aliased vector eyes directly onto the existing
//! Tauri-managed native companion HWND via Windows GDI+ (`gdiplus.dll`) and
//! Common Controls window subclassing (`SetWindowSubclass` / `DefSubclassProc` /
//! `RemoveWindowSubclass`), with zero Chromium, WebView2, Canvas, or WebGL usage.
//!
//! All eye expressions derive strictly from authoritative `BackendSupervisionStatus`
//! transitions without fabricated listening/thinking/executing indicators or a
//! continuous high-FPS animation loop.

use std::sync::{Arc, Mutex};

use crate::BackendSupervisionStatus;

/// Authoritative visual expression derived strictly from `BackendSupervisionStatus`,
/// enriched with presentation-layer expressive character moods inspired by EMO robotics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompanionExpression {
    /// Core is cleanly stopped or unmanaged: softly closed resting contour, low illumination.
    Resting,
    /// Core process startup is in progress: eyes gradually opening.
    Starting,
    /// Core spawned and cryptographic verification is active: focused eye + subtle pulse.
    Verifying,
    /// Core verified online: calm open cyan-teal eyes + infrequent low-intensity gaze drift.
    Online,
    /// Authoritative failure (spawn failure, verification failure, crash, poisoned lock).
    Error,

    // --- EMO-inspired presentation character expressions ---
    /// Gentle transition from closed resting slit to alert open eyes.
    WakingUp,
    /// Calm, relaxed squircle eyes with gentle gaze.
    Neutral,
    /// Widened aperture, attentive forward gaze, bright catchlights.
    Attentive,
    /// Concentrated narrowed aperture, inward brow slant, precision pulse.
    Focused,
    /// Inquisitive upward-angled glance with thoughtful lid narrowing.
    Thinking,
    /// Asymmetric eye aperture and subtle tilt with curious gaze.
    Curious,
    /// Joyful smiling crescent eyes with upward curved cheeks (e.g. on click interaction).
    Happy,
    /// Inquisitive asymmetric brow slant.
    Confused,
    /// Drooping outer corners expressing concern or distress.
    Concerned,
}

/// Maps authoritative supervisor state into PICO's visual expression without inventing
/// secondary state authorities or unimplemented cognitive modes.
pub fn expression_from_status(status: &BackendSupervisionStatus) -> CompanionExpression {
    match status {
        BackendSupervisionStatus::Unmanaged => CompanionExpression::Resting,
        BackendSupervisionStatus::Starting => CompanionExpression::Starting,
        BackendSupervisionStatus::Verifying { .. } => CompanionExpression::Verifying,
        BackendSupervisionStatus::Online { .. } => CompanionExpression::Online,
        BackendSupervisionStatus::Offline { reason } => {
            let trimmed = reason.trim();
            if trimmed.is_empty() || trimmed == "Application shutdown" {
                CompanionExpression::Resting
            } else {
                CompanionExpression::Error
            }
        }
    }
}

/// Minimum and maximum blink intervals in milliseconds (3.5s - 6.5s).
pub const BLINK_MIN_INTERVAL_MS: u32 = 3500;
pub const BLINK_MAX_INTERVAL_MS: u32 = 6500;
/// Active blink frame cadence (~27 FPS only for the ~216ms duration of a blink).
pub const BLINK_FRAME_INTERVAL_MS: u32 = 36;
/// Opening step cadence while Core starts up.
pub const STARTING_STEP_INTERVAL_MS: u32 = 120;
/// Restrained focus pulse cadence while verifying Core handshake.
pub const VERIFYING_PULSE_INTERVAL_MS: u32 = 500;
/// Low-frequency calm gaze cadence when Core is online.
pub const ONLINE_GAZE_INTERVAL_MS: u32 = 2400;

/// Primary natural blink closure sequence (0.0 = open, 1.0 = sealed).
pub const BLINK_PRIMARY_FRAMES: [f32; 6] = [0.35, 0.82, 1.0, 0.65, 0.20, 0.0];
/// Backwards-compatible alias for existing test assertions.
pub const BLINK_CLOSURE_FRAMES: [f32; 6] = BLINK_PRIMARY_FRAMES;
/// Secondary flutter closure sequence for natural double-blinking.
pub const BLINK_SECONDARY_FRAMES: [f32; 4] = [0.50, 0.92, 0.30, 0.0];

/// Calm and expressive gaze micro-offsets in normalized iris units.
/// Cycled every 2.4s during Online state to create natural character saccades.
pub const CALM_GAZE_OFFSETS: [(f32, f32); 8] = [
    (0.0, 0.0),     // Step 0: Neutral center
    (0.12, -0.02),  // Step 1: Gentle glance right
    (0.0, 0.0),     // Step 2: Attentive center
    (-0.14, -0.10), // Step 3: Thoughtful glance up-left
    (0.0, -0.03),   // Step 4: Alert forward
    (-0.12, 0.02),  // Step 5: Gentle glance left
    (0.0, 0.0),     // Step 6: Thinking center
    (0.15, -0.08),  // Step 7: Curious glance up-right
];

/// Computes deterministic non-identical blink intervals strictly within [3500, 6500] ms.
pub fn next_blink_interval_ms(counter: u32, previous_ms: Option<u32>) -> u32 {
    let span = BLINK_MAX_INTERVAL_MS - BLINK_MIN_INTERVAL_MS + 1;
    let mixed =
        counter.wrapping_mul(1_664_525).wrapping_add(1_013_904_223) ^ (counter.rotate_left(13));
    let mut candidate = BLINK_MIN_INTERVAL_MS + (mixed % span);
    if let Some(prev) = previous_ms {
        if candidate == prev {
            candidate = BLINK_MIN_INTERVAL_MS + ((mixed.wrapping_add(743)) % span);
        }
    }
    candidate
}

/// Converts Windows window DPI (e.g. 96, 120, 144, 192) into normalized display scale.
pub fn scale_for_dpi(dpi: u32) -> f32 {
    if dpi == 0 {
        1.0
    } else {
        (dpi as f32 / 96.0).clamp(1.0, 4.0)
    }
}

/// Desired Win32 timer configuration derived purely from visibility and animation state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimerSchedule {
    pub want_blink_schedule: bool,
    pub want_blink_frames: bool,
    pub ambient_interval_ms: Option<u32>,
}

/// Tracks discrete blink, opening transition, verification pulse, and calm idle gaze
/// without running any permanent high-frequency render loop.
#[derive(Debug, Clone)]
pub struct AnimationState {
    pub expression: CompanionExpression,
    pub visible: bool,
    pub minimized: bool,
    pub blink_frame_idx: Option<usize>,
    pub blink_count: u32,
    pub last_blink_interval_ms: Option<u32>,
    pub starting_step: u8,
    pub verifying_pulse_phase: bool,
    pub gaze_step: usize,
    pub interactive_reaction: bool,
    pub is_double_blink: bool,
    pub double_blink_stage: u8,
}

impl AnimationState {
    pub fn new(initial: CompanionExpression) -> Self {
        Self {
            expression: initial,
            visible: true,
            minimized: false,
            blink_frame_idx: None,
            blink_count: 1,
            last_blink_interval_ms: None,
            starting_step: if initial == CompanionExpression::Starting
                || initial == CompanionExpression::WakingUp
            {
                0
            } else {
                3
            },
            verifying_pulse_phase: false,
            gaze_step: 0,
            interactive_reaction: false,
            is_double_blink: false,
            double_blink_stage: 0,
        }
    }

    pub fn set_expression(&mut self, next: CompanionExpression) -> bool {
        if self.expression == next {
            return false;
        }
        self.expression = next;
        match next {
            CompanionExpression::Starting | CompanionExpression::WakingUp => {
                self.starting_step = 0;
                self.gaze_step = 0;
                self.verifying_pulse_phase = false;
            }
            CompanionExpression::Verifying | CompanionExpression::Focused => {
                self.starting_step = 3;
                self.verifying_pulse_phase = true;
                self.gaze_step = 0;
            }
            CompanionExpression::Online
            | CompanionExpression::Neutral
            | CompanionExpression::Attentive
            | CompanionExpression::Thinking
            | CompanionExpression::Curious
            | CompanionExpression::Happy
            | CompanionExpression::Confused => {
                self.starting_step = 3;
                self.verifying_pulse_phase = false;
                self.gaze_step = 0;
            }
            CompanionExpression::Resting
            | CompanionExpression::Error
            | CompanionExpression::Concerned => {
                self.blink_frame_idx = None;
                self.verifying_pulse_phase = false;
                self.gaze_step = 0;
            }
        }
        true
    }

    /// Sets or clears tactile interactive reaction (e.g. on mouse down/up).
    pub fn set_interactive_reaction(&mut self, active: bool) {
        self.interactive_reaction = active;
        if active {
            self.blink_frame_idx = None;
        }
    }

    /// Resolves the effective presentation mood taking into account interactive reactions
    /// and ambient micro-expressions without altering authoritative supervision truth.
    pub fn effective_expression(&self) -> CompanionExpression {
        if self.interactive_reaction {
            return CompanionExpression::Happy;
        }
        match self.expression {
            CompanionExpression::Online => match self.gaze_step % 8 {
                2 => CompanionExpression::Attentive,
                4 => CompanionExpression::Curious,
                6 => CompanionExpression::Thinking,
                _ => CompanionExpression::Neutral,
            },
            CompanionExpression::Starting => CompanionExpression::WakingUp,
            CompanionExpression::Verifying => CompanionExpression::Focused,
            CompanionExpression::Error => CompanionExpression::Concerned,
            other => other,
        }
    }

    /// Evaluates whether the window is actively observable and permitted to schedule timers.
    pub fn is_render_active(&self) -> bool {
        self.visible && !self.minimized
    }

    /// Computes exact active timers required for current operational and visibility state.
    pub fn timer_schedule(&self) -> TimerSchedule {
        if !self.is_render_active() {
            return TimerSchedule {
                want_blink_schedule: false,
                want_blink_frames: false,
                ambient_interval_ms: None,
            };
        }

        let can_blink = matches!(
            self.effective_expression(),
            CompanionExpression::Online
                | CompanionExpression::Neutral
                | CompanionExpression::Attentive
                | CompanionExpression::Thinking
                | CompanionExpression::Curious
                | CompanionExpression::Verifying
                | CompanionExpression::Focused
        );
        let blinking_now = can_blink && self.blink_frame_idx.is_some();

        let ambient_interval_ms = match self.expression {
            CompanionExpression::Starting if self.starting_step < 3 => {
                Some(STARTING_STEP_INTERVAL_MS)
            }
            CompanionExpression::WakingUp if self.starting_step < 3 => {
                Some(STARTING_STEP_INTERVAL_MS)
            }
            CompanionExpression::Verifying | CompanionExpression::Focused => {
                Some(VERIFYING_PULSE_INTERVAL_MS)
            }
            CompanionExpression::Online | CompanionExpression::Neutral => {
                Some(ONLINE_GAZE_INTERVAL_MS)
            }
            _ => None,
        };

        TimerSchedule {
            want_blink_schedule: can_blink && !blinking_now,
            want_blink_frames: blinking_now,
            ambient_interval_ms,
        }
    }

    /// Begins a single or double blink cycle cleanly.
    pub fn start_blink(&mut self) -> bool {
        if !self.is_render_active() {
            return false;
        }
        let can_blink = matches!(
            self.effective_expression(),
            CompanionExpression::Online
                | CompanionExpression::Neutral
                | CompanionExpression::Attentive
                | CompanionExpression::Thinking
                | CompanionExpression::Curious
                | CompanionExpression::Verifying
                | CompanionExpression::Focused
        );
        if !can_blink {
            return false;
        }
        self.blink_frame_idx = Some(0);
        // Natural double-blink every 4th cycle
        self.is_double_blink = (self.blink_count % 4 == 0) && self.blink_count > 0;
        self.double_blink_stage = 0;
        true
    }

    /// Advances active blink by one frame; returns `true` if full blink sequence ended cleanly.
    pub fn advance_blink_frame(&mut self) -> bool {
        match self.blink_frame_idx {
            Some(idx) => {
                let max_len = if self.is_double_blink && self.double_blink_stage == 1 {
                    BLINK_SECONDARY_FRAMES.len()
                } else {
                    BLINK_PRIMARY_FRAMES.len()
                };

                if idx + 1 < max_len {
                    self.blink_frame_idx = Some(idx + 1);
                    false
                } else if self.is_double_blink && self.double_blink_stage == 0 {
                    // Transition to secondary quick flutter
                    self.double_blink_stage = 1;
                    self.blink_frame_idx = Some(0);
                    false
                } else {
                    self.blink_frame_idx = None;
                    self.is_double_blink = false;
                    self.double_blink_stage = 0;
                    self.blink_count = self.blink_count.wrapping_add(1);
                    let next_ms =
                        next_blink_interval_ms(self.blink_count, self.last_blink_interval_ms);
                    self.last_blink_interval_ms = Some(next_ms);
                    true
                }
            }
            None => true,
        }
    }

    /// Advances ambient low-frequency state step (`Starting`, `Verifying`, `Online`).
    pub fn advance_ambient(&mut self) {
        match self.expression {
            CompanionExpression::Starting | CompanionExpression::WakingUp => {
                if self.starting_step < 3 {
                    self.starting_step += 1;
                }
            }
            CompanionExpression::Verifying | CompanionExpression::Focused => {
                self.verifying_pulse_phase = !self.verifying_pulse_phase;
            }
            CompanionExpression::Online | CompanionExpression::Neutral => {
                self.gaze_step = (self.gaze_step + 1) % CALM_GAZE_OFFSETS.len();
            }
            _ => {}
        }
    }

    /// Returns current eyelid closure amount in [0.0, 1.0].
    pub fn blink_closure_amount(&self) -> f32 {
        let idx = match self.blink_frame_idx {
            Some(i) => i,
            None => return 0.0,
        };
        if self.is_double_blink && self.double_blink_stage == 1 {
            BLINK_SECONDARY_FRAMES.get(idx).copied().unwrap_or(0.0)
        } else {
            BLINK_PRIMARY_FRAMES.get(idx).copied().unwrap_or(0.0)
        }
    }

    /// Returns normalized eyelid openness in [0.0, 1.15].
    pub fn eye_openness(&self) -> f32 {
        let eff = self.effective_expression();
        let base = match eff {
            CompanionExpression::Resting => 0.0,
            CompanionExpression::Starting | CompanionExpression::WakingUp => {
                match self.starting_step {
                    0 => 0.22,
                    1 => 0.52,
                    2 => 0.80,
                    _ => 1.0,
                }
            }
            CompanionExpression::Verifying | CompanionExpression::Focused => 0.86,
            CompanionExpression::Online | CompanionExpression::Neutral => 1.0,
            CompanionExpression::Attentive => 1.08,
            CompanionExpression::Thinking => 0.90,
            CompanionExpression::Curious => 1.02,
            CompanionExpression::Happy => 0.80,
            CompanionExpression::Confused => 0.94,
            CompanionExpression::Concerned | CompanionExpression::Error => 0.68,
        };
        let closure = self.blink_closure_amount();
        (base * (1.0 - closure)).clamp(0.0, 1.15)
    }

    /// Returns restrained normalized gaze offset `(gx, gy)` for iris placement.
    pub fn gaze_offset(&self) -> (f32, f32) {
        if self.interactive_reaction {
            return (0.0, -0.04);
        }
        match self.effective_expression() {
            CompanionExpression::Thinking => (-0.15, -0.12),
            CompanionExpression::Curious => (0.16, -0.08),
            CompanionExpression::Attentive => (0.0, -0.03),
            CompanionExpression::Confused => (-0.08, 0.04),
            CompanionExpression::Online | CompanionExpression::Neutral => {
                CALM_GAZE_OFFSETS[self.gaze_step % CALM_GAZE_OFFSETS.len()]
            }
            _ => (0.0, 0.0),
        }
    }
}

/// Pure rectangle in physical client coordinates.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PixelRect {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

/// Pure DPI-scaled geometry of the native companion face and symmetrical eyes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EyeGeometry {
    pub client_width: f32,
    pub client_height: f32,
    pub dpi_scale: f32,
    pub visor_x: f32,
    pub visor_y: f32,
    pub visor_w: f32,
    pub visor_h: f32,
    pub visor_radius: f32,
    pub left_eye_cx: f32,
    pub right_eye_cx: f32,
    pub eye_cy: f32,
    pub eye_w: f32,
    pub eye_h: f32,
    pub halo_pad: f32,
    pub dirty_rect: PixelRect,
}

impl EyeGeometry {
    /// Computes balanced, symmetrical vector geometry for the given client dimensions and DPI.
    pub fn compute(width: i32, height: i32, dpi: u32) -> Self {
        let w_i = width.max(1);
        let h_i = height.max(1);
        let w = w_i as f32;
        let h = h_i as f32;
        let dpi_scale = scale_for_dpi(dpi);

        let visor_x = w * 0.075;
        let visor_y = h * 0.125;
        let visor_w = (w * 0.85).max(16.0);
        let visor_h = (h * 0.75).max(16.0);
        let visor_radius = (16.0 * dpi_scale).min(visor_h * 0.46).min(visor_w * 0.22);

        let left_eye_cx = w * 0.35;
        let right_eye_cx = w * 0.65;
        let eye_cy = h * 0.50;

        let eye_w = (w * 0.195).min(h * 0.44).max(12.0);
        let eye_h = (eye_w * 1.10).min(visor_h * 0.74).max(12.0);
        let halo_pad = (8.0 * dpi_scale).max(4.0);

        let d_left = ((left_eye_cx - eye_w * 0.5 - halo_pad).floor() as i32).clamp(0, w_i);
        let d_right = ((right_eye_cx + eye_w * 0.5 + halo_pad).ceil() as i32).clamp(0, w_i);
        let d_top = ((eye_cy - eye_h * 0.5 - halo_pad).floor() as i32).clamp(0, h_i);
        let d_bottom = ((eye_cy + eye_h * 0.5 + halo_pad).ceil() as i32).clamp(0, h_i);

        Self {
            client_width: w,
            client_height: h,
            dpi_scale,
            visor_x,
            visor_y,
            visor_w,
            visor_h,
            visor_radius,
            left_eye_cx,
            right_eye_cx,
            eye_cy,
            eye_w,
            eye_h,
            halo_pad,
            dirty_rect: PixelRect {
                left: d_left,
                top: d_top,
                right: d_right,
                bottom: d_bottom,
            },
        }
    }

    /// Returns `true` when `(x, y)` falls inside the eye/visor interaction region.
    pub fn is_in_eye_click_zone(&self, x: i32, y: i32) -> bool {
        let xf = x as f32;
        let yf = y as f32;
        xf >= self.visor_x
            && xf <= (self.visor_x + self.visor_w)
            && yf >= self.visor_y
            && yf <= (self.visor_y + self.visor_h)
    }
}

/// ARGB color tuple for native vector rendering.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExpressionPalette {
    pub halo_argb: u32,
    pub rim_argb: u32,
    pub iris_outer_argb: u32,
    pub iris_core_argb: u32,
    pub highlight_argb: u32,
    pub resting_lid_argb: u32,
}

/// Returns calm PICO obsidian + cyan/teal or restrained amber warning palette.
pub fn palette_for_expression(
    expr: CompanionExpression,
    verifying_pulse: bool,
) -> ExpressionPalette {
    match expr {
        CompanionExpression::Resting => ExpressionPalette {
            halo_argb: 0x0C06B6D4,
            rim_argb: 0x7022D3EE,
            iris_outer_argb: 0x200891B2,
            iris_core_argb: 0x3567E8F9,
            highlight_argb: 0x45E0F7FA,
            resting_lid_argb: 0x8C38BDF8,
        },
        CompanionExpression::Starting | CompanionExpression::WakingUp => ExpressionPalette {
            halo_argb: 0x1E06B6D4,
            rim_argb: 0xBA22D3EE,
            iris_outer_argb: 0xD00891B2,
            iris_core_argb: 0xE622D3EE,
            highlight_argb: 0xB8E0F7FA,
            resting_lid_argb: 0xB038BDF8,
        },
        CompanionExpression::Verifying | CompanionExpression::Focused => {
            if verifying_pulse {
                ExpressionPalette {
                    halo_argb: 0x3006B6D4,
                    rim_argb: 0xF067E8F9,
                    iris_outer_argb: 0xEB0891B2,
                    iris_core_argb: 0xF538BDF8,
                    highlight_argb: 0xE6FFFFFF,
                    resting_lid_argb: 0xC838BDF8,
                }
            } else {
                ExpressionPalette {
                    halo_argb: 0x1806B6D4,
                    rim_argb: 0xB822D3EE,
                    iris_outer_argb: 0xC80E7490,
                    iris_core_argb: 0xD406B6D4,
                    highlight_argb: 0xB8E0F7FA,
                    resting_lid_argb: 0xB038BDF8,
                }
            }
        }
        CompanionExpression::Online | CompanionExpression::Neutral => ExpressionPalette {
            halo_argb: 0x2406B6D4,
            rim_argb: 0xEB22D3EE,
            iris_outer_argb: 0xEA0891B2,
            iris_core_argb: 0xF538BDF8,
            highlight_argb: 0xE8ECFEFF,
            resting_lid_argb: 0xCC38BDF8,
        },
        CompanionExpression::Attentive => ExpressionPalette {
            halo_argb: 0x2E06B6D4,
            rim_argb: 0xF522D3EE,
            iris_outer_argb: 0xF00891B2,
            iris_core_argb: 0xFA67E8F9,
            highlight_argb: 0xF0FFFFFF,
            resting_lid_argb: 0xCC38BDF8,
        },
        CompanionExpression::Thinking => ExpressionPalette {
            halo_argb: 0x1C06B6D4,
            rim_argb: 0xD822D3EE,
            iris_outer_argb: 0xDE0E7490,
            iris_core_argb: 0xEB22D3EE,
            highlight_argb: 0xD0ECFEFF,
            resting_lid_argb: 0xB838BDF8,
        },
        CompanionExpression::Curious => ExpressionPalette {
            halo_argb: 0x2606B6D4,
            rim_argb: 0xEE22D3EE,
            iris_outer_argb: 0xEC0891B2,
            iris_core_argb: 0xF838BDF8,
            highlight_argb: 0xECFFFFFF,
            resting_lid_argb: 0xCC38BDF8,
        },
        CompanionExpression::Happy => ExpressionPalette {
            halo_argb: 0x3606B6D4,
            rim_argb: 0xFF67E8F9,
            iris_outer_argb: 0xF20891B2,
            iris_core_argb: 0xFF38BDF8,
            highlight_argb: 0xFFFFFFFF,
            resting_lid_argb: 0xE067E8F9,
        },
        CompanionExpression::Confused => ExpressionPalette {
            halo_argb: 0x2006B6D4,
            rim_argb: 0xDE22D3EE,
            iris_outer_argb: 0xE00891B2,
            iris_core_argb: 0xED22D3EE,
            highlight_argb: 0xDCECFEFF,
            resting_lid_argb: 0xC038BDF8,
        },
        CompanionExpression::Concerned => ExpressionPalette {
            halo_argb: 0x24F59E0B,
            rim_argb: 0xEEF59E0B,
            iris_outer_argb: 0xDEB45309,
            iris_core_argb: 0xEEFBBF24,
            highlight_argb: 0xD8FEF3C7,
            resting_lid_argb: 0xD0F59E0B,
        },
        CompanionExpression::Error => ExpressionPalette {
            halo_argb: 0x26F59E0B,
            rim_argb: 0xF0F59E0B,
            iris_outer_argb: 0xE0B45309,
            iris_core_argb: 0xF0FBBF24,
            highlight_argb: 0xDCFEF3C7,
            resting_lid_argb: 0xD8F59E0B,
        },
    }
}

/// Geometric contour parameters for anti-aliased EMO-inspired eyelid shapes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EyeContourParams {
    pub width_scale: f32,
    pub height_scale: f32,
    pub openness: f32,
    pub upper_arch: f32,
    pub brow_slant: f32,
    pub cheek_lift: f32,
    pub corner_factor: f32,
    pub vertical_offset: f32,
}

/// Computes expressive contour parameters based on active mood and eyelid state.
pub fn contour_params_for_expression(
    expr: CompanionExpression,
    openness: f32,
    starting_step: u8,
    is_right_eye: bool,
) -> EyeContourParams {
    match expr {
        CompanionExpression::Resting => EyeContourParams {
            width_scale: 1.0,
            height_scale: 1.0,
            openness: 0.0,
            upper_arch: 0.0,
            brow_slant: 0.0,
            cheek_lift: 0.0,
            corner_factor: 0.40,
            vertical_offset: 0.0,
        },
        CompanionExpression::Starting | CompanionExpression::WakingUp => {
            let (arch, h_scale) = match starting_step {
                0 => (-0.06, 0.50),
                1 => (0.04, 0.75),
                2 => (0.10, 0.90),
                _ => (0.12, 1.0),
            };
            EyeContourParams {
                width_scale: 1.0,
                height_scale: h_scale,
                openness,
                upper_arch: arch,
                brow_slant: 0.0,
                cheek_lift: 0.0,
                corner_factor: 0.40,
                vertical_offset: 0.0,
            }
        }
        CompanionExpression::Verifying | CompanionExpression::Focused => EyeContourParams {
            width_scale: 1.02,
            height_scale: 0.88,
            openness,
            upper_arch: -0.06,
            brow_slant: 0.16,
            cheek_lift: 0.08,
            corner_factor: 0.38,
            vertical_offset: 0.0,
        },
        CompanionExpression::Online | CompanionExpression::Neutral => EyeContourParams {
            width_scale: 1.0,
            height_scale: 1.0,
            openness,
            upper_arch: 0.12,
            brow_slant: 0.0,
            cheek_lift: 0.0,
            corner_factor: 0.40,
            vertical_offset: 0.0,
        },
        CompanionExpression::Attentive => EyeContourParams {
            width_scale: 1.06,
            height_scale: 1.10,
            openness,
            upper_arch: 0.18,
            brow_slant: 0.0,
            cheek_lift: 0.0,
            corner_factor: 0.44,
            vertical_offset: -0.02,
        },
        CompanionExpression::Thinking => EyeContourParams {
            width_scale: 0.98,
            height_scale: 0.90,
            openness,
            upper_arch: -0.04,
            brow_slant: 0.08,
            cheek_lift: 0.06,
            corner_factor: 0.38,
            vertical_offset: -0.03,
        },
        CompanionExpression::Curious => {
            if is_right_eye {
                EyeContourParams {
                    width_scale: 0.95,
                    height_scale: 0.92,
                    openness: openness * 0.96,
                    upper_arch: 0.04,
                    brow_slant: 0.10,
                    cheek_lift: 0.04,
                    corner_factor: 0.38,
                    vertical_offset: 0.02,
                }
            } else {
                EyeContourParams {
                    width_scale: 1.07,
                    height_scale: 1.08,
                    openness,
                    upper_arch: 0.20,
                    brow_slant: -0.06,
                    cheek_lift: 0.0,
                    corner_factor: 0.44,
                    vertical_offset: -0.03,
                }
            }
        }
        CompanionExpression::Happy => EyeContourParams {
            width_scale: 1.05,
            height_scale: 0.84,
            openness,
            upper_arch: 0.22,
            brow_slant: 0.0,
            cheek_lift: 0.60,
            corner_factor: 0.42,
            vertical_offset: -0.01,
        },
        CompanionExpression::Confused => {
            if is_right_eye {
                EyeContourParams {
                    width_scale: 0.96,
                    height_scale: 0.94,
                    openness,
                    upper_arch: -0.02,
                    brow_slant: 0.22,
                    cheek_lift: 0.05,
                    corner_factor: 0.38,
                    vertical_offset: 0.03,
                }
            } else {
                EyeContourParams {
                    width_scale: 1.04,
                    height_scale: 1.06,
                    openness,
                    upper_arch: 0.16,
                    brow_slant: -0.18,
                    cheek_lift: 0.0,
                    corner_factor: 0.42,
                    vertical_offset: -0.03,
                }
            }
        }
        CompanionExpression::Concerned | CompanionExpression::Error => EyeContourParams {
            width_scale: 0.98,
            height_scale: 0.90,
            openness,
            upper_arch: 0.04,
            brow_slant: -0.20,
            cheek_lift: 0.0,
            corner_factor: 0.38,
            vertical_offset: 0.02,
        },
    }
}

/// Thread-safe coordinator linking Tauri `SupervisorState` transitions to the
/// attached native companion HWND without polling or secondary state ownership.
pub struct CompanionRenderCoordinator {
    hwnd: Mutex<Option<isize>>,
    status: Arc<Mutex<BackendSupervisionStatus>>,
    status_label: Arc<Mutex<String>>,
}

impl CompanionRenderCoordinator {
    pub fn new(initial_status: BackendSupervisionStatus, initial_label: String) -> Self {
        Self {
            hwnd: Mutex::new(None),
            status: Arc::new(Mutex::new(initial_status)),
            status_label: Arc::new(Mutex::new(initial_label)),
        }
    }

    pub fn update_status(&self, status: &BackendSupervisionStatus, label: String) {
        if let Ok(mut guard) = self.status.lock() {
            *guard = status.clone();
        }
        if let Ok(mut guard) = self.status_label.lock() {
            *guard = label;
        }
        #[cfg(windows)]
        if let Ok(guard) = self.hwnd.lock() {
            if let Some(raw_hwnd) = *guard {
                win32::post_status_update(raw_hwnd);
            }
        }
    }

    pub fn clear_hwnd(&self) {
        if let Ok(mut guard) = self.hwnd.lock() {
            *guard = None;
        }
    }
}

/// Attaches native GDI+ vector eye rendering and interaction subclassing to the
/// Tauri-managed `"companion"` window. Must be called from the UI thread that created it.
#[cfg(windows)]
pub fn attach_companion_renderer(
    window: &tauri::Window,
    app_handle: tauri::AppHandle,
    coordinator: &CompanionRenderCoordinator,
) -> Result<(), String> {
    let hwnd = window
        .hwnd()
        .map_err(|e| format!("Failed to obtain companion HWND: {}", e))?;
    let raw_hwnd = hwnd.0 as isize;
    if let Ok(mut guard) = coordinator.hwnd.lock() {
        *guard = Some(raw_hwnd);
    }
    if let Err(err) = win32::attach_hwnd(
        raw_hwnd,
        app_handle,
        Arc::clone(&coordinator.status),
        Arc::clone(&coordinator.status_label),
    ) {
        coordinator.clear_hwnd();
        return Err(err);
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn attach_companion_renderer(
    _window: &tauri::Window,
    _app_handle: tauri::AppHandle,
    _coordinator: &CompanionRenderCoordinator,
) -> Result<(), String> {
    Ok(())
}

#[cfg(windows)]
mod win32 {
    use super::*;
    use std::ffi::c_void;
    use std::ptr;
    use tauri::Manager;

    type HWND = *mut c_void;
    type HDC = *mut c_void;
    type HBITMAP = *mut c_void;
    type HGDIOBJ = *mut c_void;
    type HMENU = *mut c_void;
    type HCURSOR = *mut c_void;
    type HINSTANCE = *mut c_void;
    type BOOL = i32;
    type UINT = u32;
    type WPARAM = usize;
    type LPARAM = isize;
    type LRESULT = isize;
    type DWORD = u32;

    const COMPANION_SUBCLASS_ID: usize = 0x5049_434F_4559;
    const WM_APP_PICO_STATUS: UINT = 0x8000 + 0x41;

    const WM_SIZE: UINT = 0x0005;
    const WM_PAINT: UINT = 0x000F;
    const WM_ERASEBKGND: UINT = 0x0014;
    const WM_SHOWWINDOW: UINT = 0x0018;
    const WM_SETCURSOR: UINT = 0x0020;
    const WM_NCDESTROY: UINT = 0x0082;
    const WM_NCLBUTTONDOWN: UINT = 0x00A1;
    const WM_TIMER: UINT = 0x0113;
    const WM_MOUSEMOVE: UINT = 0x0200;
    const WM_LBUTTONDOWN: UINT = 0x0201;
    const WM_LBUTTONUP: UINT = 0x0202;
    const WM_RBUTTONUP: UINT = 0x0205;
    const WM_CAPTURECHANGED: UINT = 0x0215;
    const WM_DPICHANGED: UINT = 0x02E0;

    const SIZE_MINIMIZED: usize = 1;
    const HTCAPTION: usize = 2;
    const HTCLIENT: isize = 1;
    const SM_CXDRAG: i32 = 68;
    const SM_CYDRAG: i32 = 69;
    const SRCCOPY: u32 = 0x00CC_0020;
    const IDC_ARROW: usize = 32512;

    const MF_STRING: UINT = 0x0000;
    const MF_SEPARATOR: UINT = 0x0800;
    const MF_GRAYED: UINT = 0x0001;
    const TPM_RIGHTBUTTON: UINT = 0x0002;
    const TPM_RETURNCMD: UINT = 0x0100;

    const MENU_CMD_OPEN_PICO: i32 = 101;
    const MENU_CMD_QUIT_PICO: i32 = 102;

    const TIMER_BLINK_SCHEDULE_ID: usize = 1;
    const TIMER_BLINK_FRAME_ID: usize = 2;
    const TIMER_AMBIENT_GAZE_ID: usize = 3;

    const SMOOTHING_MODE_ANTI_ALIAS: i32 = 4;
    const PIXEL_OFFSET_MODE_HIGH_QUALITY: i32 = 2;
    const UNIT_PIXEL: i32 = 2;
    const LINE_CAP_ROUND: i32 = 2;
    const FILL_MODE_ALTERNATE: i32 = 0;
    const COMBINE_MODE_INTERSECT: i32 = 1;

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    struct RECT {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    struct POINT {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    struct PAINTSTRUCT {
        hdc: HDC,
        f_erase: BOOL,
        rc_paint: RECT,
        f_restore: BOOL,
        f_inc_update: BOOL,
        rgb_reserved: [u8; 32],
    }

    impl Default for PAINTSTRUCT {
        fn default() -> Self {
            Self {
                hdc: ptr::null_mut(),
                f_erase: 0,
                rc_paint: RECT::default(),
                f_restore: 0,
                f_inc_update: 0,
                rgb_reserved: [0u8; 32],
            }
        }
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct BITMAPINFOHEADER {
        bi_size: u32,
        bi_width: i32,
        bi_height: i32,
        bi_planes: u16,
        bi_bit_count: u16,
        bi_compression: u32,
        bi_size_image: u32,
        bi_xpels_per_meter: i32,
        bi_ypels_per_meter: i32,
        bi_clr_used: u32,
        bi_clr_important: u32,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct RGBQUAD {
        rgb_blue: u8,
        rgb_green: u8,
        rgb_red: u8,
        rgb_reserved: u8,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct BITMAPINFO {
        bmi_header: BITMAPINFOHEADER,
        bmi_colors: [RGBQUAD; 1],
    }

    #[repr(C)]
    struct GdiplusStartupInput {
        gdiplus_version: u32,
        debug_event_callback: *const c_void,
        suppress_background_thread: BOOL,
        suppress_external_codecs: BOOL,
    }

    #[repr(C)]
    struct GdiplusStartupOutput {
        notification_hook: *const c_void,
        notification_unhook: *const c_void,
    }

    type SubclassProc = unsafe extern "system" fn(
        hwnd: HWND,
        msg: UINT,
        wparam: WPARAM,
        lparam: LPARAM,
        u_id_subclass: usize,
        dw_ref_data: usize,
    ) -> LRESULT;

    #[link(name = "user32")]
    extern "system" {
        fn GetCurrentThreadId() -> DWORD;
        fn GetWindowThreadProcessId(hwnd: HWND, lpdw_process_id: *mut DWORD) -> DWORD;
        fn GetClientRect(hwnd: HWND, lp_rect: *mut RECT) -> BOOL;
        fn GetDpiForWindow(hwnd: HWND) -> UINT;
        fn IsWindow(hwnd: HWND) -> BOOL;
        fn IsWindowVisible(hwnd: HWND) -> BOOL;
        fn IsIconic(hwnd: HWND) -> BOOL;
        fn InvalidateRect(hwnd: HWND, lp_rect: *const RECT, b_erase: BOOL) -> BOOL;
        fn UpdateWindow(hwnd: HWND) -> BOOL;
        fn PostMessageW(hwnd: HWND, msg: UINT, wparam: WPARAM, lparam: LPARAM) -> BOOL;
        fn SendMessageW(hwnd: HWND, msg: UINT, wparam: WPARAM, lparam: LPARAM) -> LRESULT;
        fn SetTimer(
            hwnd: HWND,
            n_id_event: usize,
            u_elapse: UINT,
            lp_timer_func: *const c_void,
        ) -> usize;
        fn KillTimer(hwnd: HWND, u_id_event: usize) -> BOOL;
        fn BeginPaint(hwnd: HWND, lp_paint: *mut PAINTSTRUCT) -> HDC;
        fn EndPaint(hwnd: HWND, lp_paint: *const PAINTSTRUCT) -> BOOL;
        fn SetCapture(hwnd: HWND) -> HWND;
        fn ReleaseCapture() -> BOOL;
        fn GetSystemMetrics(n_index: i32) -> i32;
        fn GetCursorPos(lp_point: *mut POINT) -> BOOL;
        fn LoadCursorW(h_instance: HINSTANCE, lp_cursor_name: *const u16) -> HCURSOR;
        fn SetCursor(h_cursor: HCURSOR) -> HCURSOR;
        fn SetForegroundWindow(hwnd: HWND) -> BOOL;
        fn CreatePopupMenu() -> HMENU;
        fn AppendMenuW(
            h_menu: HMENU,
            u_flags: UINT,
            u_id_new_item: usize,
            lp_new_item: *const u16,
        ) -> BOOL;
        fn TrackPopupMenu(
            h_menu: HMENU,
            u_flags: UINT,
            x: i32,
            y: i32,
            n_reserved: i32,
            hwnd: HWND,
            prc_rect: *const RECT,
        ) -> i32;
        fn DestroyMenu(h_menu: HMENU) -> BOOL;
    }

    #[link(name = "gdi32")]
    extern "system" {
        fn CreateCompatibleDC(hdc: HDC) -> HDC;
        fn DeleteDC(hdc: HDC) -> BOOL;
        fn CreateCompatibleBitmap(hdc: HDC, cx: i32, cy: i32) -> HBITMAP;
        fn SelectObject(hdc: HDC, h: HGDIOBJ) -> HGDIOBJ;
        fn DeleteObject(ho: HGDIOBJ) -> BOOL;
        fn BitBlt(
            hdc: HDC,
            x: i32,
            y: i32,
            cx: i32,
            cy: i32,
            hdc_src: HDC,
            x1: i32,
            y1: i32,
            rop: u32,
        ) -> BOOL;
        #[allow(dead_code)]
        fn CreateDIBSection(
            hdc: HDC,
            pbmi: *const BITMAPINFO,
            usage: UINT,
            ppv_bits: *mut *mut c_void,
            h_section: *mut c_void,
            offset: DWORD,
        ) -> HBITMAP;
    }

    #[link(name = "comctl32")]
    extern "system" {
        fn SetWindowSubclass(
            hwnd: HWND,
            pfn_subclass: SubclassProc,
            u_id_subclass: usize,
            dw_ref_data: usize,
        ) -> BOOL;
        fn RemoveWindowSubclass(
            hwnd: HWND,
            pfn_subclass: SubclassProc,
            u_id_subclass: usize,
        ) -> BOOL;
        fn DefSubclassProc(hwnd: HWND, msg: UINT, wparam: WPARAM, lparam: LPARAM) -> LRESULT;
    }

    #[link(name = "gdiplus")]
    extern "system" {
        fn GdiplusStartup(
            token: *mut usize,
            input: *const GdiplusStartupInput,
            output: *mut GdiplusStartupOutput,
        ) -> i32;
        fn GdiplusShutdown(token: usize);
        fn GdipCreateFromHDC(hdc: HDC, graphics: *mut *mut c_void) -> i32;
        fn GdipDeleteGraphics(graphics: *mut c_void) -> i32;
        fn GdipSetSmoothingMode(graphics: *mut c_void, mode: i32) -> i32;
        fn GdipSetPixelOffsetMode(graphics: *mut c_void, mode: i32) -> i32;
        fn GdipGraphicsClear(graphics: *mut c_void, color: u32) -> i32;
        fn GdipCreateSolidFill(color: u32, brush: *mut *mut c_void) -> i32;
        fn GdipCreatePathGradientFromPath(path: *const c_void, poly_grad: *mut *mut c_void) -> i32;
        fn GdipSetPathGradientCenterColor(brush: *mut c_void, color: u32) -> i32;
        fn GdipSetPathGradientSurroundColorsWithCount(
            brush: *mut c_void,
            color: *const u32,
            count: *mut i32,
        ) -> i32;
        fn GdipDeleteBrush(brush: *mut c_void) -> i32;
        fn GdipCreatePen1(color: u32, width: f32, unit: i32, pen: *mut *mut c_void) -> i32;
        fn GdipSetPenStartCap(pen: *mut c_void, cap: i32) -> i32;
        fn GdipSetPenEndCap(pen: *mut c_void, cap: i32) -> i32;
        fn GdipDeletePen(pen: *mut c_void) -> i32;
        fn GdipCreatePath(fill_mode: i32, path: *mut *mut c_void) -> i32;
        fn GdipDeletePath(path: *mut c_void) -> i32;
        fn GdipAddPathArc(
            path: *mut c_void,
            x: f32,
            y: f32,
            width: f32,
            height: f32,
            start_angle: f32,
            sweep_angle: f32,
        ) -> i32;
        fn GdipAddPathEllipse(path: *mut c_void, x: f32, y: f32, width: f32, height: f32) -> i32;
        fn GdipAddPathBezier(
            path: *mut c_void,
            x1: f32,
            y1: f32,
            x2: f32,
            y2: f32,
            x3: f32,
            y3: f32,
            x4: f32,
            y4: f32,
        ) -> i32;
        fn GdipAddPathLine(path: *mut c_void, x1: f32, y1: f32, x2: f32, y2: f32) -> i32;
        fn GdipClosePathFigure(path: *mut c_void) -> i32;
        fn GdipFillPath(graphics: *mut c_void, brush: *mut c_void, path: *mut c_void) -> i32;
        fn GdipDrawPath(graphics: *mut c_void, pen: *mut c_void, path: *mut c_void) -> i32;
        fn GdipFillEllipse(
            graphics: *mut c_void,
            brush: *mut c_void,
            x: f32,
            y: f32,
            width: f32,
            height: f32,
        ) -> i32;
        fn GdipDrawEllipse(
            graphics: *mut c_void,
            pen: *mut c_void,
            x: f32,
            y: f32,
            width: f32,
            height: f32,
        ) -> i32;
        fn GdipDrawArc(
            graphics: *mut c_void,
            pen: *mut c_void,
            x: f32,
            y: f32,
            width: f32,
            height: f32,
            start_angle: f32,
            sweep_angle: f32,
        ) -> i32;
        fn GdipDrawLine(
            graphics: *mut c_void,
            pen: *mut c_void,
            x1: f32,
            y1: f32,
            x2: f32,
            y2: f32,
        ) -> i32;
        fn GdipSetClipPath(graphics: *mut c_void, path: *mut c_void, combine_mode: i32) -> i32;
        fn GdipResetClip(graphics: *mut c_void) -> i32;
    }

    struct GdiplusSession {
        token: usize,
    }

    impl GdiplusSession {
        fn startup() -> Result<Self, String> {
            let input = GdiplusStartupInput {
                gdiplus_version: 1,
                debug_event_callback: ptr::null(),
                suppress_background_thread: 0,
                suppress_external_codecs: 0,
            };
            let mut output = GdiplusStartupOutput {
                notification_hook: ptr::null(),
                notification_unhook: ptr::null(),
            };
            let mut token: usize = 0;
            // SAFETY: Pointers refer cleanly to stack-allocated POD startup structs.
            let status = unsafe { GdiplusStartup(&mut token, &input, &mut output) };
            if status != 0 || token == 0 {
                return Err(format!("GdiplusStartup failed with status {}", status));
            }
            Ok(Self { token })
        }
    }

    impl Drop for GdiplusSession {
        fn drop(&mut self) {
            if self.token != 0 {
                // SAFETY: `self.token` was returned by a balanced `GdiplusStartup`.
                unsafe { GdiplusShutdown(self.token) };
                self.token = 0;
            }
        }
    }

    struct GdipGraphics(*mut c_void);
    impl Drop for GdipGraphics {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { GdipDeleteGraphics(self.0) };
            }
        }
    }

    struct GdipBrush(*mut c_void);
    impl Drop for GdipBrush {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { GdipDeleteBrush(self.0) };
            }
        }
    }

    struct GdipPen(*mut c_void);
    impl Drop for GdipPen {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { GdipDeletePen(self.0) };
            }
        }
    }

    struct GdipPath(*mut c_void);
    impl Drop for GdipPath {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { GdipDeletePath(self.0) };
            }
        }
    }

    /// Reusable offscreen GDI surface so blink frames avoid reallocating bitmaps.
    struct OffscreenSurface {
        mem_dc: HDC,
        bitmap: HBITMAP,
        old_bitmap: HGDIOBJ,
        width: i32,
        height: i32,
    }

    impl OffscreenSurface {
        fn ensure_for(
            slot: &mut Option<Self>,
            screen_dc: HDC,
            width: i32,
            height: i32,
        ) -> Option<HDC> {
            let w = width.max(1);
            let h = height.max(1);
            if let Some(existing) = slot.as_ref() {
                if existing.width == w && existing.height == h {
                    return Some(existing.mem_dc);
                }
            }
            // Drop stale surface before allocating matched dimensions.
            *slot = None;
            // SAFETY: Valid compatible DC/bitmap lifecycle paired with `Drop`.
            unsafe {
                let mem_dc = CreateCompatibleDC(screen_dc);
                if mem_dc.is_null() {
                    return None;
                }
                let bitmap = CreateCompatibleBitmap(screen_dc, w, h);
                if bitmap.is_null() {
                    DeleteDC(mem_dc);
                    return None;
                }
                let old_bitmap = SelectObject(mem_dc, bitmap);
                *slot = Some(Self {
                    mem_dc,
                    bitmap,
                    old_bitmap,
                    width: w,
                    height: h,
                });
                Some(mem_dc)
            }
        }
    }

    impl Drop for OffscreenSurface {
        fn drop(&mut self) {
            // SAFETY: Restores original bitmap before deleting compatible bitmap and DC.
            unsafe {
                if !self.mem_dc.is_null() {
                    if !self.old_bitmap.is_null() {
                        SelectObject(self.mem_dc, self.old_bitmap);
                    }
                    if !self.bitmap.is_null() {
                        DeleteObject(self.bitmap);
                    }
                    DeleteDC(self.mem_dc);
                }
            }
        }
    }

    #[derive(Default)]
    struct ActiveTimers {
        blink_schedule: bool,
        blink_frames: bool,
        ambient_ms: Option<u32>,
    }

    struct CompanionWindowContext {
        _gdiplus: GdiplusSession,
        app_handle: tauri::AppHandle,
        status_snapshot: Arc<Mutex<BackendSupervisionStatus>>,
        status_label: Arc<Mutex<String>>,
        animation: AnimationState,
        timers: ActiveTimers,
        backbuffer: Option<OffscreenSurface>,
        dpi: u32,
        pressing: bool,
        down_x: i32,
        down_y: i32,
    }

    impl CompanionWindowContext {
        fn sync_status(&mut self) -> bool {
            let expr = if let Ok(guard) = self.status_snapshot.lock() {
                expression_from_status(&guard)
            } else {
                CompanionExpression::Error
            };
            self.animation.set_expression(expr)
        }

        fn reconcile_timers(&mut self, hwnd: HWND) {
            let desired = self.animation.timer_schedule();

            if desired.want_blink_schedule && !self.timers.blink_schedule {
                let next_ms = next_blink_interval_ms(
                    self.animation.blink_count,
                    self.animation.last_blink_interval_ms,
                );
                self.animation.last_blink_interval_ms = Some(next_ms);
                // SAFETY: Valid UI-thread HWND.
                unsafe { SetTimer(hwnd, TIMER_BLINK_SCHEDULE_ID, next_ms, ptr::null()) };
                self.timers.blink_schedule = true;
            } else if !desired.want_blink_schedule && self.timers.blink_schedule {
                unsafe { KillTimer(hwnd, TIMER_BLINK_SCHEDULE_ID) };
                self.timers.blink_schedule = false;
            }

            if desired.want_blink_frames && !self.timers.blink_frames {
                unsafe {
                    SetTimer(
                        hwnd,
                        TIMER_BLINK_FRAME_ID,
                        BLINK_FRAME_INTERVAL_MS,
                        ptr::null(),
                    )
                };
                self.timers.blink_frames = true;
            } else if !desired.want_blink_frames && self.timers.blink_frames {
                unsafe { KillTimer(hwnd, TIMER_BLINK_FRAME_ID) };
                self.timers.blink_frames = false;
            }

            if desired.ambient_interval_ms != self.timers.ambient_ms {
                if self.timers.ambient_ms.is_some() {
                    unsafe { KillTimer(hwnd, TIMER_AMBIENT_GAZE_ID) };
                    self.timers.ambient_ms = None;
                }
                if let Some(ms) = desired.ambient_interval_ms {
                    unsafe { SetTimer(hwnd, TIMER_AMBIENT_GAZE_ID, ms, ptr::null()) };
                    self.timers.ambient_ms = Some(ms);
                }
            }
        }

        fn kill_all_timers(&mut self, hwnd: HWND) {
            if self.timers.blink_schedule {
                unsafe { KillTimer(hwnd, TIMER_BLINK_SCHEDULE_ID) };
                self.timers.blink_schedule = false;
            }
            if self.timers.blink_frames {
                unsafe { KillTimer(hwnd, TIMER_BLINK_FRAME_ID) };
                self.timers.blink_frames = false;
            }
            if self.timers.ambient_ms.is_some() {
                unsafe { KillTimer(hwnd, TIMER_AMBIENT_GAZE_ID) };
                self.timers.ambient_ms = None;
            }
        }
    }

    pub fn post_status_update(raw_hwnd: isize) {
        if raw_hwnd == 0 {
            return;
        }
        let hwnd = raw_hwnd as HWND;
        // SAFETY: `IsWindow` + `PostMessageW` are thread-safe across Win32 threads.
        unsafe {
            if IsWindow(hwnd) != 0 {
                let _ = PostMessageW(hwnd, WM_APP_PICO_STATUS, 0, 0);
            }
        }
    }

    pub fn attach_hwnd(
        raw_hwnd: isize,
        app_handle: tauri::AppHandle,
        status_snapshot: Arc<Mutex<BackendSupervisionStatus>>,
        status_label: Arc<Mutex<String>>,
    ) -> Result<(), String> {
        if raw_hwnd == 0 {
            return Err("Invalid null HWND for native companion".to_string());
        }
        let hwnd = raw_hwnd as HWND;
        // SAFETY: Query thread ownership of the HWND before subclassing.
        unsafe {
            let owner_tid = GetWindowThreadProcessId(hwnd, ptr::null_mut());
            let current_tid = GetCurrentThreadId();
            if owner_tid == 0 || owner_tid != current_tid {
                return Err(format!(
                    "Companion HWND must be attached on owning UI thread (owner={}, current={})",
                    owner_tid, current_tid
                ));
            }
        }

        let gdiplus = GdiplusSession::startup()?;
        let initial_expr = status_snapshot
            .lock()
            .map(|g| expression_from_status(&g))
            .unwrap_or(CompanionExpression::Starting);
        // SAFETY: Valid HWND on owning thread.
        let dpi = unsafe { GetDpiForWindow(hwnd) };
        let visible = unsafe { IsWindowVisible(hwnd) != 0 };
        let minimized = unsafe { IsIconic(hwnd) != 0 };

        let mut animation = AnimationState::new(initial_expr);
        animation.visible = visible;
        animation.minimized = minimized;

        let mut ctx = Box::new(CompanionWindowContext {
            _gdiplus: gdiplus,
            app_handle,
            status_snapshot,
            status_label,
            animation,
            timers: ActiveTimers::default(),
            backbuffer: None,
            dpi,
            pressing: false,
            down_x: 0,
            down_y: 0,
        });

        ctx.reconcile_timers(hwnd);
        let raw_ctx = Box::into_raw(ctx);

        // SAFETY: Installs comctl32 subclass without altering GWLP_WNDPROC or GWL_USERDATA.
        // Ownership of `raw_ctx` is transferred to the subclass and reclaimed on `WM_NCDESTROY`
        // (or immediately below if `SetWindowSubclass` fails).
        let ok = unsafe {
            SetWindowSubclass(
                hwnd,
                companion_subclass_proc,
                COMPANION_SUBCLASS_ID,
                raw_ctx as usize,
            )
        };
        if ok == 0 {
            unsafe {
                let mut reclaimed = Box::from_raw(raw_ctx);
                reclaimed.kill_all_timers(hwnd);
            }
            return Err("SetWindowSubclass failed for PICO companion HWND".to_string());
        }

        unsafe { InvalidateRect(hwnd, ptr::null(), 0) };
        Ok(())
    }

    fn client_coords(lparam: LPARAM) -> (i32, i32) {
        let x = (lparam & 0xFFFF) as i16 as i32;
        let y = ((lparam >> 16) & 0xFFFF) as i16 as i32;
        (x, y)
    }

    fn invalidate_eyes_only(hwnd: HWND, ctx: &CompanionWindowContext) {
        let mut rc = RECT::default();
        // SAFETY: Reads live client dimensions on UI thread.
        if unsafe { GetClientRect(hwnd, &mut rc) } != 0 {
            let geom = EyeGeometry::compute(rc.right - rc.left, rc.bottom - rc.top, ctx.dpi);
            let dirty = RECT {
                left: geom.dirty_rect.left,
                top: geom.dirty_rect.top,
                right: geom.dirty_rect.right,
                bottom: geom.dirty_rect.bottom,
            };
            unsafe { InvalidateRect(hwnd, &dirty, 0) };
        } else {
            unsafe { InvalidateRect(hwnd, ptr::null(), 0) };
        }
    }

    fn create_rounded_rect_path(x: f32, y: f32, w: f32, h: f32, radius: f32) -> Option<GdipPath> {
        let r = radius.min(w * 0.48).min(h * 0.48).max(1.0);
        let d = r * 2.0;
        let mut path_ptr: *mut c_void = ptr::null_mut();
        // SAFETY: Balanced GDI+ path creation wrapped in RAII `GdipPath`.
        unsafe {
            if GdipCreatePath(FILL_MODE_ALTERNATE, &mut path_ptr) != 0 || path_ptr.is_null() {
                return None;
            }
            let path = GdipPath(path_ptr);
            GdipAddPathArc(path.0, x, y, d, d, 180.0, 90.0);
            GdipAddPathArc(path.0, x + w - d, y, d, d, 270.0, 90.0);
            GdipAddPathArc(path.0, x + w - d, y + h - d, d, d, 0.0, 90.0);
            GdipAddPathArc(path.0, x, y + h - d, d, d, 90.0, 90.0);
            GdipClosePathFigure(path.0);
            Some(path)
        }
    }

    fn create_solid_brush(argb: u32) -> Option<GdipBrush> {
        let mut ptr_brush: *mut c_void = ptr::null_mut();
        unsafe {
            if GdipCreateSolidFill(argb, &mut ptr_brush) == 0 && !ptr_brush.is_null() {
                Some(GdipBrush(ptr_brush))
            } else {
                None
            }
        }
    }

    fn create_pen(argb: u32, width: f32, round_caps: bool) -> Option<GdipPen> {
        let mut ptr_pen: *mut c_void = ptr::null_mut();
        unsafe {
            if GdipCreatePen1(argb, width, UNIT_PIXEL, &mut ptr_pen) == 0 && !ptr_pen.is_null() {
                if round_caps {
                    GdipSetPenStartCap(ptr_pen, LINE_CAP_ROUND);
                    GdipSetPenEndCap(ptr_pen, LINE_CAP_ROUND);
                }
                Some(GdipPen(ptr_pen))
            } else {
                None
            }
        }
    }

    fn create_expressive_eye_path(
        cx: f32,
        cy: f32,
        base_w: f32,
        base_h: f32,
        params: &EyeContourParams,
        is_right_eye: bool,
    ) -> Option<GdipPath> {
        let w = base_w * params.width_scale;
        let h = (base_h * params.height_scale * params.openness).max(6.0);
        let half_w = w * 0.5;
        let half_h = h * 0.5;
        let eye_cy = cy + params.vertical_offset * base_h;

        let slant_mag = params.brow_slant * half_h * 0.40;
        let (slant_left, slant_right) = if is_right_eye {
            (slant_mag, -slant_mag)
        } else {
            (-slant_mag, slant_mag)
        };

        let upper_dome = params.upper_arch * half_h * 0.55;
        let cheek_lift = params.cheek_lift * half_h * 0.95;
        let cheek_arch = params.cheek_lift * half_h * 0.65;

        let cheek_drop = cheek_lift;
        let side_span = (2.0 * half_h - cheek_drop - slant_mag.abs()).max(4.0);
        let cr = (w * params.corner_factor)
            .min(half_h * 0.88)
            .min(side_span * 0.44)
            .max(2.0);

        let p_tl_x = cx - half_w;
        let p_tl_y = eye_cy - half_h + slant_left;
        let p_tr_x = cx + half_w;
        let p_tr_y = eye_cy - half_h + slant_right;
        let p_br_x = cx + half_w;
        let p_br_y = eye_cy + half_h - cheek_lift;
        let p_bl_x = cx - half_w;
        let p_bl_y = eye_cy + half_h - cheek_lift;

        let mut path_ptr: *mut c_void = ptr::null_mut();
        unsafe {
            if GdipCreatePath(FILL_MODE_ALTERNATE, &mut path_ptr) != 0 || path_ptr.is_null() {
                return None;
            }
            let path = GdipPath(path_ptr);

            // 1. Top-left corner arc: sweeping from 180 to 270 deg
            GdipAddPathArc(path.0, p_tl_x, p_tl_y, cr * 2.0, cr * 2.0, 180.0, 90.0);

            // 2. Upper eyelid Bézier arch
            GdipAddPathBezier(
                path.0,
                p_tl_x + cr,
                p_tl_y,
                cx - half_w * 0.25,
                p_tl_y - upper_dome,
                cx + half_w * 0.25,
                p_tr_y - upper_dome,
                p_tr_x - cr,
                p_tr_y,
            );

            // 3. Top-right corner arc: sweeping from 270 to 360 deg
            GdipAddPathArc(
                path.0,
                p_tr_x - cr * 2.0,
                p_tr_y,
                cr * 2.0,
                cr * 2.0,
                270.0,
                90.0,
            );

            // 4. Right edge straight down
            GdipAddPathLine(path.0, p_tr_x, p_tr_y + cr, p_br_x, p_br_y - cr);

            // 5. Bottom-right corner arc: sweeping from 0 to 90 deg
            GdipAddPathArc(
                path.0,
                p_br_x - cr * 2.0,
                p_br_y - cr * 2.0,
                cr * 2.0,
                cr * 2.0,
                0.0,
                90.0,
            );

            // 6. Lower eyelid / cheek Bézier arch
            GdipAddPathBezier(
                path.0,
                p_br_x - cr,
                p_br_y,
                cx + half_w * 0.25,
                p_br_y - cheek_arch,
                cx - half_w * 0.25,
                p_bl_y - cheek_arch,
                p_bl_x + cr,
                p_bl_y,
            );

            // 7. Bottom-left corner arc: sweeping from 90 to 180 deg
            GdipAddPathArc(
                path.0,
                p_bl_x,
                p_bl_y - cr * 2.0,
                cr * 2.0,
                cr * 2.0,
                90.0,
                90.0,
            );

            // 8. Left edge straight up
            GdipAddPathLine(path.0, p_bl_x, p_bl_y - cr, p_tl_x, p_tl_y + cr);

            GdipClosePathFigure(path.0);
            Some(path)
        }
    }

    fn draw_single_eye(
        graphics: *mut c_void,
        cx: f32,
        cy: f32,
        eye_w: f32,
        eye_h: f32,
        dpi_scale: f32,
        openness: f32,
        gaze: (f32, f32),
        expr: CompanionExpression,
        starting_step: u8,
        palette: &ExpressionPalette,
        is_right_eye: bool,
    ) {
        let params = contour_params_for_expression(expr, openness, starting_step, is_right_eye);

        // 1. Resting / closed contour when aperture is essentially shut
        if params.openness <= 0.07 {
            let line_w = eye_w * 0.88;
            let arc_h = eye_h * 0.26;
            let pen_w = (2.2 * dpi_scale).max(1.6);
            if let Some(lid_pen) = create_pen(palette.resting_lid_argb, pen_w, true) {
                // Soft downward-curved sleeping eyelid
                unsafe {
                    GdipDrawArc(
                        graphics,
                        lid_pen.0,
                        cx - line_w * 0.5,
                        cy - arc_h * 0.35,
                        line_w,
                        arc_h * 2.0,
                        25.0,
                        130.0,
                    );
                }
            }
            return;
        }

        let eff_w = eye_w * params.width_scale;
        let eff_h = (eye_h * params.height_scale * params.openness).max(6.0);
        let eye_cy = cy + params.vertical_offset * eye_h;

        // 2. Subtle outer ambient glow halo (soft vector radial gradient)
        let halo_w = eff_w * 1.48;
        let halo_h = eff_h * 1.48;
        let mut halo_path_ptr: *mut c_void = ptr::null_mut();
        unsafe {
            if GdipCreatePath(FILL_MODE_ALTERNATE, &mut halo_path_ptr) == 0
                && !halo_path_ptr.is_null()
            {
                let halo_path = GdipPath(halo_path_ptr);
                GdipAddPathEllipse(
                    halo_path.0,
                    cx - halo_w * 0.5,
                    eye_cy - halo_h * 0.5,
                    halo_w,
                    halo_h,
                );
                let mut grad_ptr: *mut c_void = ptr::null_mut();
                if GdipCreatePathGradientFromPath(halo_path.0, &mut grad_ptr) == 0
                    && !grad_ptr.is_null()
                {
                    let grad_brush = GdipBrush(grad_ptr);
                    GdipSetPathGradientCenterColor(grad_brush.0, palette.halo_argb);
                    let mut count = 1i32;
                    let transparent = 0x0000_0000u32;
                    GdipSetPathGradientSurroundColorsWithCount(
                        grad_brush.0,
                        &transparent,
                        &mut count,
                    );
                    GdipFillEllipse(
                        graphics,
                        grad_brush.0,
                        cx - halo_w * 0.5,
                        eye_cy - halo_h * 0.5,
                        halo_w,
                        halo_h,
                    );
                }
            }
        }

        // 3. EMO-inspired expressive eyelid aperture path
        if let Some(eye_path) =
            create_expressive_eye_path(cx, cy, eye_w, eye_h, &params, is_right_eye)
        {
            if let Some(cavity_brush) = create_solid_brush(0xFF0A_1320) {
                unsafe { GdipFillPath(graphics, cavity_brush.0, eye_path.0) };
            }

            // Clip iris & catchlights cleanly inside the eye aperture
            unsafe {
                GdipSetClipPath(graphics, eye_path.0, COMBINE_MODE_INTERSECT);
            }

            let iris_d = (eff_w * 0.74).min(eye_h * params.height_scale * 0.74);
            let gx = gaze.0 * eye_w * 0.35;
            let gy = (gaze.1 + params.vertical_offset) * eye_h * 0.25;
            let iris_x = cx + gx - iris_d * 0.5;
            let iris_y = eye_cy + gy - iris_d * 0.5;

            if let Some(iris_outer) = create_solid_brush(palette.iris_outer_argb) {
                unsafe { GdipFillEllipse(graphics, iris_outer.0, iris_x, iris_y, iris_d, iris_d) };
            }

            let core_d = iris_d * 0.72;
            if let Some(iris_core) = create_solid_brush(palette.iris_core_argb) {
                unsafe {
                    GdipFillEllipse(
                        graphics,
                        iris_core.0,
                        cx + gx - core_d * 0.5,
                        eye_cy + gy - core_d * 0.5,
                        core_d,
                        core_d,
                    );
                }
            }

            // Restrained inner iris precision ring
            let ring_d = iris_d * 0.84;
            let ring_argb = if matches!(
                expr,
                CompanionExpression::Verifying | CompanionExpression::Focused
            ) {
                palette.rim_argb
            } else {
                0x50E0_F7FA
            };
            if let Some(ring_pen) = create_pen(ring_argb, (0.9 * dpi_scale).max(0.8), false) {
                unsafe {
                    GdipDrawEllipse(
                        graphics,
                        ring_pen.0,
                        cx + gx - ring_d * 0.5,
                        eye_cy + gy - ring_d * 0.5,
                        ring_d,
                        ring_d,
                    );
                }
            }

            // Restrained dark pupil depth core
            let pupil_d = iris_d * 0.36;
            if let Some(pupil_brush) = create_solid_brush(0xEE07_0E17) {
                unsafe {
                    GdipFillEllipse(
                        graphics,
                        pupil_brush.0,
                        cx + gx - pupil_d * 0.5,
                        eye_cy + gy - pupil_d * 0.5,
                        pupil_d,
                        pupil_d,
                    );
                }
            }

            // Dual specular catchlights + extra cheerful gleam in Happy mood
            let spec_big = (iris_d * 0.20).max(2.2);
            let spec_small = (iris_d * 0.09).max(1.2);
            if let Some(spec_brush) = create_solid_brush(palette.highlight_argb) {
                unsafe {
                    GdipFillEllipse(
                        graphics,
                        spec_brush.0,
                        cx + gx - iris_d * 0.22 - spec_big * 0.5,
                        eye_cy + gy - iris_d * 0.20 - spec_big * 0.5,
                        spec_big,
                        spec_big,
                    );
                    GdipFillEllipse(
                        graphics,
                        spec_brush.0,
                        cx + gx + iris_d * 0.16,
                        eye_cy + gy + iris_d * 0.14,
                        spec_small,
                        spec_small,
                    );
                    if expr == CompanionExpression::Happy {
                        let gleam_d = (iris_d * 0.14).max(1.8);
                        GdipFillEllipse(
                            graphics,
                            spec_brush.0,
                            cx + gx + iris_d * 0.12 - gleam_d * 0.5,
                            eye_cy + gy - iris_d * 0.24 - gleam_d * 0.5,
                            gleam_d,
                            gleam_d,
                        );
                    }
                }
            }

            unsafe { GdipResetClip(graphics) };

            // Outer glowing vector contour
            let rim_width = (1.6 * dpi_scale).max(1.25);
            if let Some(rim_pen) = create_pen(palette.rim_argb, rim_width, true) {
                unsafe { GdipDrawPath(graphics, rim_pen.0, eye_path.0) };
            }

            // Brow accents for Focused or Concerned/Error
            if matches!(
                expr,
                CompanionExpression::Error | CompanionExpression::Concerned
            ) {
                let brow_pen_w = (1.8 * dpi_scale).max(1.4);
                if let Some(brow_pen) = create_pen(palette.rim_argb, brow_pen_w, true) {
                    let y_outer = eye_cy - eff_h * 0.5 - 2.0 * dpi_scale;
                    let y_inner = eye_cy - eff_h * 0.5 + 3.0 * dpi_scale;
                    let (x1, y1, x2, y2) = if is_right_eye {
                        (cx - eff_w * 0.42, y_inner, cx + eff_w * 0.42, y_outer)
                    } else {
                        (cx - eff_w * 0.42, y_outer, cx + eff_w * 0.42, y_inner)
                    };
                    unsafe { GdipDrawLine(graphics, brow_pen.0, x1, y1, x2, y2) };
                }
            } else if matches!(
                expr,
                CompanionExpression::Focused | CompanionExpression::Verifying
            ) {
                let brow_pen_w = (1.5 * dpi_scale).max(1.2);
                if let Some(brow_pen) = create_pen(palette.rim_argb, brow_pen_w, true) {
                    let y_outer = eye_cy - eff_h * 0.5 - 3.0 * dpi_scale;
                    let y_inner = eye_cy - eff_h * 0.5 - 1.0 * dpi_scale;
                    let (x1, y1, x2, y2) = if is_right_eye {
                        (cx - eff_w * 0.38, y_inner, cx + eff_w * 0.38, y_outer)
                    } else {
                        (cx - eff_w * 0.38, y_outer, cx + eff_w * 0.38, y_inner)
                    };
                    unsafe { GdipDrawLine(graphics, brow_pen.0, x1, y1, x2, y2) };
                }
            }
        }
    }

    fn paint_companion(hwnd: HWND, ctx: &mut CompanionWindowContext) {
        let mut ps = PAINTSTRUCT::default();
        // SAFETY: Standard `BeginPaint` / `EndPaint` UI-thread paint block.
        let screen_dc = unsafe { BeginPaint(hwnd, &mut ps) };
        if screen_dc.is_null() {
            return;
        }

        let mut rc = RECT::default();
        if unsafe { GetClientRect(hwnd, &mut rc) } != 0 {
            let width = (rc.right - rc.left).max(1);
            let height = (rc.bottom - rc.top).max(1);

            if let Some(mem_dc) =
                OffscreenSurface::ensure_for(&mut ctx.backbuffer, screen_dc, width, height)
            {
                let mut g_ptr: *mut c_void = ptr::null_mut();
                if unsafe { GdipCreateFromHDC(mem_dc, &mut g_ptr) } == 0 && !g_ptr.is_null() {
                    let graphics = GdipGraphics(g_ptr);
                    unsafe {
                        GdipSetSmoothingMode(graphics.0, SMOOTHING_MODE_ANTI_ALIAS);
                        GdipSetPixelOffsetMode(graphics.0, PIXEL_OFFSET_MODE_HIGH_QUALITY);
                        // Deep obsidian canvas matching PICO Command Center `--bg-canvas` (#090B11)
                        GdipGraphicsClear(graphics.0, 0xFF09_0B11);
                    }

                    let geom = EyeGeometry::compute(width, height, ctx.dpi);
                    // Subtle visor face plate (`--bg-surface` #111420 + `--border-subtle` #1E2438)
                    if let Some(visor_path) = create_rounded_rect_path(
                        geom.visor_x,
                        geom.visor_y,
                        geom.visor_w,
                        geom.visor_h,
                        geom.visor_radius,
                    ) {
                        if let Some(visor_fill) = create_solid_brush(0xFF10_1421) {
                            unsafe { GdipFillPath(graphics.0, visor_fill.0, visor_path.0) };
                        }
                        if let Some(visor_border) =
                            create_pen(0xFF1E_263C, (1.1 * geom.dpi_scale).max(1.0), false)
                        {
                            unsafe { GdipDrawPath(graphics.0, visor_border.0, visor_path.0) };
                        }
                    }

                    let eff_expr = ctx.animation.effective_expression();
                    let palette =
                        palette_for_expression(eff_expr, ctx.animation.verifying_pulse_phase);
                    let openness = ctx.animation.eye_openness();
                    let gaze = ctx.animation.gaze_offset();

                    draw_single_eye(
                        graphics.0,
                        geom.left_eye_cx,
                        geom.eye_cy,
                        geom.eye_w,
                        geom.eye_h,
                        geom.dpi_scale,
                        openness,
                        gaze,
                        eff_expr,
                        ctx.animation.starting_step,
                        &palette,
                        false,
                    );
                    draw_single_eye(
                        graphics.0,
                        geom.right_eye_cx,
                        geom.eye_cy,
                        geom.eye_w,
                        geom.eye_h,
                        geom.dpi_scale,
                        openness,
                        gaze,
                        eff_expr,
                        ctx.animation.starting_step,
                        &palette,
                        true,
                    );

                    // Subtle bottom corner status dot (small restrained cue mirroring Online/Error)
                    let dot_r = (2.4 * geom.dpi_scale).max(2.0);
                    let dot_x = geom.visor_x + geom.visor_w - (11.0 * geom.dpi_scale);
                    let dot_y = geom.visor_y + geom.visor_h - (10.0 * geom.dpi_scale);
                    let dot_color = match ctx.animation.expression {
                        CompanionExpression::Online
                        | CompanionExpression::Neutral
                        | CompanionExpression::Attentive
                        | CompanionExpression::Curious
                        | CompanionExpression::Thinking
                        | CompanionExpression::Happy => 0xD010_B981,
                        CompanionExpression::Error | CompanionExpression::Concerned => 0xE8EF_4444,
                        CompanionExpression::Verifying
                        | CompanionExpression::Focused
                        | CompanionExpression::Starting
                        | CompanionExpression::WakingUp => 0xCC06_B6D4,
                        CompanionExpression::Resting => 0x6064_748B,
                        CompanionExpression::Confused => 0xCCA8_55F7,
                    };
                    if let Some(dot_brush) = create_solid_brush(dot_color) {
                        unsafe {
                            GdipFillEllipse(
                                graphics.0,
                                dot_brush.0,
                                dot_x - dot_r,
                                dot_y - dot_r,
                                dot_r * 2.0,
                                dot_r * 2.0,
                            );
                        }
                    }

                    // Ensure GDI+ flushes before BitBlt (dropping `graphics` flushes)
                    drop(graphics);
                    unsafe {
                        BitBlt(screen_dc, 0, 0, width, height, mem_dc, 0, 0, SRCCOPY);
                    }
                }
            }
        }

        unsafe { EndPaint(hwnd, &ps) };
    }

    /// Offscreen render utility used to capture pixel-perfect visual verification frames.
    #[allow(dead_code)]
    pub fn render_companion_to_bmp(
        width: i32,
        height: i32,
        dpi: u32,
        expr: CompanionExpression,
        pulse: bool,
        openness: f32,
        gaze: (f32, f32),
        starting_step: u8,
    ) -> Result<Vec<u8>, String> {
        let _gdiplus = GdiplusSession::startup()?;
        let w = width.max(1);
        let h = height.max(1);

        unsafe {
            let mem_dc = CreateCompatibleDC(ptr::null_mut());
            if mem_dc.is_null() {
                return Err("Failed to create compatible DC".to_string());
            }

            let bmi = BITMAPINFO {
                bmi_header: BITMAPINFOHEADER {
                    bi_size: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    bi_width: w,
                    bi_height: -h,
                    bi_planes: 1,
                    bi_bit_count: 32,
                    bi_compression: 0,
                    bi_size_image: (w * h * 4) as u32,
                    bi_xpels_per_meter: 0,
                    bi_ypels_per_meter: 0,
                    bi_clr_used: 0,
                    bi_clr_important: 0,
                },
                bmi_colors: [RGBQUAD {
                    rgb_blue: 0,
                    rgb_green: 0,
                    rgb_red: 0,
                    rgb_reserved: 0,
                }],
            };

            let mut bits_ptr: *mut c_void = ptr::null_mut();
            let hbm = CreateDIBSection(mem_dc, &bmi, 0, &mut bits_ptr, ptr::null_mut(), 0);
            if hbm.is_null() || bits_ptr.is_null() {
                DeleteDC(mem_dc);
                return Err("Failed to create DIB section".to_string());
            }

            let old_bm = SelectObject(mem_dc, hbm);

            let mut g_ptr: *mut c_void = ptr::null_mut();
            if GdipCreateFromHDC(mem_dc, &mut g_ptr) != 0 || g_ptr.is_null() {
                SelectObject(mem_dc, old_bm);
                DeleteObject(hbm);
                DeleteDC(mem_dc);
                return Err("Failed to create GDI+ graphics".to_string());
            }
            let graphics = GdipGraphics(g_ptr);

            GdipSetSmoothingMode(graphics.0, SMOOTHING_MODE_ANTI_ALIAS);
            GdipSetPixelOffsetMode(graphics.0, PIXEL_OFFSET_MODE_HIGH_QUALITY);
            GdipGraphicsClear(graphics.0, 0xFF09_0B11);

            let geom = EyeGeometry::compute(w, h, dpi);
            if let Some(visor_path) = create_rounded_rect_path(
                geom.visor_x,
                geom.visor_y,
                geom.visor_w,
                geom.visor_h,
                geom.visor_radius,
            ) {
                if let Some(visor_fill) = create_solid_brush(0xFF10_1421) {
                    GdipFillPath(graphics.0, visor_fill.0, visor_path.0);
                }
                if let Some(visor_border) =
                    create_pen(0xFF1E_263C, (1.1 * geom.dpi_scale).max(1.0), false)
                {
                    GdipDrawPath(graphics.0, visor_border.0, visor_path.0);
                }
            }

            let palette = palette_for_expression(expr, pulse);

            draw_single_eye(
                graphics.0,
                geom.left_eye_cx,
                geom.eye_cy,
                geom.eye_w,
                geom.eye_h,
                geom.dpi_scale,
                openness,
                gaze,
                expr,
                starting_step,
                &palette,
                false,
            );
            draw_single_eye(
                graphics.0,
                geom.right_eye_cx,
                geom.eye_cy,
                geom.eye_w,
                geom.eye_h,
                geom.dpi_scale,
                openness,
                gaze,
                expr,
                starting_step,
                &palette,
                true,
            );

            let dot_r = (2.4 * geom.dpi_scale).max(2.0);
            let dot_x = geom.visor_x + geom.visor_w - (11.0 * geom.dpi_scale);
            let dot_y = geom.visor_y + geom.visor_h - (10.0 * geom.dpi_scale);
            let dot_color = match expr {
                CompanionExpression::Online
                | CompanionExpression::Neutral
                | CompanionExpression::Attentive
                | CompanionExpression::Curious
                | CompanionExpression::Thinking
                | CompanionExpression::Happy => 0xD010_B981,
                CompanionExpression::Error | CompanionExpression::Concerned => 0xE8EF_4444,
                CompanionExpression::Verifying
                | CompanionExpression::Focused
                | CompanionExpression::Starting
                | CompanionExpression::WakingUp => 0xCC06_B6D4,
                CompanionExpression::Resting => 0x6064_748B,
                CompanionExpression::Confused => 0xCCA8_55F7,
            };
            if let Some(dot_brush) = create_solid_brush(dot_color) {
                GdipFillEllipse(
                    graphics.0,
                    dot_brush.0,
                    dot_x - dot_r,
                    dot_y - dot_r,
                    dot_r * 2.0,
                    dot_r * 2.0,
                );
            }

            drop(graphics);

            let pixel_bytes = (w * h * 4) as usize;
            let mut out = Vec::with_capacity(14 + 40 + pixel_bytes);

            out.extend_from_slice(b"BM");
            let file_size = (14 + 40 + pixel_bytes) as u32;
            out.extend_from_slice(&file_size.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            let off_bits = 54u32;
            out.extend_from_slice(&off_bits.to_le_bytes());

            out.extend_from_slice(&40u32.to_le_bytes());
            out.extend_from_slice(&w.to_le_bytes());
            out.extend_from_slice(&(-h).to_le_bytes());
            out.extend_from_slice(&1u16.to_le_bytes());
            out.extend_from_slice(&32u16.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&(pixel_bytes as u32).to_le_bytes());
            out.extend_from_slice(&0i32.to_le_bytes());
            out.extend_from_slice(&0i32.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());

            let slice = std::slice::from_raw_parts(bits_ptr as *const u8, pixel_bytes);
            out.extend_from_slice(slice);

            SelectObject(mem_dc, old_bm);
            DeleteObject(hbm);
            DeleteDC(mem_dc);

            Ok(out)
        }
    }

    fn show_native_context_menu(hwnd: HWND, status_text: &str, app_handle: &tauri::AppHandle) {
        let to_wide =
            |s: &str| -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() };
        let status_w = to_wide(status_text);
        let open_w = to_wide("Open PICO");
        let quit_w = to_wide("Quit PICO");

        let mut pt = POINT::default();
        // SAFETY: Standard Win32 popup menu tied cleanly to `hwnd` and destroyed immediately.
        let selected_cmd = unsafe {
            let menu = CreatePopupMenu();
            if menu.is_null() {
                return;
            }
            AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status_w.as_ptr());
            AppendMenuW(menu, MF_SEPARATOR, 0, ptr::null());
            AppendMenuW(
                menu,
                MF_STRING,
                MENU_CMD_OPEN_PICO as usize,
                open_w.as_ptr(),
            );
            AppendMenuW(
                menu,
                MF_STRING,
                MENU_CMD_QUIT_PICO as usize,
                quit_w.as_ptr(),
            );

            let _ = GetCursorPos(&mut pt);
            let _ = SetForegroundWindow(hwnd);
            let cmd = TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                pt.x,
                pt.y,
                0,
                hwnd,
                ptr::null(),
            );
            let _ = DestroyMenu(menu);
            cmd
        };

        match selected_cmd {
            MENU_CMD_OPEN_PICO => {
                crate::open_or_focus_command_center(app_handle);
            }
            MENU_CMD_QUIT_PICO => {
                if let Some(lc) =
                    app_handle.try_state::<crate::window_lifecycle::CommandCenterLifecycle>()
                {
                    lc.mark_shutting_down();
                }
                if let Some(state) = app_handle.try_state::<crate::SupervisorState>() {
                    state.shutdown();
                }
                app_handle.exit(0);
            }
            _ => {}
        }
    }

    unsafe extern "system" fn companion_subclass_proc(
        hwnd: HWND,
        msg: UINT,
        wparam: WPARAM,
        lparam: LPARAM,
        u_id_subclass: usize,
        dw_ref_data: usize,
    ) -> LRESULT {
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if dw_ref_data == 0 {
                return DefSubclassProc(hwnd, msg, wparam, lparam);
            }

            match msg {
                WM_ERASEBKGND => 1,
                WM_PAINT => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    paint_companion(hwnd, ctx);
                    0
                }
                WM_APP_PICO_STATUS => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    ctx.sync_status();
                    ctx.reconcile_timers(hwnd);
                    InvalidateRect(hwnd, ptr::null(), 0);
                    0
                }
                WM_SHOWWINDOW => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    ctx.animation.visible = wparam != 0;
                    ctx.reconcile_timers(hwnd);
                    if ctx.animation.visible {
                        InvalidateRect(hwnd, ptr::null(), 0);
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_SIZE => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    ctx.animation.minimized = wparam == SIZE_MINIMIZED;
                    ctx.dpi = GetDpiForWindow(hwnd);
                    ctx.reconcile_timers(hwnd);
                    InvalidateRect(hwnd, ptr::null(), 0);
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_DPICHANGED => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    ctx.dpi = GetDpiForWindow(hwnd);
                    ctx.backbuffer = None;
                    InvalidateRect(hwnd, ptr::null(), 0);
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_SETCURSOR => {
                    if (lparam & 0xFFFF) as isize == HTCLIENT {
                        let cursor = LoadCursorW(ptr::null_mut(), IDC_ARROW as *const u16);
                        if !cursor.is_null() {
                            SetCursor(cursor);
                        }
                        return 1;
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_LBUTTONDOWN => {
                    let (x, y) = client_coords(lparam);
                    {
                        let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                        ctx.pressing = true;
                        ctx.down_x = x;
                        ctx.down_y = y;
                        ctx.animation.set_interactive_reaction(true);
                        invalidate_eyes_only(hwnd, ctx);
                    }
                    SetCapture(hwnd);
                    UpdateWindow(hwnd);
                    0
                }
                WM_MOUSEMOVE => {
                    let drag_breakout = {
                        let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                        if ctx.pressing {
                            let (x, y) = client_coords(lparam);
                            let dx = (x - ctx.down_x).abs();
                            let dy = (y - ctx.down_y).abs();
                            let drag_cx = GetSystemMetrics(SM_CXDRAG).max(4);
                            let drag_cy = GetSystemMetrics(SM_CYDRAG).max(4);
                            if dx >= drag_cx || dy >= drag_cy {
                                ctx.pressing = false;
                                ctx.animation.set_interactive_reaction(false);
                                ctx.reconcile_timers(hwnd);
                                invalidate_eyes_only(hwnd, ctx);
                                true
                            } else {
                                false
                            }
                        } else {
                            false
                        }
                    };

                    if drag_breakout {
                        // Scope has closed: all mutable borrows of CompanionWindowContext have ended.
                        // ReleaseCapture() will synchronously dispatch WM_CAPTURECHANGED, which can
                        // re-enter companion_subclass_proc and safely borrow its own mutable context.
                        ReleaseCapture();
                        let mut screen_pt = POINT::default();
                        let _ = GetCursorPos(&mut screen_pt);
                        let nc_lparam = ((screen_pt.y as i16 as u16 as isize) << 16)
                            | (screen_pt.x as i16 as u16 as isize);
                        SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, nc_lparam);
                        return 0;
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_LBUTTONUP => {
                    let action = {
                        let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                        if ctx.pressing {
                            let (x, y) = client_coords(lparam);
                            ctx.pressing = false;
                            ctx.animation.set_interactive_reaction(false);
                            ctx.reconcile_timers(hwnd);
                            invalidate_eyes_only(hwnd, ctx);

                            let mut rc = RECT::default();
                            let is_click_zone = if GetClientRect(hwnd, &mut rc) != 0 {
                                let geom = EyeGeometry::compute(
                                    rc.right - rc.left,
                                    rc.bottom - rc.top,
                                    ctx.dpi,
                                );
                                geom.is_in_eye_click_zone(x, y)
                            } else {
                                false
                            };

                            let handle = if is_click_zone {
                                Some(ctx.app_handle.clone())
                            } else {
                                None
                            };
                            Some(handle)
                        } else {
                            None
                        }
                    };

                    if let Some(maybe_handle) = action {
                        // Scope has closed: all mutable borrows of CompanionWindowContext have ended.
                        // ReleaseCapture() synchronously triggers WM_CAPTURECHANGED to hwnd.
                        // Re-entrant invocation of companion_subclass_proc acquires its own mutable
                        // context with zero aliasing or undefined behavior.
                        ReleaseCapture();

                        if let Some(app_handle) = maybe_handle {
                            crate::open_or_focus_command_center(&app_handle);
                        }
                        return 0;
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_RBUTTONUP => {
                    let (status_text, app_handle) = {
                        let ctx = &*(dw_ref_data as *const CompanionWindowContext);
                        let text = ctx
                            .status_label
                            .lock()
                            .map(|g| g.clone())
                            .unwrap_or_else(|_| "Status: Unknown".to_string());
                        (text, ctx.app_handle.clone())
                    };
                    show_native_context_menu(hwnd, &status_text, &app_handle);
                    0
                }
                WM_CAPTURECHANGED => {
                    {
                        let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                        let was_active = ctx.pressing || ctx.animation.interactive_reaction;
                        ctx.pressing = false;
                        ctx.animation.set_interactive_reaction(false);
                        ctx.reconcile_timers(hwnd);
                        if was_active {
                            invalidate_eyes_only(hwnd, ctx);
                        }
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_TIMER => {
                    let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                    match wparam {
                        TIMER_BLINK_SCHEDULE_ID => {
                            KillTimer(hwnd, TIMER_BLINK_SCHEDULE_ID);
                            ctx.timers.blink_schedule = false;
                            if ctx.animation.start_blink() {
                                ctx.reconcile_timers(hwnd);
                                invalidate_eyes_only(hwnd, ctx);
                            }
                        }
                        TIMER_BLINK_FRAME_ID => {
                            let ended = ctx.animation.advance_blink_frame();
                            if ended {
                                KillTimer(hwnd, TIMER_BLINK_FRAME_ID);
                                ctx.timers.blink_frames = false;
                                ctx.reconcile_timers(hwnd);
                            }
                            invalidate_eyes_only(hwnd, ctx);
                        }
                        TIMER_AMBIENT_GAZE_ID => {
                            ctx.animation.advance_ambient();
                            ctx.reconcile_timers(hwnd);
                            invalidate_eyes_only(hwnd, ctx);
                        }
                        _ => {}
                    }
                    0
                }
                WM_NCDESTROY => {
                    {
                        let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);
                        ctx.kill_all_timers(hwnd);
                        RemoveWindowSubclass(hwnd, companion_subclass_proc, u_id_subclass);
                        if let Some(coord) =
                            ctx.app_handle.try_state::<CompanionRenderCoordinator>()
                        {
                            coord.clear_hwnd();
                        }
                    }
                    let boxed = Box::from_raw(dw_ref_data as *mut CompanionWindowContext);
                    drop(boxed);
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                _ => DefSubclassProc(hwnd, msg, wparam, lparam),
            }
        }));

        match res {
            Ok(lres) => lres,
            Err(_) => {
                eprintln!(
                    "Recovered panic in PICO companion Win32 subclass proc (msg=0x{:x})",
                    msg
                );
                DefSubclassProc(hwnd, msg, wparam, lparam)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_expression_mapping_preserves_truthful_authoritative_state() {
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Unmanaged),
            CompanionExpression::Resting
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Starting),
            CompanionExpression::Starting
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Verifying { pid: 1234 }),
            CompanionExpression::Verifying
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Online { pid: 1234 }),
            CompanionExpression::Online
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Offline {
                reason: "Application shutdown".to_string()
            }),
            CompanionExpression::Resting
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Offline {
                reason: String::new()
            }),
            CompanionExpression::Resting
        );
        assert_eq!(
            expression_from_status(&BackendSupervisionStatus::Offline {
                reason: "Startup verification failed: timeout".to_string()
            }),
            CompanionExpression::Error
        );
    }

    #[test]
    fn test_blink_intervals_within_bounds_and_non_identical() {
        let mut prev = None;
        let mut distinct = std::collections::HashSet::new();
        for i in 0..20u32 {
            let interval = next_blink_interval_ms(i, prev);
            assert!(
                (BLINK_MIN_INTERVAL_MS..=BLINK_MAX_INTERVAL_MS).contains(&interval),
                "Interval {} out of [3500, 6500] bounds",
                interval
            );
            if let Some(p) = prev {
                assert_ne!(interval, p, "Consecutive blink intervals must differ");
            }
            distinct.insert(interval);
            prev = Some(interval);
        }
        assert!(distinct.len() >= 5, "Expected varied blink cadence");
    }

    #[test]
    fn test_blink_timeline_and_timer_suspension() {
        let mut anim = AnimationState::new(CompanionExpression::Online);
        let initial_schedule = anim.timer_schedule();
        assert!(initial_schedule.want_blink_schedule);
        assert!(!initial_schedule.want_blink_frames);
        assert_eq!(
            initial_schedule.ambient_interval_ms,
            Some(ONLINE_GAZE_INTERVAL_MS)
        );

        assert!(anim.start_blink());
        let active_blink = anim.timer_schedule();
        assert!(!active_blink.want_blink_schedule);
        assert!(active_blink.want_blink_frames);
        assert!(anim.eye_openness() < 1.0);

        // Advance through every frame until completion
        for _ in 0..(BLINK_CLOSURE_FRAMES.len() - 1) {
            assert!(!anim.advance_blink_frame());
        }
        assert!(anim.advance_blink_frame());
        assert_eq!(anim.blink_frame_idx, None);
        assert_eq!(anim.eye_openness(), 1.0);

        // Hidden or minimized stops all timers cleanly
        anim.visible = false;
        assert_eq!(
            anim.timer_schedule(),
            TimerSchedule {
                want_blink_schedule: false,
                want_blink_frames: false,
                ambient_interval_ms: None,
            }
        );
        anim.visible = true;
        anim.minimized = true;
        assert_eq!(
            anim.timer_schedule(),
            TimerSchedule {
                want_blink_schedule: false,
                want_blink_frames: false,
                ambient_interval_ms: None,
            }
        );
        anim.minimized = false;
        assert!(anim.timer_schedule().want_blink_schedule);
    }

    #[test]
    fn test_starting_transition_stops_ambient_timer_after_opening() {
        let mut anim = AnimationState::new(CompanionExpression::Starting);
        assert_eq!(
            anim.timer_schedule().ambient_interval_ms,
            Some(STARTING_STEP_INTERVAL_MS)
        );
        let first_open = anim.eye_openness();
        anim.advance_ambient();
        let second_open = anim.eye_openness();
        assert!(second_open > first_open);
        anim.advance_ambient();
        anim.advance_ambient();
        assert_eq!(anim.eye_openness(), 1.0);
        assert_eq!(anim.timer_schedule().ambient_interval_ms, None);
    }

    #[test]
    fn test_dpi_scaling_and_symmetrical_eye_geometry_bounds() {
        assert_eq!(scale_for_dpi(0), 1.0);
        assert_eq!(scale_for_dpi(96), 1.0);
        assert!((scale_for_dpi(120) - 1.25).abs() < 0.001);
        assert!((scale_for_dpi(192) - 2.0).abs() < 0.001);

        let geom_1x = EyeGeometry::compute(240, 120, 96);
        let geom_2x = EyeGeometry::compute(480, 240, 192);
        assert!((geom_1x.left_eye_cx + geom_1x.right_eye_cx - 240.0).abs() < 0.01);
        assert!(geom_1x.dirty_rect.left >= 0);
        assert!(geom_1x.dirty_rect.top >= 0);
        assert!(geom_1x.dirty_rect.right <= 240);
        assert!(geom_1x.dirty_rect.bottom <= 120);
        assert!(geom_1x.is_in_eye_click_zone(120, 60));
        assert!(!geom_1x.is_in_eye_click_zone(2, 2));
        assert!(geom_2x.eye_w > geom_1x.eye_w);
    }

    #[test]
    fn test_palette_and_coordinator_lifecycle_guards() {
        let resting = palette_for_expression(CompanionExpression::Resting, false);
        let online = palette_for_expression(CompanionExpression::Online, false);
        let err = palette_for_expression(CompanionExpression::Error, false);
        assert_ne!(online.rim_argb, err.rim_argb);
        assert_ne!(resting.rim_argb, online.rim_argb);

        let coord = CompanionRenderCoordinator::new(
            BackendSupervisionStatus::Unmanaged,
            "Status: Initializing".to_string(),
        );
        coord.update_status(
            &BackendSupervisionStatus::Online { pid: 4242 },
            "Status: Online (PID: 4242)".to_string(),
        );
        assert_eq!(
            *coord.status.lock().unwrap(),
            BackendSupervisionStatus::Online { pid: 4242 }
        );
        coord.clear_hwnd();
        assert_eq!(*coord.hwnd.lock().unwrap(), None);
    }

    #[test]
    fn test_resting_and_error_states_schedule_zero_timers() {
        for expr in [CompanionExpression::Resting, CompanionExpression::Error] {
            let mut anim = AnimationState::new(expr);
            assert_eq!(
                anim.timer_schedule(),
                TimerSchedule {
                    want_blink_schedule: false,
                    want_blink_frames: false,
                    ambient_interval_ms: None,
                }
            );
            assert!(!anim.start_blink());
        }
    }

    #[test]
    fn test_effective_expression_mapping_and_interaction() {
        let mut anim = AnimationState::new(CompanionExpression::Online);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);

        // Ambient micro-expression cycle during Online
        anim.gaze_step = 2;
        assert_eq!(anim.effective_expression(), CompanionExpression::Attentive);
        anim.gaze_step = 4;
        assert_eq!(anim.effective_expression(), CompanionExpression::Curious);
        anim.gaze_step = 6;
        assert_eq!(anim.effective_expression(), CompanionExpression::Thinking);
        anim.gaze_step = 7;
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);

        // Mouse click/down interactive tactile response
        anim.set_interactive_reaction(true);
        assert_eq!(anim.effective_expression(), CompanionExpression::Happy);
        anim.set_interactive_reaction(false);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);

        // Core supervisor state truthful mapping to presentation moods
        let starting_anim = AnimationState::new(CompanionExpression::Starting);
        assert_eq!(
            starting_anim.effective_expression(),
            CompanionExpression::WakingUp
        );

        let verifying_anim = AnimationState::new(CompanionExpression::Verifying);
        assert_eq!(
            verifying_anim.effective_expression(),
            CompanionExpression::Focused
        );

        let error_anim = AnimationState::new(CompanionExpression::Error);
        assert_eq!(
            error_anim.effective_expression(),
            CompanionExpression::Concerned
        );
    }

    #[test]
    fn test_emo_double_blink_lifecycle() {
        let mut anim = AnimationState::new(CompanionExpression::Online);
        anim.blink_count = 4; // Multiple of 4 triggers natural double-blink
        assert!(anim.start_blink());
        assert!(anim.is_double_blink);
        assert_eq!(anim.double_blink_stage, 0);

        // Primary blink frames (6 frames: [0.35, 0.82, 1.0, 0.65, 0.20, 0.0])
        for _ in 0..5 {
            assert!(!anim.advance_blink_frame());
        }
        // Transition from primary to secondary flutter
        assert!(!anim.advance_blink_frame());
        assert_eq!(anim.double_blink_stage, 1);
        assert_eq!(anim.blink_frame_idx, Some(0));

        // Secondary flutter frames (4 frames: [0.50, 0.92, 0.30, 0.0])
        for _ in 0..3 {
            assert!(!anim.advance_blink_frame());
        }
        // Completion of full double-blink
        assert!(anim.advance_blink_frame());
        assert_eq!(anim.blink_frame_idx, None);
        assert!(!anim.is_double_blink);
        assert_eq!(anim.double_blink_stage, 0);
        assert_eq!(anim.blink_count, 5);
    }

    #[test]
    fn test_emo_contour_parameters_and_asymmetry() {
        // Happy mood has strong cheek lift creating smiling crescent
        let happy = contour_params_for_expression(CompanionExpression::Happy, 1.0, 3, false);
        assert!(happy.cheek_lift >= 0.50);

        // Focused mood has inward brow slant and narrowed aperture
        let focused_l = contour_params_for_expression(CompanionExpression::Focused, 1.0, 3, false);
        assert!(focused_l.brow_slant > 0.10);
        assert!(focused_l.height_scale < 1.0);

        // Concerned mood has outer brow droop
        let concerned =
            contour_params_for_expression(CompanionExpression::Concerned, 1.0, 3, false);
        assert!(concerned.brow_slant < -0.10);

        // Curious mood has subtle asymmetry between left and right eyes
        let curious_l = contour_params_for_expression(CompanionExpression::Curious, 1.0, 3, false);
        let curious_r = contour_params_for_expression(CompanionExpression::Curious, 1.0, 3, true);
        assert!(curious_l.width_scale > curious_r.width_scale);
        assert!(curious_l.upper_arch > curious_r.upper_arch);

        // Confused mood has opposite brow angles between left and right eyes
        let confused_l =
            contour_params_for_expression(CompanionExpression::Confused, 1.0, 3, false);
        let confused_r = contour_params_for_expression(CompanionExpression::Confused, 1.0, 3, true);
        assert!(confused_l.brow_slant < 0.0);
        assert!(confused_r.brow_slant > 0.0);
    }

    #[test]
    fn test_gaze_saccade_bounds_and_coverage() {
        assert_eq!(CALM_GAZE_OFFSETS.len(), 8);
        for &(gx, gy) in &CALM_GAZE_OFFSETS {
            assert!(gx.abs() <= 0.25, "Gaze X {} exceeds restrained bounds", gx);
            assert!(gy.abs() <= 0.20, "Gaze Y {} exceeds restrained bounds", gy);
        }
    }

    #[test]
    fn test_all_expression_palettes_valid_and_distinct() {
        let expressions = [
            CompanionExpression::Resting,
            CompanionExpression::WakingUp,
            CompanionExpression::Neutral,
            CompanionExpression::Attentive,
            CompanionExpression::Focused,
            CompanionExpression::Thinking,
            CompanionExpression::Curious,
            CompanionExpression::Happy,
            CompanionExpression::Confused,
            CompanionExpression::Concerned,
            CompanionExpression::Error,
        ];

        for &expr in &expressions {
            let p = palette_for_expression(expr, false);
            assert_ne!(p.rim_argb, 0, "Rim ARGB must be non-zero for {:?}", expr);
            assert_ne!(
                p.iris_core_argb, 0,
                "Iris core ARGB must be non-zero for {:?}",
                expr
            );
            assert_ne!(
                p.highlight_argb, 0,
                "Highlight ARGB must be non-zero for {:?}",
                expr
            );

            // Error and Concerned use warm amber warning colors
            if matches!(
                expr,
                CompanionExpression::Error | CompanionExpression::Concerned
            ) {
                assert_eq!(
                    p.rim_argb & 0x00FF_0000,
                    0x00F5_0000,
                    "Expected amber rim for {:?}",
                    expr
                );
            } else {
                // PICO signature cyan/teal
                assert_ne!(
                    p.rim_argb & 0x0000_FFFF,
                    0,
                    "Expected cyan/teal rim for {:?}",
                    expr
                );
            }
        }
    }

    #[test]
    #[cfg(windows)]
    fn test_render_and_export_all_expression_screenshots() {
        use std::fs;
        use std::path::PathBuf;

        let candidates = [
            PathBuf::from("docs/evaluation/screenshots"),
            PathBuf::from("../../docs/evaluation/screenshots"),
        ];
        let target_dir = candidates
            .into_iter()
            .find(|p| p.exists())
            .unwrap_or_else(|| PathBuf::from("docs/evaluation/screenshots"));

        let export_files = std::env::var("PICO_EXPORT_SCREENSHOTS").is_ok();
        if export_files {
            fs::create_dir_all(&target_dir).unwrap_or_else(|e| {
                panic!(
                    "Failed to create screenshot export directory {}: {}",
                    target_dir.display(),
                    e
                )
            });
        }

        let cases = [
            (
                "01_neutral",
                CompanionExpression::Neutral,
                false,
                1.0,
                (0.0, 0.0),
                3,
            ),
            (
                "02_blink",
                CompanionExpression::Online,
                false,
                0.35,
                (0.0, 0.0),
                3,
            ),
            (
                "03_focused_verifying",
                CompanionExpression::Focused,
                true,
                0.86,
                (0.0, 0.0),
                3,
            ),
            (
                "04_waking_up_starting",
                CompanionExpression::WakingUp,
                false,
                0.52,
                (0.0, 0.0),
                1,
            ),
            (
                "05_curious",
                CompanionExpression::Curious,
                false,
                1.02,
                (0.16, -0.08),
                3,
            ),
            (
                "06_attentive",
                CompanionExpression::Attentive,
                false,
                1.08,
                (0.0, -0.03),
                3,
            ),
            (
                "07_happy",
                CompanionExpression::Happy,
                false,
                0.80,
                (0.0, -0.04),
                3,
            ),
            (
                "08_confused",
                CompanionExpression::Confused,
                false,
                0.94,
                (-0.08, 0.04),
                3,
            ),
            (
                "09_concerned_error",
                CompanionExpression::Concerned,
                false,
                0.68,
                (0.0, 0.0),
                3,
            ),
            (
                "10_resting",
                CompanionExpression::Resting,
                false,
                0.0,
                (0.0, 0.0),
                3,
            ),
            (
                "11_thinking",
                CompanionExpression::Thinking,
                false,
                0.88,
                (-0.14, -0.10),
                3,
            ),
        ];

        for (name, expr, pulse, openness, gaze, step) in cases {
            let bmp_bytes =
                win32::render_companion_to_bmp(240, 120, 96, expr, pulse, openness, gaze, step)
                    .expect("Render to BMP must succeed");

            assert!(!bmp_bytes.is_empty());
            assert_eq!(&bmp_bytes[0..2], b"BM");

            if export_files {
                let out_file = target_dir.join(format!("{}.bmp", name));
                fs::write(&out_file, &bmp_bytes).unwrap_or_else(|e| {
                    panic!(
                        "Failed to write screenshot export file {}: {}",
                        out_file.display(),
                        e
                    )
                });
                assert!(
                    out_file.exists(),
                    "Exported screenshot file {} does not exist after writing",
                    out_file.display()
                );
            }
        }
    }

    #[test]
    fn test_status_dot_reflects_authoritative_state_not_interactive_reaction() {
        let mut anim = AnimationState::new(CompanionExpression::Error);
        assert_eq!(anim.expression, CompanionExpression::Error);
        assert_eq!(anim.effective_expression(), CompanionExpression::Concerned);

        // Click-induced Happy reaction
        anim.set_interactive_reaction(true);
        assert_eq!(anim.effective_expression(), CompanionExpression::Happy);
        // Authoritative expression remains unchanged
        assert_eq!(anim.expression, CompanionExpression::Error);

        // Clear interaction
        anim.set_interactive_reaction(false);
        assert_eq!(anim.expression, CompanionExpression::Error);
        assert_eq!(anim.effective_expression(), CompanionExpression::Concerned);
    }

    #[test]
    fn test_capture_release_reentrancy_state_and_timer_invariants() {
        // 1. Initial Online idle state
        let mut anim = AnimationState::new(CompanionExpression::Online);
        assert_eq!(anim.expression, CompanionExpression::Online);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);
        let initial_sched = anim.timer_schedule();
        assert!(initial_sched.want_blink_schedule);
        assert!(!initial_sched.want_blink_frames);

        // 2. Mouse down: tactile Happy reaction triggers and clears active blink frame
        anim.start_blink();
        assert!(anim.blink_frame_idx.is_some());
        anim.set_interactive_reaction(true);
        assert_eq!(anim.effective_expression(), CompanionExpression::Happy);
        assert_eq!(
            anim.blink_frame_idx, None,
            "Happy reaction must clear in-progress blink"
        );
        let pressing_sched = anim.timer_schedule();
        assert!(!pressing_sched.want_blink_schedule);
        assert!(!pressing_sched.want_blink_frames);

        // 3. Normal mouse up: reaction cleared, timers reconciled
        anim.set_interactive_reaction(false);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);
        let released_sched = anim.timer_schedule();
        assert!(released_sched.want_blink_schedule);
        assert!(!released_sched.want_blink_frames);

        // 4. Re-entrant WM_CAPTURECHANGED arrival:
        // Calling set_interactive_reaction(false) a second time simulates re-entrant
        // WM_CAPTURECHANGED dispatch after ReleaseCapture(). Must be strictly idempotent.
        anim.set_interactive_reaction(false);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);
        let reentrant_sched = anim.timer_schedule();
        assert_eq!(released_sched, reentrant_sched);
        assert_eq!(
            anim.expression,
            CompanionExpression::Online,
            "Authoritative state must not change"
        );

        // 5. Unexpected capture loss (e.g. Alt+Tab or system dialog during click)
        anim.set_interactive_reaction(true);
        assert_eq!(anim.effective_expression(), CompanionExpression::Happy);
        // Direct WM_CAPTURECHANGED clears reaction without WM_LBUTTONUP
        anim.set_interactive_reaction(false);
        assert_eq!(anim.effective_expression(), CompanionExpression::Neutral);
        assert!(anim.timer_schedule().want_blink_schedule);
        assert_eq!(anim.expression, CompanionExpression::Online);
    }
}

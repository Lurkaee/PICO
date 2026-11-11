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

/// Authoritative visual expression derived strictly from `BackendSupervisionStatus`.
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

/// Discrete upper-eyelid closure sequence (0.0 = open, 1.0 = sealed).
const BLINK_CLOSURE_FRAMES: [f32; 6] = [0.40, 0.85, 1.0, 0.72, 0.30, 0.0];
/// Calm gaze micro-offsets in normalized iris units (small and restrained).
const CALM_GAZE_OFFSETS: [(f32, f32); 6] = [
    (0.0, 0.0),
    (-0.11, -0.02),
    (0.0, 0.0),
    (0.11, -0.02),
    (0.0, 0.03),
    (0.0, 0.0),
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
            starting_step: if initial == CompanionExpression::Starting {
                0
            } else {
                3
            },
            verifying_pulse_phase: false,
            gaze_step: 0,
        }
    }

    pub fn set_expression(&mut self, next: CompanionExpression) -> bool {
        if self.expression == next {
            return false;
        }
        self.expression = next;
        match next {
            CompanionExpression::Starting => {
                self.starting_step = 0;
                self.gaze_step = 0;
                self.verifying_pulse_phase = false;
            }
            CompanionExpression::Verifying => {
                self.starting_step = 3;
                self.verifying_pulse_phase = true;
                self.gaze_step = 0;
            }
            CompanionExpression::Online => {
                self.starting_step = 3;
                self.verifying_pulse_phase = false;
                self.gaze_step = 0;
            }
            CompanionExpression::Resting | CompanionExpression::Error => {
                self.blink_frame_idx = None;
                self.verifying_pulse_phase = false;
                self.gaze_step = 0;
            }
        }
        true
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
            self.expression,
            CompanionExpression::Online | CompanionExpression::Verifying
        );
        let blinking_now = can_blink && self.blink_frame_idx.is_some();

        let ambient_interval_ms = match self.expression {
            CompanionExpression::Starting if self.starting_step < 3 => {
                Some(STARTING_STEP_INTERVAL_MS)
            }
            CompanionExpression::Verifying => Some(VERIFYING_PULSE_INTERVAL_MS),
            CompanionExpression::Online => Some(ONLINE_GAZE_INTERVAL_MS),
            _ => None,
        };

        TimerSchedule {
            want_blink_schedule: can_blink && !blinking_now,
            want_blink_frames: blinking_now,
            ambient_interval_ms,
        }
    }

    /// Begins a single blink cycle cleanly.
    pub fn start_blink(&mut self) -> bool {
        if !self.is_render_active()
            || !matches!(
                self.expression,
                CompanionExpression::Online | CompanionExpression::Verifying
            )
        {
            return false;
        }
        self.blink_frame_idx = Some(0);
        true
    }

    /// Advances active blink by one frame; returns `true` if blink ended cleanly.
    pub fn advance_blink_frame(&mut self) -> bool {
        match self.blink_frame_idx {
            Some(idx) if idx + 1 < BLINK_CLOSURE_FRAMES.len() => {
                self.blink_frame_idx = Some(idx + 1);
                false
            }
            Some(_) => {
                self.blink_frame_idx = None;
                self.blink_count = self.blink_count.wrapping_add(1);
                let next_ms = next_blink_interval_ms(self.blink_count, self.last_blink_interval_ms);
                self.last_blink_interval_ms = Some(next_ms);
                true
            }
            None => true,
        }
    }

    /// Advances ambient low-frequency state step (`Starting`, `Verifying`, `Online`).
    pub fn advance_ambient(&mut self) {
        match self.expression {
            CompanionExpression::Starting => {
                if self.starting_step < 3 {
                    self.starting_step += 1;
                }
            }
            CompanionExpression::Verifying => {
                self.verifying_pulse_phase = !self.verifying_pulse_phase;
            }
            CompanionExpression::Online => {
                self.gaze_step = (self.gaze_step + 1) % CALM_GAZE_OFFSETS.len();
            }
            CompanionExpression::Resting | CompanionExpression::Error => {}
        }
    }

    /// Returns normalized eyelid openness in [0.0, 1.0].
    pub fn eye_openness(&self) -> f32 {
        let base = match self.expression {
            CompanionExpression::Resting => 0.0,
            CompanionExpression::Starting => match self.starting_step {
                0 => 0.22,
                1 => 0.52,
                2 => 0.80,
                _ => 1.0,
            },
            CompanionExpression::Verifying => 0.86,
            CompanionExpression::Online => 1.0,
            CompanionExpression::Error => 0.68,
        };
        let closure = self
            .blink_frame_idx
            .and_then(|idx| BLINK_CLOSURE_FRAMES.get(idx).copied())
            .unwrap_or(0.0);
        (base * (1.0 - closure)).clamp(0.0, 1.0)
    }

    /// Returns restrained normalized gaze offset `(gx, gy)` for iris placement.
    pub fn gaze_offset(&self) -> (f32, f32) {
        match self.expression {
            CompanionExpression::Online => CALM_GAZE_OFFSETS[self.gaze_step],
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
        CompanionExpression::Starting => ExpressionPalette {
            halo_argb: 0x1E06B6D4,
            rim_argb: 0xBA22D3EE,
            iris_outer_argb: 0xD00891B2,
            iris_core_argb: 0xE622D3EE,
            highlight_argb: 0xB8E0F7FA,
            resting_lid_argb: 0xB038BDF8,
        },
        CompanionExpression::Verifying => {
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
        CompanionExpression::Online => ExpressionPalette {
            halo_argb: 0x2406B6D4,
            rim_argb: 0xEB22D3EE,
            iris_outer_argb: 0xEA0891B2,
            iris_core_argb: 0xF538BDF8,
            highlight_argb: 0xE8ECFEFF,
            resting_lid_argb: 0xCC38BDF8,
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
        palette: &ExpressionPalette,
        is_right_eye: bool,
    ) {
        // 1. Resting / closed contour when aperture is essentially shut
        if openness <= 0.07 {
            let line_w = eye_w * 0.86;
            let arc_h = eye_h * 0.24;
            let pen_w = (2.1 * dpi_scale).max(1.5);
            if let Some(lid_pen) = create_pen(palette.resting_lid_argb, pen_w, true) {
                // Soft lower-lidded curve for calm resting presence
                unsafe {
                    GdipDrawArc(
                        graphics,
                        lid_pen.0,
                        cx - line_w * 0.5,
                        cy - arc_h * 0.2,
                        line_w,
                        arc_h * 2.0,
                        20.0,
                        140.0,
                    );
                }
            }
            return;
        }

        let visible_h = (eye_h * openness).max(6.0);
        let top = cy - visible_h * 0.5;
        let left = cx - eye_w * 0.5;
        let corner_r = (eye_w * 0.42).min(visible_h * 0.46);

        // 2. Subtle outer ambient glow halo (soft vector radial gradient)
        let halo_w = eye_w * 1.48;
        let halo_h = visible_h * 1.48;
        let mut halo_path_ptr: *mut c_void = ptr::null_mut();
        unsafe {
            if GdipCreatePath(FILL_MODE_ALTERNATE, &mut halo_path_ptr) == 0
                && !halo_path_ptr.is_null()
            {
                let halo_path = GdipPath(halo_path_ptr);
                GdipAddPathEllipse(
                    halo_path.0,
                    cx - halo_w * 0.5,
                    cy - halo_h * 0.5,
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
                        cy - halo_h * 0.5,
                        halo_w,
                        halo_h,
                    );
                }
            }
        }

        // 3. Eye aperture shape (rounded PICO capsule)
        if let Some(eye_path) = create_rounded_rect_path(left, top, eye_w, visible_h, corner_r) {
            if let Some(cavity_brush) = create_solid_brush(0xFF0A_1320) {
                unsafe { GdipFillPath(graphics, cavity_brush.0, eye_path.0) };
            }

            // Clip iris & catchlights cleanly inside the eye aperture
            unsafe {
                GdipSetClipPath(graphics, eye_path.0, COMBINE_MODE_INTERSECT);
            }

            let iris_d = (eye_w * 0.72).min(eye_h * 0.72);
            let gx = gaze.0 * eye_w * 0.35;
            let gy = gaze.1 * eye_h * 0.25;
            let iris_x = cx + gx - iris_d * 0.5;
            let iris_y = cy + gy - iris_d * 0.5;

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
                        cy + gy - core_d * 0.5,
                        core_d,
                        core_d,
                    );
                }
            }

            // Restrained inner iris precision ring (accented in Verifying/Online)
            let ring_d = iris_d * 0.84;
            let ring_argb = if expr == CompanionExpression::Verifying {
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
                        cy + gy - ring_d * 0.5,
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
                        cy + gy - pupil_d * 0.5,
                        pupil_d,
                        pupil_d,
                    );
                }
            }

            // Soft dual specular catchlights for warm optical depth
            let spec_big = (iris_d * 0.20).max(2.2);
            let spec_small = (iris_d * 0.09).max(1.2);
            if let Some(spec_brush) = create_solid_brush(palette.highlight_argb) {
                unsafe {
                    GdipFillEllipse(
                        graphics,
                        spec_brush.0,
                        cx + gx - iris_d * 0.22 - spec_big * 0.5,
                        cy + gy - iris_d * 0.20 - spec_big * 0.5,
                        spec_big,
                        spec_big,
                    );
                    GdipFillEllipse(
                        graphics,
                        spec_brush.0,
                        cx + gx + iris_d * 0.16,
                        cy + gy + iris_d * 0.14,
                        spec_small,
                        spec_small,
                    );
                }
            }

            unsafe { GdipResetClip(graphics) };

            // Outer glowing vector contour
            let rim_width = (1.6 * dpi_scale).max(1.25);
            if let Some(rim_pen) = create_pen(palette.rim_argb, rim_width, true) {
                unsafe { GdipDrawPath(graphics, rim_pen.0, eye_path.0) };
            }

            // Gentle authoritative warning brow angle strictly on Error state
            if expr == CompanionExpression::Error {
                let brow_pen_w = (1.8 * dpi_scale).max(1.4);
                if let Some(brow_pen) = create_pen(palette.rim_argb, brow_pen_w, true) {
                    let y_outer = top - 2.0 * dpi_scale;
                    let y_inner = top + 3.0 * dpi_scale;
                    let (x1, y1, x2, y2) = if is_right_eye {
                        (left + eye_w * 0.08, y_inner, left + eye_w * 0.92, y_outer)
                    } else {
                        (left + eye_w * 0.08, y_outer, left + eye_w * 0.92, y_inner)
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

                    let palette = palette_for_expression(
                        ctx.animation.expression,
                        ctx.animation.verifying_pulse_phase,
                    );
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
                        ctx.animation.expression,
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
                        ctx.animation.expression,
                        &palette,
                        true,
                    );

                    // Subtle bottom corner status dot (small restrained cue mirroring Online/Error)
                    let dot_r = (2.4 * geom.dpi_scale).max(2.0);
                    let dot_x = geom.visor_x + geom.visor_w - (11.0 * geom.dpi_scale);
                    let dot_y = geom.visor_y + geom.visor_h - (10.0 * geom.dpi_scale);
                    let dot_color = match ctx.animation.expression {
                        CompanionExpression::Online => 0xD010_B981,
                        CompanionExpression::Error => 0xE8EF_4444,
                        CompanionExpression::Verifying | CompanionExpression::Starting => {
                            0xCC06_B6D4
                        }
                        CompanionExpression::Resting => 0x6064_748B,
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

    fn show_native_context_menu(hwnd: HWND, ctx: &CompanionWindowContext) {
        let status_text = ctx
            .status_label
            .lock()
            .map(|g| g.clone())
            .unwrap_or_else(|_| "Status: Unknown".to_string());

        let to_wide =
            |s: &str| -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() };
        let status_w = to_wide(&status_text);
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
                crate::open_or_focus_command_center(&ctx.app_handle);
            }
            MENU_CMD_QUIT_PICO => {
                if let Some(lc) = ctx
                    .app_handle
                    .try_state::<crate::window_lifecycle::CommandCenterLifecycle>()
                {
                    lc.mark_shutting_down();
                }
                if let Some(state) = ctx.app_handle.try_state::<crate::SupervisorState>() {
                    state.shutdown();
                }
                ctx.app_handle.exit(0);
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
            let ctx = &mut *(dw_ref_data as *mut CompanionWindowContext);

            match msg {
                WM_ERASEBKGND => 1,
                WM_PAINT => {
                    paint_companion(hwnd, ctx);
                    0
                }
                WM_APP_PICO_STATUS => {
                    ctx.sync_status();
                    ctx.reconcile_timers(hwnd);
                    InvalidateRect(hwnd, ptr::null(), 0);
                    0
                }
                WM_SHOWWINDOW => {
                    ctx.animation.visible = wparam != 0;
                    ctx.reconcile_timers(hwnd);
                    if ctx.animation.visible {
                        InvalidateRect(hwnd, ptr::null(), 0);
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_SIZE => {
                    ctx.animation.minimized = wparam == SIZE_MINIMIZED;
                    ctx.dpi = GetDpiForWindow(hwnd);
                    ctx.reconcile_timers(hwnd);
                    InvalidateRect(hwnd, ptr::null(), 0);
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_DPICHANGED => {
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
                    ctx.pressing = true;
                    ctx.down_x = x;
                    ctx.down_y = y;
                    SetCapture(hwnd);
                    0
                }
                WM_MOUSEMOVE => {
                    if ctx.pressing {
                        let (x, y) = client_coords(lparam);
                        let dx = (x - ctx.down_x).abs();
                        let dy = (y - ctx.down_y).abs();
                        let drag_cx = GetSystemMetrics(SM_CXDRAG).max(4);
                        let drag_cy = GetSystemMetrics(SM_CYDRAG).max(4);
                        if dx >= drag_cx || dy >= drag_cy {
                            ctx.pressing = false;
                            ReleaseCapture();
                            let mut screen_pt = POINT::default();
                            let _ = GetCursorPos(&mut screen_pt);
                            let nc_lparam = ((screen_pt.y as i16 as u16 as isize) << 16)
                                | (screen_pt.x as i16 as u16 as isize);
                            SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, nc_lparam);
                            return 0;
                        }
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_LBUTTONUP => {
                    if ctx.pressing {
                        let (x, y) = client_coords(lparam);
                        ctx.pressing = false;
                        ReleaseCapture();
                        let mut rc = RECT::default();
                        if GetClientRect(hwnd, &mut rc) != 0 {
                            let geom = EyeGeometry::compute(
                                rc.right - rc.left,
                                rc.bottom - rc.top,
                                ctx.dpi,
                            );
                            if geom.is_in_eye_click_zone(x, y) {
                                crate::open_or_focus_command_center(&ctx.app_handle);
                            }
                        }
                        return 0;
                    }
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_RBUTTONUP => {
                    show_native_context_menu(hwnd, ctx);
                    0
                }
                WM_CAPTURECHANGED => {
                    ctx.pressing = false;
                    DefSubclassProc(hwnd, msg, wparam, lparam)
                }
                WM_TIMER => {
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
                    ctx.kill_all_timers(hwnd);
                    RemoveWindowSubclass(hwnd, companion_subclass_proc, u_id_subclass);
                    if let Some(coord) = ctx.app_handle.try_state::<CompanionRenderCoordinator>() {
                        coord.clear_hwnd();
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
}

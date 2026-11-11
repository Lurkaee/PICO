//! Race-safe Command Center & Native Companion Lifecycle Abstraction (P11.2.4.1C).
//!
//! Isolates Tauri `WebviewWindowBuilder::build()` and `destroy()` behind a strict,
//! state-machine-guarded (`Closed` -> `Opening` -> `Open` -> `Closing`) dedicated-thread
//! execution model using cloned `AppHandle`s.
//!
//! Per locked `tauri 2.12.5` + `tauri-runtime-wry 2.12.3` Windows behavior, calling
//! `WebviewWindowBuilder::build()` from the main thread (including via `run_on_main_thread`)
//! routes `send_user_message` synchronously inline on the UI thread, risking Windows
//! WebView2 deadlocks (`wry#583`). Running `build()` and `destroy()` on separate
//! background threads routes messages through `EventLoopProxy::send_event` while keeping
//! the main UI message pump unblocked.

use std::sync::Mutex;
use tauri::{AppHandle, Manager};

/// Authoritative lifecycle state of the deferred React Command Center WebView.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandCenterState {
    Closed,
    Opening,
    Open,
    Closing,
}

/// Manages serialized, deadlock-free creation and destruction of the Command Center WebviewWindow.
pub struct CommandCenterLifecycle {
    pub state: Mutex<CommandCenterState>,
    pub pending_reopen: Mutex<bool>,
    pub shutting_down: Mutex<bool>,
}

impl CommandCenterLifecycle {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(CommandCenterState::Closed),
            pending_reopen: Mutex::new(false),
            shutting_down: Mutex::new(false),
        }
    }

    pub fn current_state(&self) -> CommandCenterState {
        *self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn mark_shutting_down(&self) {
        if let Ok(mut guard) = self.shutting_down.lock() {
            *guard = true;
        }
        if let Ok(mut pending) = self.pending_reopen.lock() {
            *pending = false;
        }
    }

    pub fn is_shutting_down(&self) -> bool {
        *self.shutting_down.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Requests opening or focusing the Command Center on a separate background thread
    /// using a cloned `AppHandle`, per Tauri 2.12.5 Windows WebView2 creation guidance.
    /// Never calls `WebviewWindowBuilder::build()` synchronously inside an event callback
    /// or inside `app.run_on_main_thread`.
    pub fn request_open(&self, app: &AppHandle) {
        if self.is_shutting_down() {
            return;
        }

        let mut state_guard = match self.state.lock() {
            Ok(g) => g,
            Err(_) => return,
        };

        match *state_guard {
            CommandCenterState::Open => {
                drop(state_guard);
                // Already open: safely dispatch focus off the callback thread
                let app_clone = app.clone();
                let _ = std::thread::Builder::new()
                    .name("pico-webview-focus".into())
                    .spawn(move || {
                        if let Some(win) = app_clone.get_webview_window("main") {
                            let _ = win.unminimize();
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    });
            }
            CommandCenterState::Opening => {
                // Creation already in flight; ignore duplicate open request
            }
            CommandCenterState::Closing => {
                // Destruction in flight; mark pending reopen so it activates cleanly once Closed
                if let Ok(mut pending) = self.pending_reopen.lock() {
                    *pending = true;
                }
            }
            CommandCenterState::Closed => {
                *state_guard = CommandCenterState::Opening;
                drop(state_guard);

                let app_clone = app.clone();
                // Create on a dedicated non-UI OS thread using cloned AppHandle so
                // tauri-runtime-wry sends Message::CreateWindow via EventLoopProxy
                // without blocking or executing synchronously on main_thread_id.
                let spawn_res = std::thread::Builder::new()
                    .name("pico-webview-open".into())
                    .spawn(move || {
                        let lifecycle = app_clone.try_state::<CommandCenterLifecycle>();
                        if let Some(ref lc) = lifecycle {
                            if lc.is_shutting_down() {
                                lc.transition_to_closed();
                                return;
                            }
                            if lc.current_state() != CommandCenterState::Opening {
                                return;
                            }
                        }

                        if let Some(existing) = app_clone.get_webview_window("main") {
                            let _ = existing.unminimize();
                            let _ = existing.show();
                            let _ = existing.set_focus();
                            if let Some(lc) = lifecycle {
                                lc.transition_to_open();
                            }
                            return;
                        }

                        let builder = tauri::WebviewWindowBuilder::new(
                            &app_clone,
                            "main",
                            tauri::WebviewUrl::App("index.html".into()),
                        )
                        .title("PICO")
                        .inner_size(1024.0, 720.0)
                        .min_inner_size(480.0, 400.0)
                        .center()
                        .resizable(true)
                        .decorations(false)
                        .transparent(true)
                        .shadow(true)
                        .visible(true);

                        match builder.build() {
                            Ok(win) => {
                                let should_abort = lifecycle
                                    .as_ref()
                                    .map(|lc| lc.complete_build_or_abort())
                                    .unwrap_or(false);
                                if should_abort {
                                    let _ = win.destroy();
                                }
                            }
                            Err(e) => {
                                eprintln!(
                                    "Failed to instantiate Command Center WebviewWindow: {}",
                                    e
                                );
                                if let Some(lc) = lifecycle {
                                    lc.transition_to_closed();
                                }
                            }
                        }
                    });

                if let Err(e) = spawn_res {
                    eprintln!("Failed to spawn Command Center open thread: {}", e);
                    self.transition_to_closed();
                }
            }
        }
    }

    /// Requests destruction of the Command Center WebviewWindow on a dedicated thread,
    /// releasing WebView2 processes cleanly without blocking UI callbacks.
    pub fn request_close(&self, app: &AppHandle) {
        let mut state_guard = match self.state.lock() {
            Ok(g) => g,
            Err(_) => return,
        };

        let prev_state = *state_guard;
        if prev_state == CommandCenterState::Closed || prev_state == CommandCenterState::Closing {
            return;
        }

        *state_guard = CommandCenterState::Closing;
        drop(state_guard);

        let app_clone = app.clone();
        let spawn_res = std::thread::Builder::new()
            .name("pico-webview-close".into())
            .spawn(move || {
                if let Some(win) = app_clone.get_webview_window("main") {
                    if let Err(e) = win.destroy() {
                        eprintln!("Failed to destroy Command Center WebviewWindow: {}", e);
                        if let Some(lc) = app_clone.try_state::<CommandCenterLifecycle>() {
                            lc.on_destroyed(&app_clone);
                        }
                    }
                } else if prev_state != CommandCenterState::Opening {
                    // Window already gone; ensure state returns to Closed cleanly.
                    // (If prev_state == Opening, the in-flight open thread sees Closing and destroys it.)
                    if let Some(lc) = app_clone.try_state::<CommandCenterLifecycle>() {
                        lc.on_destroyed(&app_clone);
                    }
                }
            });

        if let Err(e) = spawn_res {
            eprintln!("Failed to spawn Command Center close thread: {}", e);
            if let Some(win) = app.get_webview_window("main") {
                if win.destroy().is_err() {
                    self.transition_to_closed();
                }
            } else {
                self.transition_to_closed();
            }
        }
    }

    /// Finalizes `Opening` -> `Open` after `WebviewWindowBuilder::build()` succeeds,
    /// returning `true` if a close or shutdown race occurred mid-build so the caller
    /// must immediately destroy the newly created window.
    pub fn complete_build_or_abort(&self) -> bool {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if self.is_shutting_down() || *guard == CommandCenterState::Closing {
            true
        } else {
            *guard = CommandCenterState::Open;
            false
        }
    }

    pub fn transition_to_open(&self) {
        if let Ok(mut guard) = self.state.lock() {
            *guard = CommandCenterState::Open;
        }
    }

    pub fn transition_to_closed(&self) {
        if let Ok(mut guard) = self.state.lock() {
            *guard = CommandCenterState::Closed;
        }
    }

    /// Called when Tauri notifies `WindowEvent::Destroyed` for label `"main"`.
    pub fn on_destroyed(&self, app: &AppHandle) {
        self.transition_to_closed();
        let should_reopen = {
            if let Ok(mut pending) = self.pending_reopen.lock() {
                let val = *pending;
                *pending = false;
                val
            } else {
                false
            }
        };

        if should_reopen && !self.is_shutting_down() {
            self.request_open(app);
        }
    }
}

impl Default for CommandCenterLifecycle {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_command_center_lifecycle_initial_state() {
        let lc = CommandCenterLifecycle::new();
        assert_eq!(lc.current_state(), CommandCenterState::Closed);
        assert!(!lc.is_shutting_down());
    }

    #[test]
    fn test_command_center_state_transitions_and_shutdown() {
        let lc = CommandCenterLifecycle::new();
        assert_eq!(lc.current_state(), CommandCenterState::Closed);

        lc.transition_to_open();
        assert_eq!(lc.current_state(), CommandCenterState::Open);

        lc.transition_to_closed();
        assert_eq!(lc.current_state(), CommandCenterState::Closed);

        lc.mark_shutting_down();
        assert!(lc.is_shutting_down());
    }

    #[test]
    fn test_command_center_pending_reopen_cleared_on_shutdown() {
        let lc = CommandCenterLifecycle::new();
        {
            let mut p = lc.pending_reopen.lock().unwrap();
            *p = true;
        }
        lc.mark_shutting_down();
        let p = lc.pending_reopen.lock().unwrap();
        assert!(!*p, "Pending reopen flag must be cleared on application shutdown");
    }

    #[test]
    fn test_command_center_complete_build_or_abort_guards() {
        let lc = CommandCenterLifecycle::new();
        {
            let mut s = lc.state.lock().unwrap();
            *s = CommandCenterState::Opening;
        }
        assert!(!lc.complete_build_or_abort());
        assert_eq!(lc.current_state(), CommandCenterState::Open);

        {
            let mut s = lc.state.lock().unwrap();
            *s = CommandCenterState::Closing;
        }
        assert!(lc.complete_build_or_abort());
        assert_eq!(lc.current_state(), CommandCenterState::Closing);

        let lc_shutdown = CommandCenterLifecycle::new();
        {
            let mut s = lc_shutdown.state.lock().unwrap();
            *s = CommandCenterState::Opening;
        }
        lc_shutdown.mark_shutting_down();
        assert!(lc_shutdown.complete_build_or_abort());
    }
}

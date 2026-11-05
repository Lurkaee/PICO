use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

/// High-level actions corresponding to system tray menu items.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayAction {
    Status,
    OpenUi,
    PausePerception,
    Settings,
    Quit,
}

impl TrayAction {
    /// Maps a string menu identifier to its typed `TrayAction`.
    pub fn from_id(id: &str) -> Option<Self> {
        match id {
            "status" => Some(Self::Status),
            "open_ui" => Some(Self::OpenUi),
            "pause_perception" => Some(Self::PausePerception),
            "settings" => Some(Self::Settings),
            "quit" => Some(Self::Quit),
            _ => None,
        }
    }

    /// Returns the static string identifier associated with the tray action.
    pub fn id(&self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::OpenUi => "open_ui",
            Self::PausePerception => "pause_perception",
            Self::Settings => "settings",
            Self::Quit => "quit",
        }
    }

    /// Returns whether the menu item is enabled in the current P11.2.1 milestone.
    pub fn is_enabled(&self) -> bool {
        matches!(self, Self::OpenUi | Self::Quit)
    }
}

/// Decisions for tray icon clicks based on current window state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayClickAction {
    /// Window is minimized: unminimize, ensure shown, and bring to focus.
    RestoreAndFocus,
    /// Window is visible and not minimized: hide to tray.
    Hide,
    /// Window is hidden: show, unminimize defensively, and bring to focus.
    ShowAndFocus,
}

/// Evaluates the appropriate window action when the system tray icon is clicked.
///
/// On Windows, `IsWindowVisible` returns true even when a window is minimized.
/// Therefore, `is_minimized` MUST be evaluated before `is_visible` so that
/// minimized windows are restored to the foreground rather than inadvertently hidden.
pub fn decide_tray_click_action(is_minimized: bool, is_visible: bool) -> TrayClickAction {
    if is_minimized {
        TrayClickAction::RestoreAndFocus
    } else if is_visible {
        TrayClickAction::Hide
    } else {
        TrayClickAction::ShowAndFocus
    }
}

/// Pure representation of side effects triggered by menu item selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MenuDispatchEffect {
    /// Informational or disabled items produce no side effects.
    NoAction,
    /// Restore, unminimize, and focus the primary companion window.
    OpenWindow,
    /// Terminate the companion process with the specified exit code.
    Exit(i32),
}

/// Evaluates the dispatch effect for a given `TrayAction`.
pub fn decide_menu_dispatch_effect(action: TrayAction) -> MenuDispatchEffect {
    match action {
        TrayAction::Status => MenuDispatchEffect::NoAction,
        TrayAction::OpenUi => MenuDispatchEffect::OpenWindow,
        TrayAction::PausePerception => MenuDispatchEffect::NoAction,
        TrayAction::Settings => MenuDispatchEffect::NoAction,
        TrayAction::Quit => MenuDispatchEffect::Exit(0),
    }
}

/// Policy describing how window close requests are handled by the native shell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CloseRequestPolicy {
    /// Whether the standard OS window destroy operation is intercepted and cancelled.
    pub prevent_close: bool,
    /// Whether the window should be hidden to the background system tray.
    pub hide_window: bool,
}

/// Evaluates the native window close policy.
///
/// Per P11.2.1 requirements, closing the window hides it to the system tray.
/// Explicit selection of 'Quit' from the tray menu remains the sole termination path.
pub fn evaluate_close_request_policy() -> CloseRequestPolicy {
    CloseRequestPolicy {
        prevent_close: true,
        hide_window: true,
    }
}

/// Initializes and launches the native Tauri desktop companion shell.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Build system tray menu items per ADR 0001
            let status_item = MenuItemBuilder::with_id(
                TrayAction::Status.id(),
                "Status: Offline (Bridge Deferred)",
            )
            .enabled(TrayAction::Status.is_enabled())
            .build(app)?;

            let open_item = MenuItemBuilder::with_id(TrayAction::OpenUi.id(), "Open PICO")
                .enabled(TrayAction::OpenUi.is_enabled())
                .build(app)?;

            let pause_item = MenuItemBuilder::with_id(
                TrayAction::PausePerception.id(),
                "Pause Perception (Disabled)",
            )
            .enabled(TrayAction::PausePerception.is_enabled())
            .build(app)?;

            let settings_item =
                MenuItemBuilder::with_id(TrayAction::Settings.id(), "Settings (Disabled)")
                    .enabled(TrayAction::Settings.is_enabled())
                    .build(app)?;

            let quit_item = MenuItemBuilder::with_id(TrayAction::Quit.id(), "Quit PICO")
                .enabled(TrayAction::Quit.is_enabled())
                .build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&status_item)
                .separator()
                .item(&open_item)
                .item(&pause_item)
                .item(&settings_item)
                .separator()
                .item(&quit_item)
                .build()?;

            let mut tray_builder = TrayIconBuilder::with_id("pico-tray")
                .tooltip("PICO Desktop Companion")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| {
                    if let Some(action) = TrayAction::from_id(event.id().as_ref()) {
                        match decide_menu_dispatch_effect(action) {
                            MenuDispatchEffect::OpenWindow => {
                                if let Some(window) = app.get_webview_window("main") {
                                    let _ = window.show();
                                    let _ = window.unminimize();
                                    let _ = window.set_focus();
                                }
                            }
                            MenuDispatchEffect::Exit(code) => {
                                app.exit(code);
                            }
                            MenuDispatchEffect::NoAction => {}
                        }
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let is_minimized = window.is_minimized().unwrap_or(false);
                            let is_visible = window.is_visible().unwrap_or(false);
                            match decide_tray_click_action(is_minimized, is_visible) {
                                TrayClickAction::RestoreAndFocus => {
                                    let _ = window.unminimize();
                                    let _ = window.show();
                                    let _ = window.set_focus();
                                }
                                TrayClickAction::Hide => {
                                    let _ = window.hide();
                                }
                                TrayClickAction::ShowAndFocus => {
                                    let _ = window.show();
                                    let _ = window.unminimize();
                                    let _ = window.set_focus();
                                }
                            }
                        }
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }

            let _tray = tray_builder.build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let policy = evaluate_close_request_policy();
                if policy.prevent_close {
                    api.prevent_close();
                }
                if policy.hide_window {
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running PICO desktop application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tray_action_from_id_valid() {
        assert_eq!(TrayAction::from_id("status"), Some(TrayAction::Status));
        assert_eq!(TrayAction::from_id("open_ui"), Some(TrayAction::OpenUi));
        assert_eq!(
            TrayAction::from_id("pause_perception"),
            Some(TrayAction::PausePerception)
        );
        assert_eq!(TrayAction::from_id("settings"), Some(TrayAction::Settings));
        assert_eq!(TrayAction::from_id("quit"), Some(TrayAction::Quit));
    }

    #[test]
    fn test_tray_action_from_id_invalid() {
        assert_eq!(TrayAction::from_id(""), None);
        assert_eq!(TrayAction::from_id("unknown"), None);
        assert_eq!(TrayAction::from_id("admin_exec"), None);
        assert_eq!(TrayAction::from_id("close_window"), None);
    }

    #[test]
    fn test_tray_action_round_trip() {
        let all_actions = [
            TrayAction::Status,
            TrayAction::OpenUi,
            TrayAction::PausePerception,
            TrayAction::Settings,
            TrayAction::Quit,
        ];
        for action in all_actions {
            assert_eq!(TrayAction::from_id(action.id()), Some(action));
        }
    }

    #[test]
    fn test_tray_action_enabled_state() {
        // Only interactive actions are enabled in P11.2.1
        assert!(TrayAction::OpenUi.is_enabled());
        assert!(TrayAction::Quit.is_enabled());

        // Deferred capabilities remain disabled
        assert!(!TrayAction::Status.is_enabled());
        assert!(!TrayAction::PausePerception.is_enabled());
        assert!(!TrayAction::Settings.is_enabled());
    }

    #[test]
    fn test_tray_click_action_minimized_restores_and_focuses() {
        // Windows IsWindowVisible returns true even when minimized.
        // Even if is_visible is reported true, a minimized window must be restored.
        assert_eq!(
            decide_tray_click_action(true, true),
            TrayClickAction::RestoreAndFocus
        );
        // Minimized but not reported visible must also be restored.
        assert_eq!(
            decide_tray_click_action(true, false),
            TrayClickAction::RestoreAndFocus
        );
    }

    #[test]
    fn test_tray_click_action_visible_not_minimized_hides() {
        // Non-minimized visible window must be hidden to the tray.
        assert_eq!(decide_tray_click_action(false, true), TrayClickAction::Hide);
    }

    #[test]
    fn test_tray_click_action_hidden_shows_and_focuses() {
        // Hidden window must be shown, unminimized defensively, and brought to focus.
        assert_eq!(
            decide_tray_click_action(false, false),
            TrayClickAction::ShowAndFocus
        );
    }

    #[test]
    fn test_menu_dispatch_effects() {
        // Pure decision logic for tray menu selections
        assert_eq!(
            decide_menu_dispatch_effect(TrayAction::Status),
            MenuDispatchEffect::NoAction
        );
        assert_eq!(
            decide_menu_dispatch_effect(TrayAction::OpenUi),
            MenuDispatchEffect::OpenWindow
        );
        assert_eq!(
            decide_menu_dispatch_effect(TrayAction::PausePerception),
            MenuDispatchEffect::NoAction
        );
        assert_eq!(
            decide_menu_dispatch_effect(TrayAction::Settings),
            MenuDispatchEffect::NoAction
        );
        assert_eq!(
            decide_menu_dispatch_effect(TrayAction::Quit),
            MenuDispatchEffect::Exit(0)
        );
    }

    #[test]
    fn test_close_request_policy_enforcement() {
        // Native close requests are intercepted: close prevented, window hidden to tray.
        // Explicit Quit via tray menu remains the sole exit path.
        let policy = evaluate_close_request_policy();
        assert!(policy.prevent_close);
        assert!(policy.hide_window);
    }
}

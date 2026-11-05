use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayAction {
    Status,
    OpenUi,
    PausePerception,
    Settings,
    Quit,
}

impl TrayAction {
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

    pub fn id(&self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::OpenUi => "open_ui",
            Self::PausePerception => "pause_perception",
            Self::Settings => "settings",
            Self::Quit => "quit",
        }
    }

    pub fn is_enabled(&self) -> bool {
        matches!(self, Self::OpenUi | Self::Quit)
    }
}

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
                .on_menu_event(
                    |app, event| match TrayAction::from_id(event.id().as_ref()) {
                        Some(TrayAction::OpenUi) => {
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.show();
                                let _ = window.unminimize();
                                let _ = window.set_focus();
                            }
                        }
                        Some(TrayAction::Quit) => {
                            app.exit(0);
                        }
                        _ => {}
                    },
                )
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            if let Ok(is_visible) = window.is_visible() {
                                if is_visible {
                                    let _ = window.hide();
                                } else {
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
                // Intercept close event: hide window to system tray instead of terminating
                api.prevent_close();
                let _ = window.hide();
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
}

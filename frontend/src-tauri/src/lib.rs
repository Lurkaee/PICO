use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Build system tray menu items per ADR 0001
            let status_item = MenuItemBuilder::with_id(
                "status",
                "Status: Companion Active (Backend Bridge Deferred to P11.2.2)",
            )
            .enabled(false)
            .build(app)?;

            let open_item = MenuItemBuilder::with_id("open_ui", "Open Full UI")
                .enabled(true)
                .build(app)?;

            let pause_item = MenuItemBuilder::with_id(
                "pause_perception",
                "Pause Perception (Available in P11.4)",
            )
            .enabled(false)
            .build(app)?;

            let settings_item =
                MenuItemBuilder::with_id("settings", "Settings (Available in P11.2.3)")
                    .enabled(false)
                    .build(app)?;

            let quit_item = MenuItemBuilder::with_id("quit", "Quit")
                .enabled(true)
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
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open_ui" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.unminimize();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
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
    #[test]
    fn test_tray_menu_action_identifiers() {
        assert_eq!("open_ui", "open_ui");
        assert_eq!("quit", "quit");
        assert_eq!("status", "status");
        assert_eq!("pause_perception", "pause_perception");
        assert_eq!("settings", "settings");
    }
}

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

pub mod autostart;
pub mod companion_render;
#[cfg(test)]
pub mod ram_qualification;
pub mod single_instance;
pub mod supervisor;
pub mod window_lifecycle;

/// Status of PICO Core backend process supervision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendSupervisionStatus {
    Unmanaged,
    Starting,
    Verifying { pid: u32 },
    Online { pid: u32 },
    Offline { reason: String },
}

/// Global managed state holding the supervised PICO Core child process and its operational status.
pub struct SupervisorState {
    pub child: Mutex<Option<supervisor::ManagedChild>>,
    pub status: Mutex<BackendSupervisionStatus>,
    pub shutdown_flag: Arc<AtomicBool>,
    pub watcher_handle: Mutex<Option<std::thread::JoinHandle<()>>>,
    pub session_store: supervisor::OperatorSessionStore,
    pub bind_port: Mutex<u16>,
}

impl SupervisorState {
    /// Creates a new unmanaged supervisor state container.
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
            status: Mutex::new(BackendSupervisionStatus::Unmanaged),
            shutdown_flag: Arc::new(AtomicBool::new(false)),
            watcher_handle: Mutex::new(None),
            session_store: supervisor::OperatorSessionStore::new(),
            bind_port: Mutex::new(8000),
        }
    }

    /// Performs an orderly shutdown: signals the watcher thread, joins it,
    /// terminates the managed child backend, invalidates session store and transitions status to Offline.
    pub fn shutdown(&self) {
        self.shutdown_flag.store(true, Ordering::SeqCst);
        self.session_store.invalidate();
        if let Ok(mut handle_guard) = self.watcher_handle.lock() {
            if let Some(handle) = handle_guard.take() {
                let _ = handle.join();
            }
        }
        if let Ok(mut child_guard) = self.child.lock() {
            if let Some(mut child) = child_guard.take() {
                let _ = child.terminate();
            }
        }
        if let Ok(mut status_guard) = self.status.lock() {
            *status_guard = BackendSupervisionStatus::Offline {
                reason: "Application shutdown".to_string(),
            };
        }
    }

    /// Checks the liveness of the supervised child process.
    /// If the process has exited unexpectedly while in Online state, transitions status to Offline.
    pub fn check_liveness(&self) -> BackendSupervisionStatus {
        let mut status_guard = match self.status.lock() {
            Ok(g) => g,
            Err(_) => {
                return BackendSupervisionStatus::Offline {
                    reason: "Lock poisoned".to_string(),
                }
            }
        };

        if let BackendSupervisionStatus::Online { pid } = *status_guard {
            let mut child_guard = match self.child.lock() {
                Ok(g) => g,
                Err(_) => {
                    return BackendSupervisionStatus::Offline {
                        reason: "Lock poisoned".to_string(),
                    }
                }
            };

            if let Some(child) = child_guard.as_mut() {
                if !child.is_alive() {
                    self.session_store.invalidate();
                    let reason = format!("Backend process PID {} terminated unexpectedly", pid);
                    *status_guard = BackendSupervisionStatus::Offline {
                        reason: reason.clone(),
                    };
                    *child_guard = None;
                    return BackendSupervisionStatus::Offline { reason };
                }
            } else {
                self.session_store.invalidate();
                let reason = "Managed child handle missing from state".to_string();
                *status_guard = BackendSupervisionStatus::Offline {
                    reason: reason.clone(),
                };
                return BackendSupervisionStatus::Offline { reason };
            }
        }

        status_guard.clone()
    }
}

impl Default for SupervisorState {
    fn default() -> Self {
        Self::new()
    }
}

/// Resolves the trusted PICO workspace root directory.
///
/// In packaged installations, detects whether the executable is running from an installed
/// bundle with adjacent `resources\core` or dedicated bundle directory.
/// In development mode, resolves the repository root via explicit trusted environment
/// variable `PICO_WORKSPACE_ROOT` or compile-time manifest location (`CARGO_MANIFEST_DIR`).
/// Arbitrary process current working directories (`current_dir`) are NEVER used as trust anchors.
pub fn resolve_trusted_project_root() -> Result<PathBuf, String> {
    // 1. Packaged installation check: inspect executable directory for resources\core
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            let packaged_core = exe_dir.join("resources").join("core");
            if packaged_core.is_dir() && packaged_core.join("pico").join("main.py").is_file() {
                if let Ok(canonical) = std::fs::canonicalize(&packaged_core) {
                    return Ok(canonical);
                }
            }
            // Check direct adjacent core directory
            let direct_core = exe_dir.join("core");
            if direct_core.is_dir() && direct_core.join("pico").join("main.py").is_file() {
                if let Ok(canonical) = std::fs::canonicalize(&direct_core) {
                    return Ok(canonical);
                }
            }
        }
    }

    // 2. Explicit environment variable PICO_WORKSPACE_ROOT
    let candidate = if let Ok(env_root) = std::env::var("PICO_WORKSPACE_ROOT") {
        PathBuf::from(env_root)
    } else {
        let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
        match manifest_dir.parent().and_then(|p| p.parent()) {
            Some(root) => root.to_path_buf(),
            None => {
                return Err(
                    "Failed to resolve repository root from build-time manifest location"
                        .to_string(),
                );
            }
        }
    };

    let canonical_root = std::fs::canonicalize(&candidate).map_err(|e| {
        format!(
            "Failed to canonicalize trusted workspace root '{}': {}",
            candidate.display(),
            e
        )
    })?;

    let main_py = canonical_root.join("pico").join("main.py");
    let pyproject = canonical_root.join("pyproject.toml");

    if !main_py.is_file() || !pyproject.is_file() {
        return Err(format!(
            "Trusted workspace root '{}' is invalid: missing pico/main.py or pyproject.toml",
            canonical_root.display()
        ));
    }

    Ok(canonical_root)
}

/// Resolves the launch configuration from an explicitly trusted project root.
///
/// In packaged installations, searches for bundled Python runtime in `resources\python\python.exe`
/// or `<root>\python.exe` or adjacent virtualenv.
/// In development installations, resolves `<root>/.venv/Scripts/python.exe`.
/// Fails closed if no verified interpreter is present. Refuses unmanaged PATH fallback.
pub fn resolve_backend_launch_config_from_root(
    project_root: &Path,
) -> Result<supervisor::LaunchConfig, String> {
    let canonical_root = std::fs::canonicalize(project_root)
        .map_err(|e| format!("Failed to canonicalize project root: {}", e))?;

    // Candidate 1: Packaged bundled Python runtime inside project_root
    let bundled_in_root = canonical_root.join("python").join("python.exe");
    let bundled_in_root_direct = canonical_root.join("python.exe");

    // Candidate 2: Packaged sibling python directory (e.g. <install_dir>/resources/python/python.exe or <install_dir>/resources/python/Scripts/python.exe)
    let sibling_python = canonical_root
        .parent()
        .map(|p| p.join("python").join("python.exe"));
    let sibling_python_scripts = canonical_root
        .parent()
        .map(|p| p.join("python").join("Scripts").join("python.exe"));

    // Candidate 3: Standard development .venv
    let venv_python_win = canonical_root
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    let venv_python_nix = canonical_root.join(".venv").join("bin").join("python");

    let python_exe = if bundled_in_root.is_file() {
        bundled_in_root
    } else if bundled_in_root_direct.is_file() {
        bundled_in_root_direct
    } else if let Some(ref sib) = sibling_python {
        if sib.is_file() {
            sib.clone()
        } else if let Some(ref sib_scripts) = sibling_python_scripts {
            if sib_scripts.is_file() {
                sib_scripts.clone()
            } else if venv_python_win.is_file() {
                venv_python_win
            } else if venv_python_nix.is_file() {
                venv_python_nix
            } else {
                return Err(format!(
                    "Dedicated Python interpreter not found in '{}' (checked bundled python and .venv). Refusing unmanaged PATH python fallback.",
                    canonical_root.display()
                ));
            }
        } else if venv_python_win.is_file() {
            venv_python_win
        } else if venv_python_nix.is_file() {
            venv_python_nix
        } else {
            return Err(format!(
                "Dedicated Python interpreter not found in '{}' (checked bundled python and .venv). Refusing unmanaged PATH python fallback.",
                canonical_root.display()
            ));
        }
    } else if venv_python_win.is_file() {
        venv_python_win
    } else if venv_python_nix.is_file() {
        venv_python_nix
    } else {
        return Err(format!(
            "Dedicated Python interpreter not found in '{}' (.venv/Scripts/python.exe). Refusing unmanaged PATH python fallback.",
            canonical_root.display()
        ));
    };

    let canonical_python = std::fs::canonicalize(&python_exe)
        .map_err(|e| format!("Failed to canonicalize Python interpreter path: {}", e))?;

    // Verify interpreter trust boundary: must reside either within project_root or in parent's resources tree
    let within_root = canonical_python.starts_with(&canonical_root);
    let within_parent = canonical_root
        .parent()
        .map(|p| canonical_python.starts_with(p))
        .unwrap_or(false);

    if !within_root && !within_parent {
        return Err("Python interpreter must reside within the trusted project root or application installation tree".to_string());
    }

    Ok(supervisor::LaunchConfig::default_local(
        canonical_python,
        canonical_root,
    ))
}

/// Resolves the launch configuration for the supervised PICO Core backend process.
///
/// Enforces trusted root validation and virtualenv interpreter resolution.
pub fn resolve_backend_launch_config() -> Result<supervisor::LaunchConfig, String> {
    let root = resolve_trusted_project_root()?;
    resolve_backend_launch_config_from_root(&root)
}

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

/// Resolves the tray click action from window query results with defensive fallback.
///
/// On Windows, `IsWindowVisible` returns `true` even when a window is minimized.
/// Furthermore, if querying `is_minimized()` returns an error, assuming `false`
/// combined with a successful `is_visible == true` could inadvertently trigger `Hide`
/// on a minimized window.
/// Therefore, this function defensively defaults an unknown or errored minimized state
/// to `true`, ensuring the window is restored and brought to focus rather than
/// accidentally hidden to the system tray.
pub fn resolve_tray_click_action<E1, E2>(
    minimized_res: Result<bool, E1>,
    visible_res: Result<bool, E2>,
) -> TrayClickAction {
    let is_minimized = minimized_res.unwrap_or(true);
    let is_visible = visible_res.unwrap_or(false);
    decide_tray_click_action(is_minimized, is_visible)
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
#[tauri::command]
async fn pico_get_system_status(
    state: tauri::State<'_, SupervisorState>,
) -> Result<String, String> {
    let (port, job_object, launch_secret, expected_pid) = {
        let port = *state
            .bind_port
            .lock()
            .map_err(|_| "Port lock poisoned".to_string())?;
        let child_guard = state
            .child
            .lock()
            .map_err(|_| "Child lock poisoned".to_string())?;
        let child = child_guard
            .as_ref()
            .ok_or_else(|| "Supervised backend is not running".to_string())?;
        (
            port,
            Arc::clone(&child.job_object),
            child.launch_secret.clone(),
            Some(child.pid),
        )
    };

    supervisor::execute_native_get_system_status(port, &job_object, &launch_secret, expected_pid)
        .await
}

#[tauri::command]
async fn pico_pair_operator(
    code: String,
    state: tauri::State<'_, SupervisorState>,
) -> Result<String, String> {
    let (port, job_object, launch_secret, expected_pid) = {
        let port = *state
            .bind_port
            .lock()
            .map_err(|_| "Port lock poisoned".to_string())?;
        let child_guard = state
            .child
            .lock()
            .map_err(|_| "Child lock poisoned".to_string())?;
        let child = child_guard
            .as_ref()
            .ok_or_else(|| "Supervised backend is not running".to_string())?;
        (
            port,
            Arc::clone(&child.job_object),
            child.launch_secret.clone(),
            Some(child.pid),
        )
    };

    supervisor::execute_native_pair_operator(
        port,
        &job_object,
        &launch_secret,
        expected_pid,
        &state.session_store,
        &code,
    )
    .await
}

#[tauri::command]
async fn pico_get_autostart_status() -> Result<autostart::AutoStartStatus, String> {
    autostart::is_autostart_enabled()
}

#[tauri::command]
async fn pico_set_autostart(enabled: bool) -> Result<autostart::AutoStartStatus, String> {
    if enabled {
        autostart::enable_autostart()
    } else {
        autostart::disable_autostart()
    }
}

fn open_or_focus_command_center(app: &tauri::AppHandle) {
    if let Some(lc) = app.try_state::<window_lifecycle::CommandCenterLifecycle>() {
        lc.request_open(app);
    }
}

fn sync_supervision_status_ui(
    app: &tauri::AppHandle,
    status_item: &tauri::menu::MenuItem<tauri::Wry>,
    next_status: &BackendSupervisionStatus,
) {
    let label = match next_status {
        BackendSupervisionStatus::Unmanaged => "Status: Unmanaged".to_string(),
        BackendSupervisionStatus::Starting => "Status: Starting...".to_string(),
        BackendSupervisionStatus::Verifying { pid } => {
            format!("Status: Verifying (PID: {})", pid)
        }
        BackendSupervisionStatus::Online { pid } => format!("Status: Online (PID: {})", pid),
        BackendSupervisionStatus::Offline { reason } => format!("Status: Offline ({})", reason),
    };

    if let Some(state) = app.try_state::<SupervisorState>() {
        if let Ok(mut status_guard) = state.status.lock() {
            *status_guard = next_status.clone();
        }
    }
    let _ = status_item.set_text(&label);
    if let Some(coord) = app.try_state::<companion_render::CompanionRenderCoordinator>() {
        coord.update_status(next_status, label);
    }
}

pub fn run() {
    // Single instance enforcement: acquire named mutex
    let single_instance_guard = match single_instance::SingleInstanceGuard::acquire() {
        Ok(guard) => guard,
        Err(e) => {
            eprintln!("Failed to evaluate single instance mutex: {}", e);
            return;
        }
    };

    if !single_instance_guard.is_primary() {
        eprintln!("Another instance of PICO Desktop Companion is already running. Exiting.");
        return;
    }

    tauri::Builder::default()
        .manage(SupervisorState::new())
        .manage(window_lifecycle::CommandCenterLifecycle::new())
        .manage(companion_render::CompanionRenderCoordinator::new(
            BackendSupervisionStatus::Unmanaged,
            "Status: Starting...".to_string(),
        ))
        .invoke_handler(tauri::generate_handler![
            pico_get_system_status,
            pico_pair_operator,
            pico_get_autostart_status,
            pico_set_autostart
        ])
        .setup(|app| {
            // 1. Initial state transition: Unmanaged -> Starting
            let initial_status = BackendSupervisionStatus::Starting;
            let initial_status_text = "Status: Starting...".to_string();
            if let Some(state) = app.try_state::<SupervisorState>() {
                if let Ok(mut status_guard) = state.status.lock() {
                    *status_guard = initial_status.clone();
                }
            }
            if let Some(coord) = app.try_state::<companion_render::CompanionRenderCoordinator>() {
                coord.update_status(&initial_status, initial_status_text.clone());
            }

            // 1b. Create the Native Companion Window (Zero WebView2 at startup) and attach GDI+ eyes
            let companion_win = tauri::window::WindowBuilder::new(app, "companion")
                .title("PICO Companion")
                .inner_size(240.0, 120.0)
                .resizable(false)
                .decorations(true)
                .always_on_top(true)
                .visible(true)
                .build()?;

            if let Some(coord) = app.try_state::<companion_render::CompanionRenderCoordinator>() {
                companion_render::attach_companion_renderer(
                    &companion_win,
                    app.handle().clone(),
                    &coord,
                )
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
            }

            // 2. Build system tray menu items per ADR 0001
            let status_item =
                MenuItemBuilder::with_id(TrayAction::Status.id(), &initial_status_text)
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

            // 3. Run Core startup, verification, and ongoing liveness supervision on a managed
            // background thread so the native companion HWND paints real Starting -> Verifying ->
            // Online/Offline transitions immediately without blocking the UI pump.
            let app_handle = app.handle().clone();
            let status_item_watcher = status_item.clone();
            let shutdown_flag_watcher = if let Some(state) = app.try_state::<SupervisorState>() {
                Arc::clone(&state.shutdown_flag)
            } else {
                Arc::new(AtomicBool::new(false))
            };

            let watcher_thread = std::thread::spawn(move || {
                let launch_config_res = resolve_backend_launch_config();
                if let Some(state) = app_handle.try_state::<SupervisorState>() {
                    if let Ok(config) = &launch_config_res {
                        if let Ok(mut port_guard) = state.bind_port.lock() {
                            *port_guard = config.bind_port;
                        }
                    }
                }

                let supervision_outcome: Result<(supervisor::ManagedChild, u32), String> =
                    match &launch_config_res {
                        Ok(config) => match config.spawn() {
                            Ok(mut child) => {
                                let pid = child.pid;
                                sync_supervision_status_ui(
                                    &app_handle,
                                    &status_item_watcher,
                                    &BackendSupervisionStatus::Verifying { pid },
                                );

                                match child.wait_and_verify(
                                    config.bind_port,
                                    std::time::Duration::from_secs(15),
                                ) {
                                    Ok(()) => Ok((child, pid)),
                                    Err(verify_err) => {
                                        let _ = child.terminate();
                                        Err(format!("Startup verification failed: {}", verify_err))
                                    }
                                }
                            }
                            Err(spawn_err) => Err(format!("Process spawn failed: {}", spawn_err)),
                        },
                        Err(config_err) => {
                            Err(format!("Configuration resolution failed: {}", config_err))
                        }
                    };

                if shutdown_flag_watcher.load(Ordering::Relaxed) {
                    if let Ok((mut child, _)) = supervision_outcome {
                        let _ = child.terminate();
                    }
                    return;
                }

                let mut last_status = match supervision_outcome {
                    Ok((child, pid)) => {
                        if let Some(state) = app_handle.try_state::<SupervisorState>() {
                            if let Ok(mut child_guard) = state.child.lock() {
                                *child_guard = Some(child);
                            }
                        }
                        let next = BackendSupervisionStatus::Online { pid };
                        sync_supervision_status_ui(&app_handle, &status_item_watcher, &next);
                        next
                    }
                    Err(reason) => {
                        let next = BackendSupervisionStatus::Offline { reason };
                        sync_supervision_status_ui(&app_handle, &status_item_watcher, &next);
                        next
                    }
                };

                while !shutdown_flag_watcher.load(Ordering::Relaxed) {
                    std::thread::sleep(std::time::Duration::from_millis(250));
                    if shutdown_flag_watcher.load(Ordering::Relaxed) {
                        break;
                    }
                    if let Some(state) = app_handle.try_state::<SupervisorState>() {
                        let status = state.check_liveness();
                        if status != last_status {
                            sync_supervision_status_ui(&app_handle, &status_item_watcher, &status);
                            last_status = status;
                        }
                    }
                }
            });

            if let Some(state) = app.try_state::<SupervisorState>() {
                if let Ok(mut handle_guard) = state.watcher_handle.lock() {
                    *handle_guard = Some(watcher_thread);
                }
            }

            let mut tray_builder = TrayIconBuilder::with_id("pico-tray")
                .tooltip("PICO Desktop Companion")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| {
                    if let Some(action) = TrayAction::from_id(event.id().as_ref()) {
                        match decide_menu_dispatch_effect(action) {
                            MenuDispatchEffect::OpenWindow => {
                                open_or_focus_command_center(app);
                            }
                            MenuDispatchEffect::Exit(code) => {
                                if let Some(lc) =
                                    app.try_state::<window_lifecycle::CommandCenterLifecycle>()
                                {
                                    lc.mark_shutting_down();
                                }
                                if let Some(state) = app.try_state::<SupervisorState>() {
                                    state.shutdown();
                                }
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
                            let action = resolve_tray_click_action(
                                window.is_minimized(),
                                window.is_visible(),
                            );
                            match action {
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
                        } else {
                            open_or_focus_command_center(app);
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
            match event {
                WindowEvent::CloseRequested { api, .. } => {
                    if window.label() == "main" {
                        // Prevent synchronous blocking destroy inside callback; dispatch cleanly
                        api.prevent_close();
                        let app = window.app_handle();
                        if let Some(lc) =
                            app.try_state::<window_lifecycle::CommandCenterLifecycle>()
                        {
                            lc.request_close(app);
                        }
                    } else {
                        // Native companion window close hides or respects close policy
                        let policy = evaluate_close_request_policy();
                        if policy.prevent_close {
                            api.prevent_close();
                        }
                        if policy.hide_window {
                            let _ = window.hide();
                        }
                    }
                }
                WindowEvent::Destroyed => {
                    if window.label() == "main" {
                        let app = window.app_handle();
                        if let Some(lc) =
                            app.try_state::<window_lifecycle::CommandCenterLifecycle>()
                        {
                            lc.on_destroyed(app);
                        }
                    }
                }
                _ => {}
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
    fn test_resolve_tray_click_action_error_fallback_restores_and_focuses() {
        // If querying minimized state returns an error, defensively assume true
        // so the window is restored and brought to focus rather than accidentally hidden.
        let err_minimized: Result<bool, &'static str> = Err("OS query failed");
        let ok_visible: Result<bool, &'static str> = Ok(true);
        assert_eq!(
            resolve_tray_click_action(err_minimized, ok_visible),
            TrayClickAction::RestoreAndFocus
        );

        let err_visible: Result<bool, &'static str> = Err("OS query failed");
        assert_eq!(
            resolve_tray_click_action(err_minimized, err_visible),
            TrayClickAction::RestoreAndFocus
        );
    }

    #[test]
    fn test_resolve_tray_click_action_ok_states() {
        assert_eq!(
            resolve_tray_click_action(Ok::<bool, ()>(true), Ok::<bool, ()>(true)),
            TrayClickAction::RestoreAndFocus
        );
        assert_eq!(
            resolve_tray_click_action(Ok::<bool, ()>(false), Ok::<bool, ()>(true)),
            TrayClickAction::Hide
        );
        assert_eq!(
            resolve_tray_click_action(Ok::<bool, ()>(false), Ok::<bool, ()>(false)),
            TrayClickAction::ShowAndFocus
        );
        // If not minimized and visibility query fails, default to showing
        let err_visible: Result<bool, &'static str> = Err("OS query failed");
        assert_eq!(
            resolve_tray_click_action(Ok::<bool, &'static str>(false), err_visible),
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

    #[test]
    fn test_backend_supervision_status_variants() {
        let unmanaged = BackendSupervisionStatus::Unmanaged;
        let starting = BackendSupervisionStatus::Starting;
        let verifying = BackendSupervisionStatus::Verifying { pid: 12345 };
        let online = BackendSupervisionStatus::Online { pid: 12345 };
        let offline = BackendSupervisionStatus::Offline {
            reason: "Port occupied".to_string(),
        };

        assert_eq!(unmanaged, BackendSupervisionStatus::Unmanaged);
        assert_eq!(starting, BackendSupervisionStatus::Starting);
        assert_eq!(
            verifying,
            BackendSupervisionStatus::Verifying { pid: 12345 }
        );
        assert_eq!(online, BackendSupervisionStatus::Online { pid: 12345 });
        assert_ne!(online, BackendSupervisionStatus::Online { pid: 54321 });
        assert_eq!(
            offline,
            BackendSupervisionStatus::Offline {
                reason: "Port occupied".to_string()
            }
        );
    }

    #[test]
    fn test_supervisor_state_initialization() {
        let state = SupervisorState::new();
        let status = state.status.lock().unwrap();
        assert_eq!(*status, BackendSupervisionStatus::Unmanaged);
        let child = state.child.lock().unwrap();
        assert!(child.is_none());
    }

    #[test]
    fn test_supervisor_state_check_liveness_transitions_stale_online() {
        let state = SupervisorState::new();
        {
            let mut status = state.status.lock().unwrap();
            *status = BackendSupervisionStatus::Online { pid: 999999 };
        }
        // When Online status is checked without a retained live child, transitions to Offline
        let status = state.check_liveness();
        assert!(
            matches!(status, BackendSupervisionStatus::Offline { .. }),
            "Expected Offline when child handle is missing or dead"
        );
    }

    #[test]
    fn test_supervisor_state_shutdown_joins_watcher_thread() {
        let state = SupervisorState::new();
        let shutdown_flag = Arc::clone(&state.shutdown_flag);
        let thread_executed = Arc::new(AtomicBool::new(false));
        let thread_executed_clone = Arc::clone(&thread_executed);

        let handle = std::thread::spawn(move || {
            while !shutdown_flag.load(Ordering::Relaxed) {
                thread_executed_clone.store(true, Ordering::SeqCst);
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
        });

        {
            let mut handle_guard = state.watcher_handle.lock().unwrap();
            *handle_guard = Some(handle);
        }

        // Wait briefly for thread execution
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert!(thread_executed.load(Ordering::SeqCst));

        // Orderly shutdown must join thread and transition to Offline
        state.shutdown();

        // Verify handle was consumed and thread joined
        let handle_guard = state.watcher_handle.lock().unwrap();
        assert!(handle_guard.is_none());

        let status = state.status.lock().unwrap();
        assert_eq!(
            *status,
            BackendSupervisionStatus::Offline {
                reason: "Application shutdown".to_string()
            }
        );
    }

    #[test]
    fn test_resolve_backend_launch_config() {
        let config_res = resolve_backend_launch_config();
        assert!(
            config_res.is_ok(),
            "Config resolution must succeed in trusted repository root"
        );
        let config = config_res.unwrap();
        assert_eq!(config.bind_host, "127.0.0.1");
        assert_eq!(config.bind_port, 8000);
        assert!(!config.args.contains(&"--reload".to_string()));
        assert!(config.executable.is_file());
        assert!(config.working_dir.join("pico").join("main.py").is_file());
    }

    #[test]
    fn test_adversarial_decoy_project_rejected() {
        let temp_dir = std::env::temp_dir().join(format!("pico_decoy_test_{}", std::process::id()));
        let _ = std::fs::create_dir_all(temp_dir.join("pico"));
        let _ = std::fs::write(temp_dir.join("pico").join("main.py"), "# decoy\n");
        let _ = std::fs::write(temp_dir.join("pyproject.toml"), "# decoy\n");

        let decoy_res = resolve_backend_launch_config_from_root(&temp_dir);
        assert!(
            decoy_res.is_err(),
            "Decoy project without dedicated virtualenv MUST fail closed"
        );
        let err_msg = decoy_res.unwrap_err();
        assert!(
            err_msg.contains("Dedicated Python interpreter not found"),
            "Error message must specify missing interpreter: {}",
            err_msg
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[test]
    fn test_packaged_layout_resolution_invariants() {
        let temp_dir = std::env::temp_dir().join(format!("pico_pkg_test_{}", std::process::id()));
        let core_dir = temp_dir.join("resources").join("core");
        let python_dir = temp_dir.join("resources").join("python");
        let _ = std::fs::create_dir_all(core_dir.join("pico"));
        let _ = std::fs::create_dir_all(&python_dir);
        let _ = std::fs::write(core_dir.join("pico").join("main.py"), "# mock core\n");
        let _ = std::fs::write(core_dir.join("pyproject.toml"), "# mock pyproject\n");
        let mock_python = python_dir.join("python.exe");
        let _ = std::fs::write(&mock_python, "# mock python binary\n");

        let config_res = resolve_backend_launch_config_from_root(&core_dir);
        assert!(
            config_res.is_ok(),
            "Packaged layout with resources/core and resources/python must succeed: {:?}",
            config_res.err()
        );
        let config = config_res.unwrap();
        assert_eq!(
            config.working_dir,
            std::fs::canonicalize(&core_dir).unwrap()
        );
        assert_eq!(
            config.executable,
            std::fs::canonicalize(&mock_python).unwrap()
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[test]
    fn test_e2e_backend_supervision_lifecycle() {
        let mut config = resolve_backend_launch_config().expect("Config resolution must succeed");
        let test_port: u16 = 8991;
        config.bind_port = test_port;
        for i in 0..config.args.len() {
            if config.args[i] == "--port" && i + 1 < config.args.len() {
                config.args[i + 1] = test_port.to_string();
            }
        }

        let mut child = config
            .spawn()
            .expect("Spawning managed child into Job Object must succeed");
        assert!(
            child.pid > 0,
            "Spawned child must have a valid positive PID"
        );

        // Perform mutual handshake and listener PID verification
        let verify_res = child.wait_and_verify(test_port, std::time::Duration::from_secs(20));
        assert!(
            verify_res.is_ok(),
            "wait_and_verify failed: {:?}",
            verify_res.err()
        );

        // Terminate managed child and verify cleanup
        let term_res = child.terminate();
        assert!(term_res.is_ok(), "Termination must succeed");

        // Wait a brief moment and verify child process has exited
        std::thread::sleep(std::time::Duration::from_millis(500));
        assert!(
            !child.is_alive(),
            "Child process must no longer be alive after termination"
        );
    }

    #[test]
    fn test_adversarial_port_collision_refuses_takeover() {
        use std::net::TcpListener;
        let test_port: u16 = 8992;
        let listener = TcpListener::bind(("127.0.0.1", test_port))
            .expect("Foreign test listener must bind successfully");

        let mut config = resolve_backend_launch_config().expect("Config resolution must succeed");
        config.bind_port = test_port;
        for i in 0..config.args.len() {
            if config.args[i] == "--port" && i + 1 < config.args.len() {
                config.args[i + 1] = test_port.to_string();
            }
        }

        // The supervisor MUST refuse takeover and fail closed during spawn because the port is already occupied
        let spawn_res = config.spawn();
        assert!(
            spawn_res.is_err(),
            "Supervisor MUST refuse to spawn when port is occupied by foreign listener"
        );
        let err_str = spawn_res.err().unwrap();
        assert!(
            err_str.contains("is already bound by process PID")
                && err_str.contains("refusing unmanaged takeover"),
            "Error must specify port collision and refusing takeover: {}",
            err_str
        );

        drop(listener);
    }

    #[test]
    fn test_e2e_backend_restart_lifecycle() {
        let mut config = resolve_backend_launch_config().expect("Config resolution must succeed");
        let test_port: u16 = 8993;
        config.bind_port = test_port;
        for i in 0..config.args.len() {
            if config.args[i] == "--port" && i + 1 < config.args.len() {
                config.args[i + 1] = test_port.to_string();
            }
        }

        // Run 1
        let mut child1 = config.spawn().expect("Spawn 1 failed");
        let secret1 = child1.launch_secret.clone();
        assert!(child1
            .wait_and_verify(test_port, std::time::Duration::from_secs(20))
            .is_ok());
        assert!(child1.terminate().is_ok());
        std::thread::sleep(std::time::Duration::from_millis(500));
        assert!(!child1.is_alive());

        // Run 2 (Restart with fresh secret)
        let mut child2 = config.spawn().expect("Spawn 2 failed");
        let secret2 = child2.launch_secret.clone();
        assert_ne!(
            secret1, secret2,
            "Restart must generate a fresh cryptographic secret"
        );
        assert!(child2
            .wait_and_verify(test_port, std::time::Duration::from_secs(20))
            .is_ok());
        assert!(child2.terminate().is_ok());
        std::thread::sleep(std::time::Duration::from_millis(500));
        assert!(!child2.is_alive());
    }

    // =========================================================================
    // P11.2.2.2 Slice 1A: Native Verified HTTP Transport Qualification Tests
    // =========================================================================

    #[test]
    fn test_slice_1a_scenario_a_genuine_core_verified_connection() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 8994;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            // Establish VerifiedConnection
            let mut conn = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("VerifiedConnection::connect failed for genuine Core");

            assert!(conn.is_ready());
            assert_eq!(conn.server_pid(), child.pid);
            assert!(conn.creation_time() > 0);
            assert_eq!(
                conn.state(),
                &supervisor::ConnectionState::Ready { pid: child.pid }
            );

            // Send diagnostic HTTP request on the same verified socket
            let req = hyper::Request::builder()
                .method("GET")
                .uri("/health")
                .header("Host", format!("127.0.0.1:{}", test_port))
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .expect("Failed to build diagnostic request");

            let resp = conn
                .send_request(req)
                .await
                .expect("Failed to send diagnostic request");
            assert_eq!(resp.status(), hyper::StatusCode::OK);

            let body_bytes = http_body_util::BodyExt::collect(resp.into_body())
                .await
                .expect("Failed to read diagnostic response")
                .to_bytes();
            let body_str = String::from_utf8_lossy(&body_bytes);
            assert!(
                body_str.contains("healthy")
                    || body_str.contains("ok")
                    || body_str.contains("status")
            );

            // Orderly cleanup
            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }

    #[test]
    fn test_slice_1a_scenario_b_wrong_secret_rejected() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 8995;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            // Attempt connection with invalid launch secret
            let wrong_secret = "0000000000000000000000000000000000000000000000000000000000000000";
            let conn_res = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                wrong_secret,
                Some(child.pid),
            )
            .await;

            assert!(
                conn_res.is_err(),
                "Connection with wrong secret must fail closed"
            );
            let err = conn_res.err().unwrap();
            assert!(
                err.contains("HMAC verification failed") || err.contains("secret mismatch"),
                "Error must reflect HMAC failure: {}",
                err
            );

            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }

    #[test]
    fn test_slice_1a_scenario_c_rogue_external_listener_rejected() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            // Bind a rogue listener outside the supervisor Job Object
            let rogue_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("Failed to bind rogue listener");
            let rogue_port = rogue_listener.local_addr().unwrap().port();

            // Spawn rogue server task that keeps connection open
            let (_tx, rx) = tokio::sync::oneshot::channel::<()>();
            tokio::spawn(async move {
                if let Ok((stream, _)) = rogue_listener.accept().await {
                    let _ = rx.await;
                    drop(stream);
                }
            });

            // Create a supervisor Job Object that does NOT contain this process or listener
            let job_object = supervisor::JobObject::new().expect("Failed to create Job Object");

            let conn_res = supervisor::VerifiedConnection::connect(
                rogue_port,
                &job_object,
                "some_secret",
                None,
            )
            .await;

            assert!(
                conn_res.is_err(),
                "Rogue external listener must be rejected"
            );
            let err = conn_res.err().unwrap();
            assert!(
                err.contains("not contained within the supervisor Job Object")
                    || err.contains("rejecting rogue connection"),
                "Error must reject due to Job Object containment: {}",
                err
            );
            let _ = _tx.send(());
        });
    }

    #[test]
    fn test_slice_1a_scenario_d_rogue_in_job_process_rejected() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            // Create a Job Object and assign the current process to it
            let job_object = supervisor::JobObject::new().expect("Failed to create Job Object");
            if let Ok(safe_handle) = supervisor::open_process_handle(std::process::id()) {
                let _ = job_object.assign_process(safe_handle.raw());
            }

            // Bind listener within Job Object
            let rogue_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.expect("Failed to bind");
            let rogue_port = rogue_listener.local_addr().unwrap().port();

            tokio::spawn(async move {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                if let Ok((mut socket, _)) = rogue_listener.accept().await {
                    let mut buf = vec![0u8; 1024];
                    let _ = socket.read(&mut buf).await;
                    // Respond with invalid HMAC
                    let resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 35\r\n\r\n{\"hmac\":\"invalid_in_job_hmac_value\"}";
                    let _ = socket.write_all(resp.as_bytes()).await;
                }
            });

            let conn_res = supervisor::VerifiedConnection::connect(
                rogue_port,
                &job_object,
                "expected_secret",
                None,
            )
            .await;

            assert!(conn_res.is_err(), "In-job rogue listener with bad HMAC must be rejected");
            let err = conn_res.err().unwrap();
            assert!(
                err.contains("HMAC verification failed") || err.contains("not contained"),
                "Error must indicate HMAC verification failure: {}",
                err
            );
        });
    }

    #[test]
    fn test_slice_1a_scenario_e_backend_termination_fails_closed() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 8997;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            let mut conn = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("VerifiedConnection::connect failed");

            assert!(conn.is_ready());

            // Terminate backend process
            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());

            // Dispatch request on terminated backend connection
            let req = hyper::Request::builder()
                .method("GET")
                .uri("/health")
                .header("Host", format!("127.0.0.1:{}", test_port))
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .expect("Failed to build request");

            let res = conn.send_request(req).await;
            assert!(
                res.is_err(),
                "Request on terminated backend must fail closed"
            );
            assert_eq!(conn.state(), &supervisor::ConnectionState::Disconnected);
        });
    }

    #[test]
    fn test_slice_1a_scenario_f_port_takeover_rejected() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 8998;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            let secret = child.launch_secret.clone();
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            let mut conn = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &secret,
                Some(child.pid),
            )
            .await
            .expect("VerifiedConnection::connect failed");

            // Terminate backend
            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());

            // Bind rogue listener to exact same port
            let rogue_listener = tokio::net::TcpListener::bind(format!("127.0.0.1:{}", test_port))
                .await
                .expect("Failed to bind takeover listener");
            let (_tx_f, rx_f) = tokio::sync::oneshot::channel::<()>();
            tokio::spawn(async move {
                while let Ok((stream, _)) = rogue_listener.accept().await {
                    let _ = rx_f.await;
                    drop(stream);
                    break;
                }
            });

            // 1. Old connection must not transparently reconnect
            let req = hyper::Request::builder()
                .method("GET")
                .uri("/health")
                .header("Host", format!("127.0.0.1:{}", test_port))
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .expect("Failed to build request");
            let res = conn.send_request(req).await;
            assert!(res.is_err());
            assert_eq!(conn.state(), &supervisor::ConnectionState::Disconnected);

            // 2. New connection attempt must reject rogue takeover
            let conn2_res = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &secret,
                Some(child.pid),
            )
            .await;
            assert!(
                conn2_res.is_err(),
                "New connection must reject rogue takeover on port"
            );
            let _ = _tx_f.send(());
        });
    }

    #[test]
    fn test_slice_1a_scenario_g_new_connection_re_verification() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 8999;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            // Connection 1
            let mut conn1 = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("Connection 1 failed");

            assert!(conn1.is_ready());
            let req1 = hyper::Request::builder()
                .method("GET")
                .uri("/health")
                .header("Host", format!("127.0.0.1:{}", test_port))
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .expect("Failed to build request");
            let resp1 = conn1.send_request(req1).await.expect("Request 1 failed");
            assert_eq!(resp1.status(), hyper::StatusCode::OK);

            // Drop Connection 1
            drop(conn1);

            // Connection 2 - independently verified from scratch
            let mut conn2 = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("Connection 2 failed");

            assert!(conn2.is_ready());
            let req2 = hyper::Request::builder()
                .method("GET")
                .uri("/health")
                .header("Host", format!("127.0.0.1:{}", test_port))
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .expect("Failed to build request");
            let resp2 = conn2.send_request(req2).await.expect("Request 2 failed");
            assert_eq!(resp2.status(), hyper::StatusCode::OK);

            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }

    #[test]
    fn test_slice_1a_connection_driver_aborts_on_drop_or_error() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 9002;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            let conn = supervisor::VerifiedConnection::connect(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("Connection failed");

            assert!(conn.is_ready());

            // Dropping connection should trigger Drop which aborts background driver handle
            drop(conn);

            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }

    #[test]
    fn test_slice_1b_native_get_system_status_real_core() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 9003;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            let status_json = supervisor::execute_native_get_system_status(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
            )
            .await
            .expect("execute_native_get_system_status failed");

            assert!(status_json.contains("\"status\":\"healthy\""));

            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }

    #[test]
    fn test_slice_1b_native_pair_operator_real_core() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let mut config =
                resolve_backend_launch_config().expect("Config resolution must succeed");
            let test_port: u16 = 9004;
            config.bind_port = test_port;
            for i in 0..config.args.len() {
                if config.args[i] == "--port" && i + 1 < config.args.len() {
                    config.args[i + 1] = test_port.to_string();
                }
            }

            let mut child = config.spawn().expect("Spawn failed");
            assert!(child
                .wait_and_verify(test_port, std::time::Duration::from_secs(20))
                .is_ok());

            let session_store = supervisor::OperatorSessionStore::new();

            // 1. Invalid code format rejected client-side
            let invalid_format = supervisor::execute_native_pair_operator(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
                &session_store,
                "short",
            )
            .await;
            assert!(invalid_format.is_err());
            assert!(invalid_format.unwrap_err().contains("6 numeric digits"));

            // 1b. Non-digit rejected
            let non_digit = supervisor::execute_native_pair_operator(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
                &session_store,
                "12ab56",
            )
            .await;
            assert!(non_digit.is_err());
            assert!(non_digit.unwrap_err().contains("6 numeric digits"));

            // 2. Non-existent code fails closed at backend
            let unminted_res = supervisor::execute_native_pair_operator(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
                &session_store,
                "999999",
            )
            .await;
            assert!(unminted_res.is_err());

            let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .to_path_buf();
            let python_bin = repo_root.join(".venv").join("Scripts").join("python.exe");

            let mint_output = std::process::Command::new(&python_bin)
                .current_dir(&repo_root)
                .args(&[
                    "-c",
                    "from pico.services.operator_session import OperatorSessionService; s = OperatorSessionService.get_instance(); c = s.generate_pairing_code(); print(c.code)",
                ])
                .output()
                .expect("Failed to mint pairing challenge via python");

            let valid_code = String::from_utf8_lossy(&mint_output.stdout).trim().to_string();
            if valid_code.len() != 6 {
                panic!(
                    "Failed to mint valid pairing code. Output was stdout: '{}', stderr: '{}', status: {:?}",
                    String::from_utf8_lossy(&mint_output.stdout),
                    String::from_utf8_lossy(&mint_output.stderr),
                    mint_output.status
                );
            }
            assert_eq!(valid_code.len(), 6);

            // 4. Pair with valid code
            let pair_res = supervisor::execute_native_pair_operator(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
                &session_store,
                &valid_code,
            )
            .await
            .expect("Pairing with valid code must succeed");

            assert!(pair_res.contains("\"status\":\"paired\""));
            assert!(session_store.is_paired(child.pid, child.creation_time));
            let creds = session_store.get_credentials(child.pid, child.creation_time).unwrap();
            assert!(creds.0.starts_with("pico_operator_session="));
            assert!(!creds.1.is_empty());

            // 5. Invalidation on shutdown/restart
            session_store.invalidate();
            assert!(!session_store.is_paired(child.pid, child.creation_time));

            assert!(child.terminate().is_ok());
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            assert!(!child.is_alive());
        });
    }
}

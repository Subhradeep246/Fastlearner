//! Zipity desktop shell entry point.
//!
//! The shell composes the platform-neutral [`DesktopController`] over the
//! [`os`] adapter registry and exposes lifecycle commands to the React client.
//! Tray installation and wake-shortcut registration are non-fatal: a failure
//! keeps the main-window keyboard and text flow available and is recorded for
//! the client to surface (Requirements 3.2, 3.9).

use std::sync::Arc;

use tauri::{Manager, WindowEvent};

use controller::DesktopController;

mod commands;
mod controller;
mod os;
mod wake_runtime;

/// Builds the shared desktop controller for the current platform.
#[must_use]
pub fn build_controller() -> Arc<DesktopController> {
    Arc::new(DesktopController::new(os::current_adapters()))
}

/// Starts the native Zipity desktop process.
///
/// # Panics
///
/// Panics when Tauri cannot initialize or run the application.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let controller = build_controller();
    let setup_controller = Arc::clone(&controller);
    let wake_runtime = Arc::new(wake_runtime::WakeRuntime::default());
    let setup_wake_runtime = Arc::clone(&wake_runtime);

    tauri::Builder::default()
        .manage(Arc::clone(&controller))
        .manage(Arc::clone(&wake_runtime))
        .invoke_handler(tauri::generate_handler![
            commands::get_capabilities,
            commands::get_wake_config,
            commands::set_wake_config,
            commands::pause_wake,
            commands::resume_wake,
            commands::open_overlay,
            commands::report_microphone_permission,
            commands::set_login_at_startup,
            commands::secure_session_set,
            commands::secure_session_clear,
            commands::quit,
        ])
        .setup(move |app| {
            // Tray failure is non-fatal; the controller records it as recoverable.
            if let Err(error) = setup_controller.install_tray() {
                eprintln!("continuing without tray: {error}");
            }
            if let Err(error) = setup_controller.register_wake_shortcut() {
                setup_controller.note_recoverable(format!("wake shortcut unavailable: {error}"));
                eprintln!("continuing without wake shortcut: {error}");
            }
            let wake_config = setup_controller.wake_config()?;
            if !wake_config.keyboard_only {
                match setup_wake_runtime.start(app.handle().clone(), wake_config) {
                    Ok(()) => {
                        let _ = setup_controller
                            .record_microphone_permission(os::PermissionState::Granted);
                        if let Err(error) = setup_controller.begin_wake_listening() {
                            setup_controller
                                .note_recoverable(format!("wake status unavailable: {error}"));
                        }
                    }
                    Err(error) => {
                        setup_controller
                            .note_recoverable(format!("double-clap wake unavailable: {error}"));
                        eprintln!("continuing without microphone wake: {error}");
                    }
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let controller = window.state::<Arc<DesktopController>>();
                // Closing the main window hides it and keeps the companion in the
                // tray rather than exiting the process.
                if matches!(controller.handle_main_window_close(), Ok(true)) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Zipity desktop");
}

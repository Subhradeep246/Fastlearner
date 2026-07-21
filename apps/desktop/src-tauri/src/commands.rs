//! Platform-neutral Tauri commands that delegate to the [`DesktopController`].
//!
//! The command surface is identical on every platform: the controller resolves
//! the behavior through the OS adapter registry, so Windows and Linux builds
//! reuse these commands unchanged.

use std::sync::Arc;

use tauri::{AppHandle, State};

use crate::controller::{DesktopController, LifecycleStatus, OverlayPlacement, WakeConfig};
use crate::os::{Capabilities, OsError, PermissionState};
use crate::wake_runtime::WakeRuntime;

type Controller<'a> = State<'a, Arc<DesktopController>>;
type Runtime<'a> = State<'a, Arc<WakeRuntime>>;

/// Reports platform capabilities and any non-fatal issues.
#[tauri::command]
pub fn get_capabilities(controller: Controller<'_>) -> Result<Capabilities, OsError> {
    controller.capabilities()
}

/// Returns the current wake configuration.
#[tauri::command]
pub fn get_wake_config(controller: Controller<'_>) -> Result<WakeConfig, OsError> {
    controller.wake_config()
}

/// Validates and stores new wake settings.
#[tauri::command]
pub fn set_wake_config(
    app: AppHandle,
    controller: Controller<'_>,
    runtime: Runtime<'_>,
    config: WakeConfig,
) -> Result<WakeConfig, OsError> {
    let config = controller.set_wake_config(config)?;
    runtime.stop();
    if !config.keyboard_only {
        runtime.start(app, config.clone())?;
    }
    Ok(config)
}

/// Pauses wake listening, stopping the microphone stream first.
#[tauri::command]
pub fn pause_wake(
    controller: Controller<'_>,
    runtime: Runtime<'_>,
) -> Result<LifecycleStatus, OsError> {
    runtime.stop();
    controller.pause_wake()?;
    controller.status()
}

/// Resumes wake listening when permitted.
#[tauri::command]
pub fn resume_wake(
    app: AppHandle,
    controller: Controller<'_>,
    runtime: Runtime<'_>,
) -> Result<LifecycleStatus, OsError> {
    controller.resume_wake()?;
    let config = controller.wake_config()?;
    if !config.keyboard_only {
        if let Err(error) = runtime.start(app, config) {
            controller.handle_device_unavailable();
            return Err(error);
        }
    }
    controller.status()
}

/// Computes centered overlay placement for the companion.
#[tauri::command]
pub fn open_overlay(controller: Controller<'_>) -> Result<OverlayPlacement, OsError> {
    controller.open_overlay()
}

/// Records a microphone permission decision reported by the operating system.
#[tauri::command]
pub fn report_microphone_permission(
    controller: Controller<'_>,
    runtime: Runtime<'_>,
    state: PermissionState,
) -> Result<LifecycleStatus, OsError> {
    if !state.allows_capture() {
        runtime.stop();
    }
    controller.record_microphone_permission(state)
}

/// Enables or disables opt-in start-at-login.
#[tauri::command]
pub fn set_login_at_startup(
    controller: Controller<'_>,
    enabled: bool,
) -> Result<LifecycleStatus, OsError> {
    controller.set_login_at_startup(enabled)?;
    controller.status()
}

/// Stores a session secret in operating-system secure storage.
#[tauri::command]
pub fn secure_session_set(
    controller: Controller<'_>,
    key: String,
    secret: String,
) -> Result<(), OsError> {
    controller.secure_session_set(&key, secret.as_bytes())
}

/// Removes a session secret from operating-system secure storage.
#[tauri::command]
pub fn secure_session_clear(controller: Controller<'_>, key: String) -> Result<(), OsError> {
    controller.secure_session_clear(&key)
}

/// Performs explicit-quit cleanup and terminates the desktop process.
#[tauri::command]
pub fn quit(
    app: AppHandle,
    controller: Controller<'_>,
    runtime: Runtime<'_>,
) -> Result<(), OsError> {
    runtime.stop();
    controller.quit()?;
    app.exit(0);
    Ok(())
}

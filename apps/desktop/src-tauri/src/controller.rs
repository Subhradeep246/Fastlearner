//! Platform-neutral desktop lifecycle controller.
//!
//! The controller owns wake configuration and lifecycle state and drives the
//! [`OsAdapters`] registry. It contains no Tauri or network types, so its
//! behavior (close-to-tray, quit cleanup, pause-before-report, permission gates,
//! capability reporting, and non-fatal tray recovery) is unit-tested directly.

use std::sync::Mutex;

use wake_detector::{DetectorConfig, DetectorConfigError};

use crate::os::{
    Capabilities, Notification, OsAdapters, OsError, PermissionState, Rect, TrayAction,
    DEFAULT_WAKE_CHORD, OVERLAY_HEIGHT, OVERLAY_WIDTH,
};

/// Wake detection settings surfaced to the desktop client.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct WakeConfig {
    pub frame_ms: u16,
    pub sensitivity: f32,
    pub min_gap_ms: u16,
    pub max_gap_ms: u16,
    pub cooldown_ms: u16,
    pub microphone_device: Option<String>,
    pub keyboard_only: bool,
}

impl Default for WakeConfig {
    fn default() -> Self {
        let detector = DetectorConfig::default();
        Self {
            frame_ms: detector.frame_ms,
            sensitivity: detector.sensitivity,
            min_gap_ms: detector.min_gap_ms,
            max_gap_ms: detector.max_gap_ms,
            cooldown_ms: detector.cooldown_ms,
            microphone_device: None,
            keyboard_only: false,
        }
    }
}

impl WakeConfig {
    /// Validates the wake settings against the pure detector configuration rules.
    ///
    /// # Errors
    /// Returns [`OsError::InvalidInput`] when a value is outside the detector bounds.
    pub fn validate(&self) -> Result<(), OsError> {
        DetectorConfig {
            frame_ms: self.frame_ms,
            sensitivity: self.sensitivity,
            min_gap_ms: self.min_gap_ms,
            max_gap_ms: self.max_gap_ms,
            cooldown_ms: self.cooldown_ms,
        }
        .validate()
        .map(|_| ())
        .map_err(|error| OsError::InvalidInput(describe_config_error(error)))
    }
}

fn describe_config_error(error: DetectorConfigError) -> String {
    match error {
        DetectorConfigError::FrameWindow => "frame window must be 10 to 30 milliseconds".into(),
        DetectorConfigError::Sensitivity => "sensitivity must be between 0.0 and 1.0".into(),
        DetectorConfigError::PairingInterval => {
            "double-clap interval must be 120 to 900 milliseconds".into()
        }
        DetectorConfigError::Cooldown => "cooldown must be 1500 to 3000 milliseconds".into(),
    }
}

/// Mutable lifecycle state guarded by the controller.
#[derive(Debug, Clone)]
struct DesktopState {
    wake_config: WakeConfig,
    wake_paused: bool,
    listening_indicator: bool,
    main_window_visible: bool,
    login_at_startup: bool,
    shutting_down: bool,
    recoverable_errors: Vec<String>,
}

impl Default for DesktopState {
    fn default() -> Self {
        Self {
            wake_config: WakeConfig::default(),
            wake_paused: false,
            listening_indicator: false,
            main_window_visible: true,
            login_at_startup: false,
            shutting_down: false,
            recoverable_errors: Vec::new(),
        }
    }
}

/// A snapshot of the lifecycle state for the client and for assertions.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct LifecycleStatus {
    pub wake_paused: bool,
    pub listening: bool,
    pub main_window_visible: bool,
    pub login_at_startup: bool,
    pub shutting_down: bool,
    pub microphone_permission: PermissionState,
    pub audio_running: bool,
}

/// Overlay placement returned when the companion is opened.
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize)]
pub struct OverlayPlacement {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// Owns the OS adapters and drives the desktop lifecycle.
pub struct DesktopController {
    adapters: OsAdapters,
    state: Mutex<DesktopState>,
}

impl DesktopController {
    /// Creates a controller over the supplied adapter registry.
    #[must_use]
    pub fn new(adapters: OsAdapters) -> Self {
        Self {
            adapters,
            state: Mutex::new(DesktopState::default()),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, DesktopState>, OsError> {
        self.state
            .lock()
            .map_err(|_| OsError::Backend("controller state lock poisoned".into()))
    }

    /// Records a non-fatal issue that the client should surface without failing.
    pub fn note_recoverable(&self, message: impl Into<String>) {
        if let Ok(mut state) = self.lock() {
            state.recoverable_errors.push(message.into());
        }
    }

    /// Attempts to install the tray. Failure is non-fatal (Requirement 3.9):
    /// the main-window keyboard and text flow stay available and the error is
    /// recorded for the client.
    ///
    /// # Errors
    /// Returns the underlying [`OsError`] so the shell can log it; the error is
    /// also recorded as recoverable.
    pub fn install_tray(&self) -> Result<(), OsError> {
        match self.adapters.tray.install(TrayAction::defaults()) {
            Ok(()) => Ok(()),
            Err(error) => {
                self.note_recoverable(format!("tray unavailable: {error}"));
                Err(error)
            }
        }
    }

    /// Registers the default wake shortcut unless the student chose keyboard-only.
    ///
    /// # Errors
    /// Returns [`OsError`] when the shortcut backend rejects the chord.
    pub fn register_wake_shortcut(&self) -> Result<(), OsError> {
        let keyboard_only = self.lock()?.wake_config.keyboard_only;
        if keyboard_only {
            return self.adapters.shortcut.unregister_wake();
        }
        self.adapters.shortcut.register_wake(DEFAULT_WAKE_CHORD)
    }

    /// Reports platform capabilities and any recorded non-fatal issues.
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn capabilities(&self) -> Result<Capabilities, OsError> {
        let recoverable = self.lock()?.recoverable_errors.clone();
        let audio_input_devices = self.adapters.audio.input_devices().unwrap_or_default();
        let default_audio_input = self.adapters.audio.default_input_device().unwrap_or(None);
        let secure_store_available = self.adapters.secure_store.get("__probe__").is_ok();
        Ok(Capabilities {
            platform: self.adapters.platform,
            tray_available: self.adapters.tray.is_installed(),
            global_shortcut_available: true,
            microphone_permission: self.adapters.permission.microphone_state(),
            secure_store_available,
            notifications_available: true,
            login_item_available: self.adapters.login_item.is_enabled().is_ok(),
            login_item_mechanism: self.adapters.login_item.mechanism().to_owned(),
            audio_input_devices,
            default_audio_input,
            wake_default_chord: DEFAULT_WAKE_CHORD.to_owned(),
            recoverable_errors: recoverable,
        })
    }

    /// Returns the current wake configuration.
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn wake_config(&self) -> Result<WakeConfig, OsError> {
        Ok(self.lock()?.wake_config.clone())
    }

    /// Validates and stores new wake settings, restarting capture when listening.
    ///
    /// # Errors
    /// Returns [`OsError::InvalidInput`] for out-of-range settings, or an audio
    /// error when the stream cannot be reopened on the selected device.
    pub fn set_wake_config(&self, config: WakeConfig) -> Result<WakeConfig, OsError> {
        config.validate()?;
        let restart_device = {
            let mut state = self.lock()?;
            let listening = state.listening_indicator && !state.wake_paused;
            state.wake_config = config.clone();
            if state.wake_config.keyboard_only {
                state.listening_indicator = false;
            }
            listening.then(|| state.wake_config.microphone_device.clone())
        };
        // Restart the stream outside the lock so device selection changes apply.
        if let Some(device) = restart_device {
            self.adapters.audio_control.stop();
            if !config.keyboard_only {
                self.adapters.audio_control.start(device.as_deref())?;
            }
        }
        Ok(config)
    }

    /// Begins wake listening after confirming microphone permission is granted
    /// (Requirement 19.2). Without permission the keyboard and text flow remain.
    ///
    /// # Errors
    /// Returns [`OsError::PermissionDenied`] when permission is not granted,
    /// [`OsError::InvalidInput`] in keyboard-only mode, or an audio error.
    pub fn begin_wake_listening(&self) -> Result<(), OsError> {
        let device = {
            let state = self.lock()?;
            if state.wake_config.keyboard_only {
                return Err(OsError::InvalidInput(
                    "wake listening is disabled in keyboard-only mode".into(),
                ));
            }
            state.wake_config.microphone_device.clone()
        };
        let permission = self.adapters.permission.microphone_state();
        if !permission.allows_capture() {
            return Err(OsError::PermissionDenied(
                "microphone permission is required before opening a stream".into(),
            ));
        }
        self.adapters.audio_control.start(device.as_deref())?;
        let mut state = self.lock()?;
        state.wake_paused = false;
        state.listening_indicator = true;
        Ok(())
    }

    /// Pauses wake listening, stopping the microphone stream before reporting the
    /// paused state (Requirement 3.7).
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn pause_wake(&self) -> Result<(), OsError> {
        self.adapters.audio_control.stop();
        let mut state = self.lock()?;
        state.wake_paused = true;
        state.listening_indicator = false;
        Ok(())
    }

    /// Resumes wake listening when permitted and not in keyboard-only mode.
    ///
    /// # Errors
    /// Returns [`OsError`] when permission is missing or the stream fails to open.
    pub fn resume_wake(&self) -> Result<(), OsError> {
        {
            let mut state = self.lock()?;
            state.wake_paused = false;
            if state.wake_config.keyboard_only {
                state.listening_indicator = false;
                return Ok(());
            }
        }
        self.begin_wake_listening()
    }

    /// Records a microphone permission decision reported by the operating system
    /// and stops capture when access is no longer granted (Requirement 4.11).
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn record_microphone_permission(
        &self,
        state: PermissionState,
    ) -> Result<LifecycleStatus, OsError> {
        self.adapters.permission.set_microphone_state(state);
        if !state.allows_capture() {
            self.handle_device_unavailable();
        }
        self.status()
    }

    /// Handles a microphone device becoming unavailable: stops capture and clears
    /// the listening indicator (Requirement 4.11).
    pub fn handle_device_unavailable(&self) {
        self.adapters.audio_control.stop();
        if let Ok(mut state) = self.lock() {
            state.listening_indicator = false;
        }
    }

    /// Handles a main-window close request. The companion stays in the tray
    /// (Requirement 3.2); returns `true` when the window should hide rather than
    /// exit.
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn handle_main_window_close(&self) -> Result<bool, OsError> {
        let mut state = self.lock()?;
        if state.shutting_down {
            return Ok(false);
        }
        state.main_window_visible = false;
        Ok(true)
    }

    /// Marks the main window visible again (for example after an open-dashboard
    /// tray action).
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn show_main_window(&self) -> Result<(), OsError> {
        self.lock()?.main_window_visible = true;
        Ok(())
    }

    /// Computes centered overlay placement on the active display (Requirement 5.2).
    ///
    /// # Errors
    /// Returns [`OsError`] when display bounds cannot be queried.
    pub fn open_overlay(&self) -> Result<OverlayPlacement, OsError> {
        let bounds: Rect = self.adapters.display.active_display_bounds()?;
        let placement = bounds.centered_child(OVERLAY_WIDTH, OVERLAY_HEIGHT);
        Ok(OverlayPlacement {
            x: placement.x,
            y: placement.y,
            width: placement.width,
            height: placement.height,
        })
    }

    /// Sets opt-in start-at-login (Requirement 3.5).
    ///
    /// # Errors
    /// Returns [`OsError`] when the login-item registry cannot be updated.
    pub fn set_login_at_startup(&self, enabled: bool) -> Result<(), OsError> {
        self.adapters.login_item.set_enabled(enabled)?;
        self.lock()?.login_at_startup = enabled;
        Ok(())
    }

    /// Stores a session secret in operating-system secure storage (Requirement 19.3).
    ///
    /// # Errors
    /// Returns [`OsError`] when the secure store rejects the write.
    pub fn secure_session_set(&self, key: &str, secret: &[u8]) -> Result<(), OsError> {
        self.adapters.secure_store.put(key, secret)
    }

    /// Removes a session secret from operating-system secure storage.
    ///
    /// # Errors
    /// Returns [`OsError`] when the secure store rejects the deletion.
    pub fn secure_session_clear(&self, key: &str) -> Result<(), OsError> {
        self.adapters.secure_store.delete(key)
    }

    /// Emits a local notification through the platform adapter.
    ///
    /// # Errors
    /// Returns [`OsError`] when the notification cannot be presented.
    pub fn notify(&self, notification: Notification) -> Result<(), OsError> {
        self.adapters.notification.notify(notification)
    }

    /// Performs explicit-quit cleanup: stops the microphone stream, unregisters
    /// the wake shortcut, and marks shutdown (Requirement 3.3).
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn quit(&self) -> Result<(), OsError> {
        self.adapters.audio_control.stop();
        let _ = self.adapters.shortcut.unregister_wake();
        let mut state = self.lock()?;
        state.listening_indicator = false;
        state.wake_paused = true;
        state.shutting_down = true;
        Ok(())
    }

    /// Returns a snapshot of the current lifecycle state.
    ///
    /// # Errors
    /// Returns [`OsError`] when the state lock cannot be acquired.
    pub fn status(&self) -> Result<LifecycleStatus, OsError> {
        let state = self.lock()?;
        Ok(LifecycleStatus {
            wake_paused: state.wake_paused,
            listening: state.listening_indicator,
            main_window_visible: state.main_window_visible,
            login_at_startup: state.login_at_startup,
            shutting_down: state.shutting_down,
            microphone_permission: self.adapters.permission.microphone_state(),
            audio_running: self.adapters.audio_control.is_running(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{DesktopController, WakeConfig};
    use crate::os::adapters::{
        InMemorySecureStore, StatefulAudioControl, StatefulLoginItem, StatefulNotification,
        StatefulPermission, StatefulShortcut, StatefulTray, StaticDisplay, SystemAudioDevices,
    };
    use crate::os::{OsAdapters, PermissionState, Platform};

    fn controller_with(permission: PermissionState, tray_supported: bool) -> DesktopController {
        let adapters = OsAdapters {
            platform: Platform::MacOs,
            tray: Box::new(StatefulTray::new(tray_supported)),
            shortcut: Box::new(StatefulShortcut::default()),
            permission: Box::new(StatefulPermission::new(permission)),
            secure_store: Box::new(InMemorySecureStore::default()),
            notification: Box::new(StatefulNotification::new(true)),
            login_item: Box::new(StatefulLoginItem::new("test")),
            display: Box::new(StaticDisplay::default()),
            audio: Box::new(SystemAudioDevices),
            audio_control: Box::new(StatefulAudioControl::default()),
        };
        DesktopController::new(adapters)
    }

    #[test]
    fn tray_failure_is_recoverable_and_reported() {
        let controller = controller_with(PermissionState::Granted, false);
        assert!(controller.install_tray().is_err());
        let capabilities = controller.capabilities().expect("capabilities");
        assert!(!capabilities.tray_available);
        assert_eq!(capabilities.recoverable_errors.len(), 1);
        assert!(capabilities.recoverable_errors[0].contains("tray"));
    }

    #[test]
    fn wake_listening_requires_microphone_permission() {
        let denied = controller_with(PermissionState::Denied, true);
        assert!(denied.begin_wake_listening().is_err());
        assert!(!denied.status().expect("status").audio_running);

        let granted = controller_with(PermissionState::Granted, true);
        granted.begin_wake_listening().expect("listening");
        let status = granted.status().expect("status");
        assert!(status.audio_running);
        assert!(status.listening);
    }

    #[test]
    fn pause_stops_stream_before_reporting_paused() {
        let controller = controller_with(PermissionState::Granted, true);
        controller.begin_wake_listening().expect("listening");
        controller.pause_wake().expect("pause");
        let status = controller.status().expect("status");
        assert!(status.wake_paused);
        assert!(!status.listening);
        assert!(!status.audio_running);
    }

    #[test]
    fn quit_cleans_up_stream_and_marks_shutdown() {
        let controller = controller_with(PermissionState::Granted, true);
        controller.begin_wake_listening().expect("listening");
        controller.quit().expect("quit");
        let status = controller.status().expect("status");
        assert!(status.shutting_down);
        assert!(!status.audio_running);
        // After shutdown, close requests do not re-hide the window.
        assert!(!controller.handle_main_window_close().expect("close"));
    }

    #[test]
    fn close_hides_window_and_keeps_companion_in_tray() {
        let controller = controller_with(PermissionState::Granted, true);
        assert!(controller.handle_main_window_close().expect("close"));
        assert!(!controller.status().expect("status").main_window_visible);
        controller.show_main_window().expect("show");
        assert!(controller.status().expect("status").main_window_visible);
    }

    #[test]
    fn invalid_wake_config_is_rejected() {
        let controller = controller_with(PermissionState::Granted, true);
        let config = WakeConfig {
            min_gap_ms: 10,
            ..WakeConfig::default()
        };
        assert!(controller.set_wake_config(config).is_err());
    }

    #[test]
    fn keyboard_only_mode_disables_listening() {
        let controller = controller_with(PermissionState::Granted, true);
        let config = WakeConfig {
            keyboard_only: true,
            ..WakeConfig::default()
        };
        controller.set_wake_config(config).expect("config");
        assert!(controller.begin_wake_listening().is_err());
        controller
            .register_wake_shortcut()
            .expect("keyboard-only unregisters");
    }

    #[test]
    fn overlay_is_centered_on_active_display() {
        let controller = controller_with(PermissionState::Granted, true);
        let placement = controller.open_overlay().expect("overlay");
        assert!(placement.x > 0.0);
        assert!(placement.y > 0.0);
        assert!((placement.width - super::OVERLAY_WIDTH).abs() < f64::EPSILON);
    }

    #[test]
    fn secure_session_round_trips_through_store() {
        let controller = controller_with(PermissionState::Granted, true);
        controller
            .secure_session_set("session", b"token")
            .expect("set");
        controller.secure_session_clear("session").expect("clear");
    }

    #[test]
    fn device_loss_stops_capture() {
        let controller = controller_with(PermissionState::Granted, true);
        controller.begin_wake_listening().expect("listening");
        controller.handle_device_unavailable();
        let status = controller.status().expect("status");
        assert!(!status.audio_running);
        assert!(!status.listening);
    }
}

//! Concrete adapter implementations shared across platform registries.
//!
//! The registry contract is identical on every platform; the differences are
//! the login-item mechanism, the keyring backend, and capability flags. Tray,
//! shortcut, notification, and display effects that require a live Tauri handle
//! are applied by the shell glue and reported back through the controller, so
//! these adapters keep portable state and honor the trait contracts. In-memory
//! doubles used by tests live here alongside the production types.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use wake_detector::capture::{default_input_device_name, input_device_names};

use super::{
    AudioDeviceAdapter, DisplayAdapter, LoginItemAdapter, Notification, NotificationAdapter,
    OsError, PermissionAdapter, PermissionState, Rect, SecureStore, ShortcutAdapter, TrayAction,
    TrayAdapter, WakeAudioControl,
};

/// Tray adapter that records installation intent and reports availability.
#[derive(Debug, Default)]
pub struct StatefulTray {
    installed: AtomicBool,
    supported: bool,
}

impl StatefulTray {
    #[must_use]
    pub const fn new(supported: bool) -> Self {
        Self {
            installed: AtomicBool::new(false),
            supported,
        }
    }
}

impl TrayAdapter for StatefulTray {
    fn install(&self, actions: &[TrayAction]) -> Result<(), OsError> {
        if !self.supported {
            return Err(OsError::Unsupported("system tray is not available".into()));
        }
        if actions.is_empty() {
            return Err(OsError::InvalidInput(
                "tray requires at least one action".into(),
            ));
        }
        self.installed.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn is_installed(&self) -> bool {
        self.installed.load(Ordering::SeqCst)
    }
}

/// Global-shortcut adapter that validates chords and tracks registration.
#[derive(Debug, Default)]
pub struct StatefulShortcut {
    registered: Mutex<Option<String>>,
}

impl ShortcutAdapter for StatefulShortcut {
    fn register_wake(&self, chord: &str) -> Result<(), OsError> {
        if chord.trim().is_empty() {
            return Err(OsError::InvalidInput("wake chord must not be empty".into()));
        }
        let mut guard = self
            .registered
            .lock()
            .map_err(|_| OsError::Backend("shortcut registry lock poisoned".into()))?;
        *guard = Some(chord.to_owned());
        Ok(())
    }

    fn unregister_wake(&self) -> Result<(), OsError> {
        let mut guard = self
            .registered
            .lock()
            .map_err(|_| OsError::Backend("shortcut registry lock poisoned".into()))?;
        *guard = None;
        Ok(())
    }
}

/// Microphone permission adapter backed by a stored decision.
///
/// Platform registries seed the initial state; the real operating-system
/// prompt is presented by the shell during event wiring.
#[derive(Debug)]
pub struct StatefulPermission {
    state: Mutex<PermissionState>,
}

impl StatefulPermission {
    #[must_use]
    pub fn new(initial: PermissionState) -> Self {
        Self {
            state: Mutex::new(initial),
        }
    }
}

impl PermissionAdapter for StatefulPermission {
    fn microphone_state(&self) -> PermissionState {
        self.state
            .lock()
            .map_or(PermissionState::Unavailable, |guard| *guard)
    }

    fn request_microphone(&self) -> Result<PermissionState, OsError> {
        let mut guard = self
            .state
            .lock()
            .map_err(|_| OsError::Backend("permission lock poisoned".into()))?;
        if *guard == PermissionState::Prompt {
            // A real prompt is presented by the shell; default to denied until answered.
            *guard = PermissionState::Denied;
        }
        Ok(*guard)
    }

    fn set_microphone_state(&self, state: PermissionState) {
        if let Ok(mut guard) = self.state.lock() {
            *guard = state;
        }
    }
}

/// Notification adapter that reports support and accepts non-empty messages.
#[derive(Debug, Default)]
pub struct StatefulNotification {
    supported: bool,
}

impl StatefulNotification {
    #[must_use]
    pub const fn new(supported: bool) -> Self {
        Self { supported }
    }
}

impl NotificationAdapter for StatefulNotification {
    fn notify(&self, message: Notification) -> Result<(), OsError> {
        if !self.supported {
            return Err(OsError::Unsupported(
                "notifications are not available".into(),
            ));
        }
        if message.title.trim().is_empty() {
            return Err(OsError::InvalidInput(
                "notification title must not be empty".into(),
            ));
        }
        Ok(())
    }
}

/// Login-item adapter that tracks opt-in start-at-login state.
#[derive(Debug)]
pub struct StatefulLoginItem {
    enabled: AtomicBool,
    mechanism: &'static str,
}

impl StatefulLoginItem {
    #[must_use]
    pub const fn new(mechanism: &'static str) -> Self {
        Self {
            enabled: AtomicBool::new(false),
            mechanism,
        }
    }
}

impl LoginItemAdapter for StatefulLoginItem {
    fn set_enabled(&self, enabled: bool) -> Result<(), OsError> {
        self.enabled.store(enabled, Ordering::SeqCst);
        Ok(())
    }

    fn is_enabled(&self) -> Result<bool, OsError> {
        Ok(self.enabled.load(Ordering::SeqCst))
    }

    fn mechanism(&self) -> &'static str {
        self.mechanism
    }
}

/// Display adapter that returns fixed bounds until the shell supplies a monitor.
#[derive(Debug)]
pub struct StaticDisplay {
    bounds: Rect,
}

impl Default for StaticDisplay {
    fn default() -> Self {
        Self {
            bounds: Rect {
                x: 0.0,
                y: 0.0,
                width: 1920.0,
                height: 1080.0,
            },
        }
    }
}

impl DisplayAdapter for StaticDisplay {
    fn active_display_bounds(&self) -> Result<Rect, OsError> {
        Ok(self.bounds)
    }
}

/// Audio-device adapter backed by the same `cpal` host used for wake capture.
#[derive(Debug, Default)]
pub struct SystemAudioDevices;

impl AudioDeviceAdapter for SystemAudioDevices {
    fn input_devices(&self) -> Result<Vec<String>, OsError> {
        input_device_names().map_err(|error| OsError::Unavailable(error.to_string()))
    }

    fn default_input_device(&self) -> Result<Option<String>, OsError> {
        default_input_device_name().map_err(|error| OsError::Unavailable(error.to_string()))
    }
}

/// Wake audio control that tracks whether a stream is running.
///
/// Stopping is idempotent so pause and quit can always guarantee the stream is
/// closed before reporting their state.
#[derive(Debug, Default)]
pub struct StatefulAudioControl {
    running: AtomicBool,
}

impl WakeAudioControl for StatefulAudioControl {
    fn start(&self, _device: Option<&str>) -> Result<(), OsError> {
        self.running.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }

    fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }
}

/// Operating-system secure store backed by the platform keyring.
///
/// AI credentials and session secrets are written to the macOS Keychain,
/// Windows Credential Manager, or the Linux kernel keyring. Secrets never touch
/// frontend bundles, relational records, or plaintext configuration.
pub struct KeyringSecureStore {
    service: String,
}

impl KeyringSecureStore {
    #[must_use]
    pub fn new(service: impl Into<String>) -> Self {
        Self {
            service: service.into(),
        }
    }

    fn entry(&self, key: &str) -> Result<keyring::Entry, OsError> {
        if key.trim().is_empty() {
            return Err(OsError::InvalidInput(
                "secure-store key must not be empty".into(),
            ));
        }
        keyring::Entry::new(&self.service, key).map_err(|error| OsError::Backend(error.to_string()))
    }
}

impl SecureStore for KeyringSecureStore {
    fn put(&self, key: &str, secret: &[u8]) -> Result<(), OsError> {
        self.entry(key)?
            .set_secret(secret)
            .map_err(|error| OsError::Backend(error.to_string()))
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, OsError> {
        match self.entry(key)?.get_secret() {
            Ok(secret) => Ok(Some(secret)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(OsError::Backend(error.to_string())),
        }
    }

    fn delete(&self, key: &str) -> Result<(), OsError> {
        match self.entry(key)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(OsError::Backend(error.to_string())),
        }
    }
}

/// In-memory secure store used by tests and by builds without a keyring backend.
#[derive(Debug, Default)]
pub struct InMemorySecureStore {
    entries: Mutex<std::collections::BTreeMap<String, Vec<u8>>>,
}

impl SecureStore for InMemorySecureStore {
    fn put(&self, key: &str, secret: &[u8]) -> Result<(), OsError> {
        if key.trim().is_empty() {
            return Err(OsError::InvalidInput(
                "secure-store key must not be empty".into(),
            ));
        }
        self.entries
            .lock()
            .map_err(|_| OsError::Backend("secure store lock poisoned".into()))?
            .insert(key.to_owned(), secret.to_vec());
        Ok(())
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, OsError> {
        Ok(self
            .entries
            .lock()
            .map_err(|_| OsError::Backend("secure store lock poisoned".into()))?
            .get(key)
            .cloned())
    }

    fn delete(&self, key: &str) -> Result<(), OsError> {
        self.entries
            .lock()
            .map_err(|_| OsError::Backend("secure store lock poisoned".into()))?
            .remove(key);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{
        InMemorySecureStore, PermissionState, StatefulAudioControl, StatefulLoginItem,
        StatefulNotification, StatefulPermission, StatefulShortcut, StatefulTray,
    };
    use crate::os::{
        LoginItemAdapter, Notification, NotificationAdapter, PermissionAdapter, SecureStore,
        ShortcutAdapter, TrayAction, TrayAdapter, WakeAudioControl,
    };

    #[test]
    fn tray_install_requires_support_and_actions() {
        let unsupported = StatefulTray::new(false);
        assert!(unsupported.install(TrayAction::defaults()).is_err());

        let tray = StatefulTray::new(true);
        assert!(tray.install(&[]).is_err());
        tray.install(TrayAction::defaults()).expect("install");
        assert!(tray.is_installed());
    }

    #[test]
    fn shortcut_rejects_empty_chord_and_round_trips() {
        let shortcut = StatefulShortcut::default();
        assert!(shortcut.register_wake("  ").is_err());
        shortcut
            .register_wake("CmdOrCtrl+Shift+Space")
            .expect("register");
        shortcut.unregister_wake().expect("unregister");
    }

    #[test]
    fn permission_request_resolves_prompt_to_denied() {
        let permission = StatefulPermission::new(PermissionState::Prompt);
        assert_eq!(
            permission.request_microphone().expect("request"),
            PermissionState::Denied
        );
        permission.set_microphone_state(PermissionState::Granted);
        assert_eq!(permission.microphone_state(), PermissionState::Granted);
    }

    #[test]
    fn notification_requires_support_and_title() {
        let disabled = StatefulNotification::new(false);
        assert!(disabled
            .notify(Notification {
                title: "hi".into(),
                body: "x".into()
            })
            .is_err());
        let enabled = StatefulNotification::new(true);
        assert!(enabled
            .notify(Notification {
                title: " ".into(),
                body: "x".into()
            })
            .is_err());
        enabled
            .notify(Notification {
                title: "Listening".into(),
                body: "Wake ready".into(),
            })
            .expect("notify");
    }

    #[test]
    fn login_item_defaults_disabled_and_toggles() {
        let login_item = StatefulLoginItem::new("test-mechanism");
        assert!(!login_item.is_enabled().expect("read"));
        login_item.set_enabled(true).expect("enable");
        assert!(login_item.is_enabled().expect("read"));
    }

    #[test]
    fn audio_control_start_and_stop_are_idempotent() {
        let control = StatefulAudioControl::default();
        assert!(!control.is_running());
        control.start(None).expect("start");
        control.start(Some("Mic")).expect("start again");
        assert!(control.is_running());
        control.stop();
        control.stop();
        assert!(!control.is_running());
    }

    #[test]
    fn in_memory_secure_store_round_trips_and_deletes() {
        let store = InMemorySecureStore::default();
        assert!(store.put("", b"x").is_err());
        store.put("session", b"token").expect("put");
        assert_eq!(store.get("session").expect("get"), Some(b"token".to_vec()));
        store.delete("session").expect("delete");
        assert_eq!(store.get("session").expect("get"), None);
        store.delete("missing").expect("delete missing is ok");
    }
}

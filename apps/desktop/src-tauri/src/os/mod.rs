//! Portable operating-system adapter registry for the Zipity desktop shell.
//!
//! Every native concern (tray, global shortcut, microphone permission, secure
//! storage, notifications, login item, display bounds, and audio devices) is
//! expressed as a platform-neutral trait. macOS ships first; Windows and Linux
//! implement the same [`OsAdapters`] registry without changing Tauri commands or
//! Python contracts. The registry carries no Tauri or network types, so its logic
//! is exercised in isolation by unit tests with in-memory doubles.

use std::error::Error;
use std::fmt;

pub mod adapters;
pub mod platform;

pub use platform::current_adapters;

/// Default wake shortcut. `CmdOrCtrl` maps to `Cmd` on macOS and `Ctrl` elsewhere.
pub const DEFAULT_WAKE_CHORD: &str = "CmdOrCtrl+Shift+Space";

/// Fixed overlay size used when centering the companion on the active display.
pub const OVERLAY_WIDTH: f64 = 460.0;
/// Fixed overlay height used when centering the companion on the active display.
pub const OVERLAY_HEIGHT: f64 = 260.0;

/// Safe, serializable failures surfaced by native adapters.
///
/// Variants never carry secrets or raw audio; messages are operator-facing.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "kind", content = "message", rename_all = "snake_case")]
pub enum OsError {
    /// The capability is not available on the current platform build.
    Unsupported(String),
    /// The operating system denied a required permission.
    PermissionDenied(String),
    /// A device or facility is temporarily unavailable.
    Unavailable(String),
    /// The secure store or another backend reported a failure.
    Backend(String),
    /// The caller supplied an invalid value.
    InvalidInput(String),
}

impl fmt::Display for OsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported(message) => write!(formatter, "unsupported capability: {message}"),
            Self::PermissionDenied(message) => write!(formatter, "permission denied: {message}"),
            Self::Unavailable(message) => write!(formatter, "unavailable: {message}"),
            Self::Backend(message) => write!(formatter, "backend error: {message}"),
            Self::InvalidInput(message) => write!(formatter, "invalid input: {message}"),
        }
    }
}

impl Error for OsError {}

/// The platform an [`OsAdapters`] registry targets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Platform {
    MacOs,
    Windows,
    Linux,
}

impl Platform {
    /// The platform this binary was compiled for.
    #[must_use]
    pub const fn current() -> Self {
        #[cfg(target_os = "macos")]
        {
            Self::MacOs
        }
        #[cfg(target_os = "windows")]
        {
            Self::Windows
        }
        #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
        {
            Self::Linux
        }
    }
}

/// Operating-system microphone permission state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionState {
    /// Access has been granted; capture may be opened.
    Granted,
    /// Access was explicitly denied; keyboard and text flows remain available.
    Denied,
    /// Access has not been decided; a request must be shown to the student.
    Prompt,
    /// No microphone facility is available on this device.
    Unavailable,
}

impl PermissionState {
    /// Whether an audio stream may be opened in this state.
    #[must_use]
    pub const fn allows_capture(self) -> bool {
        matches!(self, Self::Granted)
    }
}

/// A tray menu action offered by the background companion.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrayAction {
    OpenDashboard,
    Wake,
    MicrophoneSettings,
    PauseWake,
    ResumeWake,
    SyncStatus,
    Quit,
}

impl TrayAction {
    /// The default tray actions required by the desktop companion.
    #[must_use]
    pub const fn defaults() -> &'static [Self] {
        &[
            Self::OpenDashboard,
            Self::Wake,
            Self::MicrophoneSettings,
            Self::PauseWake,
            Self::SyncStatus,
            Self::Quit,
        ]
    }
}

/// A local, user-facing notification. Bodies never contain secrets or raw audio.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Notification {
    pub title: String,
    pub body: String,
}

/// Screen bounds in logical pixels.
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl Rect {
    /// Centers a `width` x `height` rectangle inside these bounds.
    #[must_use]
    pub fn centered_child(self, width: f64, height: f64) -> Rect {
        Rect {
            x: self.x + (self.width - width) / 2.0,
            y: self.y + (self.height - height) / 2.0,
            width,
            height,
        }
    }
}

/// Installs and updates the background tray surface.
pub trait TrayAdapter: Send + Sync {
    /// Installs the tray with the supplied actions.
    ///
    /// # Errors
    /// Returns [`OsError`] when the platform cannot create a tray surface.
    fn install(&self, actions: &[TrayAction]) -> Result<(), OsError>;

    /// Whether a tray surface is currently installed.
    fn is_installed(&self) -> bool;
}

/// Registers the global wake shortcut.
pub trait ShortcutAdapter: Send + Sync {
    /// Registers a chord (for example `CmdOrCtrl+Shift+Space`) as the wake trigger.
    ///
    /// # Errors
    /// Returns [`OsError`] when the chord is invalid or already held elsewhere.
    fn register_wake(&self, chord: &str) -> Result<(), OsError>;

    /// Releases a previously registered wake chord.
    ///
    /// # Errors
    /// Returns [`OsError`] when the platform reports a release failure.
    fn unregister_wake(&self) -> Result<(), OsError>;
}

/// Reports and requests operating-system microphone permission.
pub trait PermissionAdapter: Send + Sync {
    /// The current microphone permission state.
    fn microphone_state(&self) -> PermissionState;

    /// Requests microphone permission from the operating system.
    ///
    /// # Errors
    /// Returns [`OsError`] when the request cannot be presented.
    fn request_microphone(&self) -> Result<PermissionState, OsError>;

    /// Records a permission decision reported by the operating system (for
    /// example a `permission://changed` event). The default is a no-op.
    fn set_microphone_state(&self, _state: PermissionState) {}
}

/// Stores AI credentials and session secrets in operating-system secure storage.
///
/// Implementations must never persist secrets in frontend bundles, relational
/// records, or plaintext configuration (Requirement 19.3).
pub trait SecureStore: Send + Sync {
    /// Writes a secret under `key`.
    ///
    /// # Errors
    /// Returns [`OsError`] when the secure store rejects the write.
    fn put(&self, key: &str, secret: &[u8]) -> Result<(), OsError>;

    /// Reads a secret by `key`, returning `None` when absent.
    ///
    /// # Errors
    /// Returns [`OsError`] when the secure store cannot be read.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, OsError>;

    /// Removes a secret by `key`. Missing keys are not an error.
    ///
    /// # Errors
    /// Returns [`OsError`] when the secure store rejects the deletion.
    fn delete(&self, key: &str) -> Result<(), OsError>;
}

/// Emits local, user-facing notifications.
pub trait NotificationAdapter: Send + Sync {
    /// Displays a local notification.
    ///
    /// # Errors
    /// Returns [`OsError`] when the platform cannot present the notification.
    fn notify(&self, message: Notification) -> Result<(), OsError>;
}

/// Controls opt-in start-at-login behavior.
pub trait LoginItemAdapter: Send + Sync {
    /// Enables or disables launching at operating-system login.
    ///
    /// # Errors
    /// Returns [`OsError`] when the login-item registry cannot be updated.
    fn set_enabled(&self, enabled: bool) -> Result<(), OsError>;

    /// Whether start-at-login is currently enabled.
    ///
    /// # Errors
    /// Returns [`OsError`] when the login-item registry cannot be read.
    fn is_enabled(&self) -> Result<bool, OsError>;

    /// The platform mechanism backing the login item (for capability reporting).
    fn mechanism(&self) -> &'static str {
        "unknown"
    }
}

/// Reports the bounds of the display that should host the overlay.
pub trait DisplayAdapter: Send + Sync {
    /// Bounds of the currently active display.
    ///
    /// # Errors
    /// Returns [`OsError`] when no display can be queried.
    fn active_display_bounds(&self) -> Result<Rect, OsError>;
}

/// Enumerates microphone input devices.
pub trait AudioDeviceAdapter: Send + Sync {
    /// Names of available input devices.
    ///
    /// # Errors
    /// Returns [`OsError`] when the audio host cannot be queried.
    fn input_devices(&self) -> Result<Vec<String>, OsError>;

    /// Name of the default input device, if any.
    ///
    /// # Errors
    /// Returns [`OsError`] when the audio host cannot be queried.
    fn default_input_device(&self) -> Result<Option<String>, OsError>;
}

/// Starts and stops the local wake audio stream.
///
/// The stream is always stopped before pause or quit is reported so that no
/// microphone capture outlives an explicit user request (Requirements 3.3, 3.7).
pub trait WakeAudioControl: Send + Sync {
    /// Opens the wake audio stream on the supplied device (or the default).
    ///
    /// # Errors
    /// Returns [`OsError`] when the stream cannot be opened.
    fn start(&self, device: Option<&str>) -> Result<(), OsError>;

    /// Stops the wake audio stream. Safe to call when already stopped.
    fn stop(&self);

    /// Whether the wake audio stream is currently running.
    fn is_running(&self) -> bool;
}

/// The set of native adapters composed for a single platform.
pub struct OsAdapters {
    pub platform: Platform,
    pub tray: Box<dyn TrayAdapter>,
    pub shortcut: Box<dyn ShortcutAdapter>,
    pub permission: Box<dyn PermissionAdapter>,
    pub secure_store: Box<dyn SecureStore>,
    pub notification: Box<dyn NotificationAdapter>,
    pub login_item: Box<dyn LoginItemAdapter>,
    pub display: Box<dyn DisplayAdapter>,
    pub audio: Box<dyn AudioDeviceAdapter>,
    pub audio_control: Box<dyn WakeAudioControl>,
}

/// A platform capability report returned to the desktop client.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Capabilities {
    pub platform: Platform,
    pub tray_available: bool,
    pub global_shortcut_available: bool,
    pub microphone_permission: PermissionState,
    pub secure_store_available: bool,
    pub notifications_available: bool,
    pub login_item_available: bool,
    pub login_item_mechanism: String,
    pub audio_input_devices: Vec<String>,
    pub default_audio_input: Option<String>,
    pub wake_default_chord: String,
    /// Non-fatal issues (for example a failed tray install) the client should surface.
    pub recoverable_errors: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::{OsError, PermissionState, Platform, Rect, TrayAction};

    #[test]
    fn permission_only_allows_capture_when_granted() {
        assert!(PermissionState::Granted.allows_capture());
        assert!(!PermissionState::Denied.allows_capture());
        assert!(!PermissionState::Prompt.allows_capture());
        assert!(!PermissionState::Unavailable.allows_capture());
    }

    #[test]
    fn default_tray_actions_expose_required_controls() {
        let actions = TrayAction::defaults();
        assert!(actions.contains(&TrayAction::OpenDashboard));
        assert!(actions.contains(&TrayAction::PauseWake));
        assert!(actions.contains(&TrayAction::SyncStatus));
        assert!(actions.contains(&TrayAction::Quit));
    }

    #[test]
    fn centered_child_is_inside_parent_bounds() {
        let parent = Rect {
            x: 0.0,
            y: 0.0,
            width: 1000.0,
            height: 800.0,
        };
        let child = parent.centered_child(460.0, 260.0);
        assert!((child.x - 270.0).abs() < f64::EPSILON);
        assert!((child.y - 270.0).abs() < f64::EPSILON);
    }

    #[test]
    fn os_error_serializes_with_tagged_kind() {
        let json = serde_json::to_string(&OsError::PermissionDenied("mic".into())).expect("json");
        assert!(json.contains("permission_denied"));
        assert!(json.contains("mic"));
    }

    #[test]
    fn current_platform_matches_build_target() {
        let platform = Platform::current();
        #[cfg(target_os = "windows")]
        assert_eq!(platform, Platform::Windows);
        #[cfg(target_os = "macos")]
        assert_eq!(platform, Platform::MacOs);
        let _ = platform;
    }
}

//! Platform-specific registry assembly.
//!
//! Each platform composes the same [`OsAdapters`] contract. macOS ships first;
//! the Windows and Linux seams differ only in their login-item mechanism and
//! keyring backend, never in the trait surface consumed by the shell commands
//! or the Python contracts.

use super::adapters::{
    KeyringSecureStore, StaticDisplay, StatefulAudioControl, StatefulLoginItem,
    StatefulNotification, StatefulPermission, StatefulShortcut, StatefulTray, SystemAudioDevices,
};
use super::{OsAdapters, PermissionState, Platform};

/// The keyring service namespace for `FastLearner` secrets.
pub const KEYRING_SERVICE: &str = "app.fastlearner.desktop";

/// Builds the adapter registry for the platform this binary targets.
#[must_use]
pub fn current_adapters() -> OsAdapters {
    let platform = Platform::current();
    let login_mechanism = match platform {
        Platform::MacOs => "macos-login-item",
        Platform::Windows => "windows-run-key",
        Platform::Linux => "xdg-autostart",
    };
    OsAdapters {
        platform,
        tray: Box::new(StatefulTray::new(true)),
        shortcut: Box::new(StatefulShortcut::default()),
        permission: Box::new(StatefulPermission::new(PermissionState::Prompt)),
        secure_store: Box::new(KeyringSecureStore::new(KEYRING_SERVICE)),
        notification: Box::new(StatefulNotification::new(true)),
        login_item: Box::new(StatefulLoginItem::new(login_mechanism)),
        display: Box::new(StaticDisplay::default()),
        audio: Box::new(SystemAudioDevices),
        audio_control: Box::new(StatefulAudioControl::default()),
    }
}

#[cfg(test)]
mod tests {
    use super::current_adapters;
    use crate::os::Platform;

    #[test]
    fn current_registry_reports_target_platform() {
        let adapters = current_adapters();
        assert_eq!(adapters.platform, Platform::current());
    }
}

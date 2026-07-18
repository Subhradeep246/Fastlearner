//! Privacy-preserving, on-device double-clap wake detection.
//!
//! The pure detector accepts bounded mono frames. Audio capture is isolated in
//! [`capture`]; neither module has a network, AI, or durable raw-audio boundary.

mod detector;
mod diagnostics;
pub mod capture;

pub use detector::{
    AudioError, AudioFrame, DetectorConfig, DetectorConfigError, DetectorState, FrameSource,
    WakeDetector, WakeEvent, WakeSink,
};
pub use diagnostics::{
    AggregateDiagnosticSnapshot, AggregateDiagnostics, DiagnosticConfigBucket,
};

/// Identifies the privacy-sensitive native component in aggregate diagnostics.
#[must_use]
pub const fn component_name() -> &'static str {
    "wake-detector"
}

#[cfg(test)]
mod tests {
    use super::component_name;

    #[test]
    fn component_has_stable_name() {
        assert_eq!(component_name(), "wake-detector");
    }
}

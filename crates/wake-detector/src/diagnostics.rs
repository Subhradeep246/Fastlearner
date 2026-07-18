/// Coarse configuration grouping; no audio or sample-derived values are retained.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiagnosticConfigBucket {
    LowSensitivity,
    StandardSensitivity,
    HighSensitivity,
}

/// An opted-in aggregate-only diagnostic snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AggregateDiagnosticSnapshot {
    pub evaluations: u64,
    pub expected_detections: u64,
    pub detected_events: u64,
    pub false_positives: u64,
    pub false_negatives: u64,
    pub last_evaluation_at_ms: Option<u64>,
    pub config_bucket: Option<DiagnosticConfigBucket>,
}

/// Local aggregate diagnostics. Disabled instances retain nothing.
#[derive(Debug, Clone, Default)]
pub struct AggregateDiagnostics {
    enabled: bool,
    snapshot: AggregateDiagnosticSnapshot,
}

impl AggregateDiagnostics {
    #[must_use]
    pub const fn new(enabled: bool) -> Self {
        Self { enabled, snapshot: AggregateDiagnosticSnapshot {
            evaluations: 0, expected_detections: 0, detected_events: 0,
            false_positives: 0, false_negatives: 0,
            last_evaluation_at_ms: None, config_bucket: None,
        } }
    }

    #[must_use]
    pub const fn enabled(&self) -> bool { self.enabled }

    pub fn set_enabled(&mut self, enabled: bool) { self.enabled = enabled; }

    pub fn clear(&mut self) { self.snapshot = AggregateDiagnosticSnapshot::default(); }

    /// Records only expected/detected labels and a timestamp after explicit opt-in.
    pub fn record_evaluation(
        &mut self,
        expected: bool,
        detected: bool,
        timestamp_ms: u64,
        bucket: DiagnosticConfigBucket,
    ) {
        if !self.enabled { return; }
        self.snapshot.evaluations = self.snapshot.evaluations.saturating_add(1);
        self.snapshot.expected_detections = self.snapshot.expected_detections
            .saturating_add(u64::from(expected));
        self.snapshot.detected_events = self.snapshot.detected_events
            .saturating_add(u64::from(detected));
        self.snapshot.false_positives = self.snapshot.false_positives
            .saturating_add(u64::from(!expected && detected));
        self.snapshot.false_negatives = self.snapshot.false_negatives
            .saturating_add(u64::from(expected && !detected));
        self.snapshot.last_evaluation_at_ms = Some(timestamp_ms);
        self.snapshot.config_bucket = Some(bucket);
    }

    #[must_use]
    pub const fn snapshot(&self) -> AggregateDiagnosticSnapshot { self.snapshot }
}

#[cfg(test)]
mod tests {
    use super::{AggregateDiagnostics, DiagnosticConfigBucket};

    #[test]
    fn diagnostics_require_opt_in_and_retain_aggregates_only() {
        let mut diagnostics = AggregateDiagnostics::new(false);
        diagnostics.record_evaluation(true, false, 20, DiagnosticConfigBucket::StandardSensitivity);
        assert_eq!(diagnostics.snapshot().evaluations, 0);

        diagnostics.set_enabled(true);
        diagnostics.record_evaluation(true, false, 40, DiagnosticConfigBucket::StandardSensitivity);
        diagnostics.record_evaluation(false, true, 60, DiagnosticConfigBucket::StandardSensitivity);
        let snapshot = diagnostics.snapshot();
        assert_eq!(snapshot.false_negatives, 1);
        assert_eq!(snapshot.false_positives, 1);
        assert_eq!(snapshot.last_evaluation_at_ms, Some(60));

        diagnostics.clear();
        assert_eq!(diagnostics.snapshot().evaluations, 0);
    }
}

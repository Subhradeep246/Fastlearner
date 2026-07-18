use std::fmt;

/// A bounded mono analysis frame whose samples are cleared on drop.
pub struct AudioFrame {
    timestamp_ms: u64,
    sample_rate_hz: u32,
    samples: Box<[f32]>,
}

impl AudioFrame {
    /// Creates an in-memory mono frame. Samples are never serialized by this crate.
    #[must_use]
    pub fn new(timestamp_ms: u64, sample_rate_hz: u32, samples: Vec<f32>) -> Self {
        Self { timestamp_ms, sample_rate_hz, samples: samples.into_boxed_slice() }
    }

    #[must_use]
    pub const fn timestamp_ms(&self) -> u64 { self.timestamp_ms }

    #[must_use]
    pub const fn sample_rate_hz(&self) -> u32 { self.sample_rate_hz }

    #[must_use]
    pub fn samples(&self) -> &[f32] { &self.samples }

    fn duration_ms(&self) -> Option<u64> {
        (self.sample_rate_hz > 0).then(|| {
            u64::try_from(self.samples.len()).unwrap_or(u64::MAX)
                .saturating_mul(1_000) / u64::from(self.sample_rate_hz)
        })
    }
}

impl Drop for AudioFrame {
    fn drop(&mut self) { self.samples.fill(0.0); }
}

/// Safe failures exposed by capture and analysis boundaries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioError {
    InvalidFrame(&'static str),
    NonMonotonicTime { previous_ms: u64, received_ms: u64 },
    CaptureUnavailable(String),
}

impl fmt::Display for AudioError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidFrame(message) => write!(formatter, "invalid audio frame: {message}"),
            Self::NonMonotonicTime { previous_ms, received_ms } => write!(
                formatter,
                "audio timestamp moved backward from {previous_ms}ms to {received_ms}ms"
            ),
            Self::CaptureUnavailable(message) => write!(formatter, "audio capture unavailable: {message}"),
        }
    }
}

impl std::error::Error for AudioError {}

pub trait FrameSource {
    fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError>;
}

pub trait WakeSink {
    fn emit(&mut self, event: WakeEvent);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WakeEvent { pub detected_at_ms: u64 }

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DetectorConfig {
    pub frame_ms: u16,
    pub sensitivity: f32,
    pub min_gap_ms: u16,
    pub max_gap_ms: u16,
    pub cooldown_ms: u16,
}

impl Default for DetectorConfig {
    fn default() -> Self {
        Self { frame_ms: 20, sensitivity: 0.6, min_gap_ms: 120, max_gap_ms: 900, cooldown_ms: 2_000 }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetectorConfigError {
    FrameWindow, Sensitivity, PairingInterval, Cooldown,
}

impl DetectorConfig {
    pub fn validate(self) -> Result<Self, DetectorConfigError> {
        if !(10..=30).contains(&self.frame_ms) { return Err(DetectorConfigError::FrameWindow); }
        if !self.sensitivity.is_finite() || !(0.0..=1.0).contains(&self.sensitivity) {
            return Err(DetectorConfigError::Sensitivity);
        }
        if self.min_gap_ms < 120 || self.max_gap_ms > 900 || self.min_gap_ms > self.max_gap_ms {
            return Err(DetectorConfigError::PairingInterval);
        }
        if !(1_500..=3_000).contains(&self.cooldown_ms) {
            return Err(DetectorConfigError::Cooldown);
        }
        Ok(self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetectorState {
    Listening,
    OneTransient { at_ms: u64 },
    Cooldown { until_ms: u64 },
    Paused,
    Unavailable,
}

#[derive(Debug, Clone, Copy)]
struct TransientCandidate {
    started_at_ms: u64,
    peak_at_ms: u64,
    peak: f32,
    maximum_rms: f32,
    onset_rms: f32,
}

pub struct WakeDetector<F: FrameSource, S: WakeSink> {
    config: DetectorConfig,
    source: F,
    sink: S,
    state: DetectorState,
    noise_floor: f32,
    candidate: Option<TransientCandidate>,
    last_timestamp_ms: Option<u64>,
}

impl<F: FrameSource, S: WakeSink> WakeDetector<F, S> {
    pub fn new(config: DetectorConfig, source: F, sink: S) -> Result<Self, DetectorConfigError> {
        Ok(Self {
            config: config.validate()?, source, sink,
            state: DetectorState::Listening,
            noise_floor: 0.002,
            candidate: None,
            last_timestamp_ms: None,
        })
    }

    #[must_use]
    pub const fn state(&self) -> DetectorState { self.state }

    #[must_use]
    pub const fn noise_floor(&self) -> f32 { self.noise_floor }

    pub fn pause(&mut self) { self.candidate = None; self.state = DetectorState::Paused; }

    pub fn resume(&mut self) {
        if self.state == DetectorState::Paused { self.state = DetectorState::Listening; }
    }

    pub fn mark_unavailable(&mut self) {
        self.candidate = None;
        self.state = DetectorState::Unavailable;
    }

    pub fn mark_available(&mut self) {
        if self.state == DetectorState::Unavailable { self.state = DetectorState::Listening; }
    }

    /// Pulls and analyzes at most one frame from the capture adapter.
    pub fn poll(&mut self) -> Result<bool, AudioError> {
        let Some(frame) = self.source.next_mono_frame()? else { return Ok(false); };
        self.process_frame(&frame)?;
        Ok(true)
    }

    /// Advances expiry and cooldown using a monotonic timestamp without audio.
    pub fn advance_time(&mut self, timestamp_ms: u64) -> Result<(), AudioError> {
        self.check_monotonic(timestamp_ms)?;
        self.expire(timestamp_ms);
        Ok(())
    }

    pub fn process_frame(&mut self, frame: &AudioFrame) -> Result<(), AudioError> {
        let timestamp_ms = frame.timestamp_ms();
        self.check_monotonic(timestamp_ms)?;
        let duration_ms = frame.duration_ms().ok_or(AudioError::InvalidFrame("sample rate is zero"))?;
        if !(10..=30).contains(&duration_ms) {
            return Err(AudioError::InvalidFrame("analysis frame must be 10 to 30 milliseconds"));
        }
        if frame.samples().is_empty() {
            return Err(AudioError::InvalidFrame("analysis frame is empty"));
        }
        self.expire(timestamp_ms);
        if matches!(self.state, DetectorState::Paused | DetectorState::Unavailable | DetectorState::Cooldown { .. }) {
            return Ok(());
        }

        let metrics = FrameMetrics::from_samples(frame.samples());
        let threshold = (self.noise_floor * self.threshold_multiplier()).max(0.008);
        let release_threshold = (self.noise_floor * 1.8).max(0.004);
        if let Some(mut candidate) = self.candidate.take() {
            if metrics.rms > release_threshold {
                if metrics.peak > candidate.peak {
                    candidate.peak = metrics.peak;
                    candidate.peak_at_ms = timestamp_ms;
                }
                candidate.maximum_rms = candidate.maximum_rms.max(metrics.rms);
                if timestamp_ms.saturating_sub(candidate.started_at_ms) <= 90 {
                    self.candidate = Some(candidate);
                } else {
                    self.update_noise_floor(metrics.rms);
                }
            } else {
                if Self::valid_transient(candidate, timestamp_ms) {
                    self.accept_transient(candidate.started_at_ms);
                }
                self.update_noise_floor(metrics.rms);
            }
        } else if metrics.rms >= threshold
            && metrics.peak >= threshold * 1.8
            && metrics.crest_factor >= 2.0
        {
            self.candidate = Some(TransientCandidate {
                started_at_ms: timestamp_ms,
                peak_at_ms: timestamp_ms,
                peak: metrics.peak,
                maximum_rms: metrics.rms,
                onset_rms: metrics.rms,
            });
        } else {
            self.update_noise_floor(metrics.rms);
        }
        Ok(())
    }

    fn check_monotonic(&mut self, timestamp_ms: u64) -> Result<(), AudioError> {
        if let Some(previous_ms) = self.last_timestamp_ms {
            if timestamp_ms < previous_ms {
                return Err(AudioError::NonMonotonicTime { previous_ms, received_ms: timestamp_ms });
            }
        }
        self.last_timestamp_ms = Some(timestamp_ms);
        Ok(())
    }

    fn expire(&mut self, timestamp_ms: u64) {
        // A transient is timed from its onset, but it is only accepted once its
        // decay frame arrives. When a candidate is in progress, use its onset as
        // the pairing reference so a valid second clap whose onset lands on the
        // max-gap boundary is not lost to expiry when its later decay frame is
        // processed.
        let pairing_reference_ms = self
            .candidate
            .map_or(timestamp_ms, |candidate| candidate.started_at_ms.min(timestamp_ms));
        match self.state {
            DetectorState::OneTransient { at_ms }
                if pairing_reference_ms.saturating_sub(at_ms) > u64::from(self.config.max_gap_ms) =>
            {
                self.state = DetectorState::Listening;
            }
            DetectorState::Cooldown { until_ms } if timestamp_ms >= until_ms => {
                self.state = DetectorState::Listening;
            }
            _ => {}
        }
        if self.candidate.is_some_and(|candidate| {
            timestamp_ms.saturating_sub(candidate.started_at_ms) > 90
        }) {
            self.candidate = None;
        }
    }

    fn threshold_multiplier(&self) -> f32 { 6.0 - self.config.sensitivity * 3.0 }

    fn update_noise_floor(&mut self, rms: f32) {
        let bounded = rms.clamp(0.000_1, 0.25);
        self.noise_floor = (self.noise_floor * 0.94 + bounded * 0.06).clamp(0.000_1, 0.25);
    }

    fn valid_transient(candidate: TransientCandidate, ended_at_ms: u64) -> bool {
        let duration = ended_at_ms.saturating_sub(candidate.started_at_ms);
        let rise = candidate.peak_at_ms.saturating_sub(candidate.started_at_ms);
        let peak_to_rms = candidate.peak / candidate.maximum_rms.max(f32::EPSILON);
        (10..=90).contains(&duration)
            && rise <= 40
            && peak_to_rms >= 1.8
            && candidate.maximum_rms >= candidate.onset_rms * 0.85
    }

    fn accept_transient(&mut self, at_ms: u64) {
        match self.state {
            DetectorState::Listening => self.state = DetectorState::OneTransient { at_ms },
            DetectorState::OneTransient { at_ms: first_at_ms } => {
                let gap = at_ms.saturating_sub(first_at_ms);
                if gap < u64::from(self.config.min_gap_ms) || gap > u64::from(self.config.max_gap_ms) {
                    self.state = DetectorState::OneTransient { at_ms };
                } else {
                    self.sink.emit(WakeEvent { detected_at_ms: at_ms });
                    self.state = DetectorState::Cooldown {
                        until_ms: at_ms.saturating_add(u64::from(self.config.cooldown_ms)),
                    };
                }
            }
            DetectorState::Cooldown { .. } | DetectorState::Paused | DetectorState::Unavailable => {}
        }
    }

    #[must_use]
    pub fn into_parts(self) -> (F, S) { (self.source, self.sink) }
}

#[derive(Debug, Clone, Copy)]
struct FrameMetrics { rms: f32, peak: f32, crest_factor: f32 }

impl FrameMetrics {
    fn from_samples(samples: &[f32]) -> Self {
        let mut previous = 0.0_f32;
        let mut energy = 0.0_f32;
        let mut peak = 0.0_f32;
        for &sample in samples {
            let sanitized = if sample.is_finite() { sample.clamp(-1.0, 1.0) } else { 0.0 };
            let high_passed = sanitized - previous * 0.97;
            previous = sanitized;
            energy += high_passed * high_passed;
            peak = peak.max(high_passed.abs());
        }
        let rms = (energy / samples.len() as f32).sqrt();
        Self { rms, peak, crest_factor: peak / rms.max(f32::EPSILON) }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AudioError, AudioFrame, DetectorConfig, DetectorConfigError, DetectorState, FrameSource,
        WakeDetector, WakeEvent, WakeSink,
    };

    #[derive(Default)]
    struct EmptySource;
    impl FrameSource for EmptySource {
        fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError> { Ok(None) }
    }

    #[derive(Default)]
    struct EventSink(Vec<WakeEvent>);
    impl WakeSink for EventSink {
        fn emit(&mut self, event: WakeEvent) { self.0.push(event); }
    }

    fn frame(timestamp_ms: u64, clap: bool) -> AudioFrame {
        let mut samples = vec![0.000_1; 20];
        if clap {
            samples[2] = 1.0;
            samples[3] = -0.8;
            samples[4] = 0.2;
        }
        AudioFrame::new(timestamp_ms, 1_000, samples)
    }

    fn feed_clap(detector: &mut WakeDetector<EmptySource, EventSink>, at_ms: u64) {
        detector.process_frame(&frame(at_ms, true)).expect("clap onset");
        detector.process_frame(&frame(at_ms + 20, false)).expect("clap decay");
    }

    fn event_count(detector: WakeDetector<EmptySource, EventSink>) -> usize {
        detector.into_parts().1.0.len()
    }

    #[test]
    fn validates_analysis_pairing_and_cooldown_bounds() {
        assert_eq!(DetectorConfig { frame_ms: 9, ..DetectorConfig::default() }.validate(), Err(DetectorConfigError::FrameWindow));
        assert_eq!(DetectorConfig { sensitivity: f32::NAN, ..DetectorConfig::default() }.validate(), Err(DetectorConfigError::Sensitivity));
        assert_eq!(DetectorConfig { min_gap_ms: 119, ..DetectorConfig::default() }.validate(), Err(DetectorConfigError::PairingInterval));
        assert_eq!(DetectorConfig { cooldown_ms: 1_499, ..DetectorConfig::default() }.validate(), Err(DetectorConfigError::Cooldown));
    }

    #[test]
    fn emits_once_at_both_pairing_boundaries_and_suppresses_cooldown() {
        for second_at in [120, 900] {
            let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
            feed_clap(&mut detector, 0);
            feed_clap(&mut detector, second_at);
            assert_eq!(detector.state(), DetectorState::Cooldown { until_ms: second_at + 2_000 });
            feed_clap(&mut detector, second_at + 100);
            assert_eq!(event_count(detector), 1);
        }
    }

    #[test]
    fn expires_stale_first_transient_using_monotonic_time() {
        let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
        feed_clap(&mut detector, 0);
        detector.advance_time(901).expect("monotonic");
        assert_eq!(detector.state(), DetectorState::Listening);
        feed_clap(&mut detector, 1_000);
        assert_eq!(event_count(detector), 0);
    }

    #[test]
    fn rejects_backward_timestamps() {
        let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
        detector.advance_time(50).expect("first timestamp");
        assert_eq!(detector.advance_time(49), Err(AudioError::NonMonotonicTime { previous_ms: 50, received_ms: 49 }));
    }

    #[test]
    fn pause_and_unavailable_states_ignore_audio_until_restored() {
        let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
        detector.pause();
        feed_clap(&mut detector, 0);
        assert_eq!(detector.state(), DetectorState::Paused);
        detector.resume();
        detector.mark_unavailable();
        feed_clap(&mut detector, 100);
        assert_eq!(detector.state(), DetectorState::Unavailable);
        detector.mark_available();
        assert_eq!(detector.state(), DetectorState::Listening);
        assert_eq!(event_count(detector), 0);
    }

    #[test]
    fn adapts_noise_floor_without_retaining_frames() {
        let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
        let initial = detector.noise_floor();
        for timestamp in (0..400).step_by(20) {
            let samples = vec![0.02; 20];
            detector.process_frame(&AudioFrame::new(timestamp, 1_000, samples)).expect("quiet frame");
        }
        assert_ne!(detector.noise_floor(), initial);
        assert_eq!(detector.state(), DetectorState::Listening);
    }

    #[test]
    fn rejects_frames_outside_ten_to_thirty_milliseconds() {
        let mut detector = WakeDetector::new(DetectorConfig::default(), EmptySource, EventSink::default()).expect("config");
        assert!(detector.process_frame(&AudioFrame::new(0, 1_000, vec![0.0; 9])).is_err());
    }
}

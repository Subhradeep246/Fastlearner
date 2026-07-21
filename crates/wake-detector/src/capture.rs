use std::collections::VecDeque;

use crate::{AudioError, AudioFrame, FrameSource};

/// Converts bounded interleaved capture buffers to mono in memory.
#[derive(Debug, Clone)]
pub struct InMemoryDownmixer {
    channels: usize,
    max_interleaved_samples: usize,
}

impl InMemoryDownmixer {
    /// Creates a downmixer with an explicit upper bound on each input buffer.
    pub fn new(channels: usize, max_interleaved_samples: usize) -> Result<Self, AudioError> {
        if channels == 0 {
            return Err(AudioError::InvalidFrame("channel count must be positive"));
        }
        if max_interleaved_samples < channels {
            return Err(AudioError::InvalidFrame(
                "capture bound is smaller than one sample frame",
            ));
        }
        Ok(Self {
            channels,
            max_interleaved_samples,
        })
    }

    /// Selects the highest-energy channel, sanitizes non-finite values, and clamps the result.
    ///
    /// Selecting one channel for the entire buffer preserves transient shape and avoids
    /// cancelling stereo inputs whose channels have opposite polarity.
    pub fn downmix(&self, interleaved: &[f32]) -> Result<Vec<f32>, AudioError> {
        if interleaved.len() > self.max_interleaved_samples {
            return Err(AudioError::InvalidFrame(
                "capture buffer exceeds configured bound",
            ));
        }
        if interleaved.len() % self.channels != 0 {
            return Err(AudioError::InvalidFrame(
                "interleaved buffer is not channel aligned",
            ));
        }
        let sanitize = |sample: f32| {
            if sample.is_finite() {
                sample.clamp(-1.0, 1.0)
            } else {
                0.0
            }
        };
        let mut channel_energy = vec![0.0_f32; self.channels];
        for channel_frame in interleaved.chunks_exact(self.channels) {
            for (channel, &sample) in channel_frame.iter().enumerate() {
                let sanitized = sanitize(sample);
                channel_energy[channel] += sanitized * sanitized;
            }
        }
        let selected_channel = channel_energy
            .iter()
            .enumerate()
            .max_by(|left, right| left.1.total_cmp(right.1))
            .map_or(0, |(channel, _)| channel);
        let mono = interleaved
            .chunks_exact(self.channels)
            .map(|channel_frame| sanitize(channel_frame[selected_channel]))
            .collect();
        Ok(mono)
    }
}

/// A fixed-capacity frame queue suitable for an audio callback boundary.
pub struct BoundedCaptureAdapter {
    downmixer: InMemoryDownmixer,
    sample_rate_hz: u32,
    max_frames: usize,
    frames: VecDeque<AudioFrame>,
    available: bool,
}

impl BoundedCaptureAdapter {
    pub fn new(
        channels: usize,
        sample_rate_hz: u32,
        max_interleaved_samples: usize,
        max_frames: usize,
    ) -> Result<Self, AudioError> {
        if sample_rate_hz == 0 {
            return Err(AudioError::InvalidFrame("sample rate must be positive"));
        }
        if max_frames == 0 {
            return Err(AudioError::InvalidFrame(
                "frame queue must be bounded above zero",
            ));
        }
        Ok(Self {
            downmixer: InMemoryDownmixer::new(channels, max_interleaved_samples)?,
            sample_rate_hz,
            max_frames,
            frames: VecDeque::with_capacity(max_frames),
            available: true,
        })
    }

    /// Downmixes and enqueues one analysis frame; oldest data is dropped at capacity.
    pub fn push_interleaved(
        &mut self,
        timestamp_ms: u64,
        samples: &[f32],
    ) -> Result<(), AudioError> {
        if !self.available {
            return Err(AudioError::CaptureUnavailable(
                "selected microphone is unavailable".into(),
            ));
        }
        let mono = self.downmixer.downmix(samples)?;
        if self.frames.len() == self.max_frames {
            self.frames.pop_front();
        }
        self.frames
            .push_back(AudioFrame::new(timestamp_ms, self.sample_rate_hz, mono));
        Ok(())
    }

    pub fn set_available(&mut self, available: bool) {
        self.available = available;
        if !available {
            self.frames.clear();
        }
    }

    #[must_use]
    pub fn queued_frames(&self) -> usize {
        self.frames.len()
    }
}

impl FrameSource for BoundedCaptureAdapter {
    fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError> {
        if !self.available {
            return Err(AudioError::CaptureUnavailable(
                "selected microphone is unavailable".into(),
            ));
        }
        Ok(self.frames.pop_front())
    }
}

#[cfg(feature = "native-capture")]
mod native {
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

    use super::BoundedCaptureAdapter;
    use crate::{AudioError, AudioFrame, DetectorConfig, FrameSource};

    struct NativeAccumulator {
        capture: BoundedCaptureAdapter,
        pending: Vec<f32>,
        frame_interleaved: usize,
        frame_ms: u64,
        next_timestamp_ms: Option<u64>,
        error: Option<String>,
    }

    impl NativeAccumulator {
        fn push(&mut self, samples: impl IntoIterator<Item = f32>, elapsed_ms: u64) {
            for sample in samples {
                self.pending.push(sample);
                if self.pending.len() == self.frame_interleaved {
                    let onset = elapsed_ms.saturating_sub(self.frame_ms);
                    let timestamp = self.next_timestamp_ms.unwrap_or(onset).max(onset);
                    let frame = std::mem::replace(
                        &mut self.pending,
                        Vec::with_capacity(self.frame_interleaved),
                    );
                    if let Err(error) = self.capture.push_interleaved(timestamp, &frame) {
                        self.error = Some(error.to_string());
                        return;
                    }
                    self.next_timestamp_ms = Some(timestamp.saturating_add(self.frame_ms));
                }
            }
        }
    }

    /// Running native microphone stream backed by fixed-size in-memory buffers.
    pub struct CpalCaptureAdapter {
        stream: cpal::Stream,
        inner: Arc<Mutex<NativeAccumulator>>,
    }

    /// Returns stable input-device names for settings and diagnostics.
    pub fn input_device_names() -> Result<Vec<String>, AudioError> {
        let host = cpal::default_host();
        let mut names = host
            .input_devices()
            .map_err(unavailable)?
            .filter_map(|device| device.name().ok())
            .collect::<Vec<_>>();
        names.sort();
        names.dedup();
        Ok(names)
    }

    /// Returns the current system-default microphone name, when available.
    pub fn default_input_device_name() -> Result<Option<String>, AudioError> {
        let host = cpal::default_host();
        host.default_input_device()
            .map(|device| device.name().map_err(unavailable))
            .transpose()
    }

    impl CpalCaptureAdapter {
        /// Opens a selected input device (or the system default) for local capture only.
        pub fn start(
            device_name: Option<&str>,
            detector: DetectorConfig,
        ) -> Result<Self, AudioError> {
            detector
                .validate()
                .map_err(|_| AudioError::InvalidFrame("invalid detector configuration"))?;
            let host = cpal::default_host();
            let device = match device_name {
                Some(expected) => host
                    .input_devices()
                    .map_err(unavailable)?
                    .find(|device| device.name().is_ok_and(|name| name == expected))
                    .ok_or_else(|| {
                        AudioError::CaptureUnavailable("selected microphone was not found".into())
                    })?,
                None => host.default_input_device().ok_or_else(|| {
                    AudioError::CaptureUnavailable("no default microphone is available".into())
                })?,
            };
            let supported = device.default_input_config().map_err(unavailable)?;
            let channels = usize::from(supported.channels());
            let sample_rate_hz = supported.sample_rate().0;
            let mono_samples =
                usize::try_from(u64::from(sample_rate_hz) * u64::from(detector.frame_ms) / 1_000)
                    .map_err(|_| {
                    AudioError::InvalidFrame("analysis frame size exceeds platform limits")
                })?;
            let frame_interleaved =
                mono_samples
                    .checked_mul(channels)
                    .ok_or(AudioError::InvalidFrame(
                        "analysis frame size exceeds platform limits",
                    ))?;
            let inner = Arc::new(Mutex::new(NativeAccumulator {
                capture: BoundedCaptureAdapter::new(
                    channels,
                    sample_rate_hz,
                    frame_interleaved,
                    8,
                )?,
                pending: Vec::with_capacity(frame_interleaved),
                frame_interleaved,
                frame_ms: u64::from(detector.frame_ms),
                next_timestamp_ms: None,
                error: None,
            }));
            let origin = Instant::now();
            let stream_config: cpal::StreamConfig = supported.clone().into();
            let stream = match supported.sample_format() {
                cpal::SampleFormat::F32 => {
                    let shared = Arc::clone(&inner);
                    let error_shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[f32], _| {
                            push(&shared, &callback_origin, data.iter().copied())
                        },
                        move |error| report_error(&error_shared, error),
                        None,
                    )
                }
                cpal::SampleFormat::I16 => {
                    let shared = Arc::clone(&inner);
                    let error_shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[i16], _| {
                            push(
                                &shared,
                                &callback_origin,
                                data.iter()
                                    .map(|&sample| f32::from(sample) / f32::from(i16::MAX)),
                            )
                        },
                        move |error| report_error(&error_shared, error),
                        None,
                    )
                }
                cpal::SampleFormat::U16 => {
                    let shared = Arc::clone(&inner);
                    let error_shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[u16], _| {
                            push(
                                &shared,
                                &callback_origin,
                                data.iter()
                                    .map(|&sample| (f32::from(sample) - 32_768.0) / 32_768.0),
                            )
                        },
                        move |error| report_error(&error_shared, error),
                        None,
                    )
                }
                _ => {
                    return Err(AudioError::CaptureUnavailable(
                        "microphone sample format is unsupported".into(),
                    ))
                }
            }
            .map_err(unavailable)?;
            stream.play().map_err(unavailable)?;
            Ok(Self { stream, inner })
        }
    }

    impl FrameSource for CpalCaptureAdapter {
        fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError> {
            let _keep_stream_alive = &self.stream;
            let mut inner = self.inner.lock().map_err(|_| {
                AudioError::CaptureUnavailable("microphone queue lock failed".into())
            })?;
            if let Some(error) = inner.error.take() {
                return Err(AudioError::CaptureUnavailable(error));
            }
            inner.capture.next_mono_frame()
        }
    }

    fn push(
        inner: &Arc<Mutex<NativeAccumulator>>,
        origin: &Instant,
        samples: impl IntoIterator<Item = f32>,
    ) {
        if let Ok(mut inner) = inner.lock() {
            let elapsed_ms = u64::try_from(origin.elapsed().as_millis()).unwrap_or(u64::MAX);
            inner.push(samples, elapsed_ms);
        }
    }

    fn report_error(inner: &Arc<Mutex<NativeAccumulator>>, error: impl std::fmt::Display) {
        if let Ok(mut inner) = inner.lock() {
            inner.error = Some(error.to_string());
        }
    }

    fn unavailable(error: impl std::fmt::Display) -> AudioError {
        AudioError::CaptureUnavailable(error.to_string())
    }
}

#[cfg(feature = "native-capture")]
pub use native::{default_input_device_name, input_device_names, CpalCaptureAdapter};

#[cfg(test)]
mod tests {
    use crate::{DetectorConfig, FrameSource, WakeDetector, WakeEvent, WakeSink};

    use super::{BoundedCaptureAdapter, InMemoryDownmixer};

    #[test]
    fn selects_highest_energy_channel_and_sanitizes_samples() {
        let downmixer = InMemoryDownmixer::new(2, 8).expect("valid downmixer");
        let mono = downmixer
            .downmix(&[1.0, 0.2, 0.5, 0.2, f32::NAN, 0.2])
            .expect("aligned input");
        assert_eq!(mono, vec![1.0, 0.5, 0.0]);
    }

    #[derive(Default)]
    struct EventSink(Vec<WakeEvent>);

    impl WakeSink for EventSink {
        fn emit(&mut self, event: WakeEvent) {
            self.0.push(event);
        }
    }

    fn anti_phase_stereo_frame(clap: bool) -> Vec<f32> {
        let mut mono = vec![0.000_1; 20];
        if clap {
            mono[2] = 1.0;
            mono[3] = -0.8;
            mono[4] = 0.2;
        }
        mono.into_iter()
            .flat_map(|sample| [sample, -sample])
            .collect()
    }

    #[test]
    fn anti_phase_stereo_claps_remain_detectable() {
        let stereo_clap = anti_phase_stereo_frame(true);
        let mono_clap = InMemoryDownmixer::new(2, 40)
            .expect("valid downmixer")
            .downmix(&stereo_clap)
            .expect("aligned stereo clap");
        assert!(mono_clap.iter().any(|sample| sample.abs() > 0.9));

        let mut capture = BoundedCaptureAdapter::new(2, 1_000, 40, 4).expect("valid capture");
        for (timestamp, clap) in [(0, true), (20, false), (420, true), (440, false)] {
            capture
                .push_interleaved(timestamp, &anti_phase_stereo_frame(clap))
                .expect("bounded stereo frame");
        }
        let mut detector =
            WakeDetector::new(DetectorConfig::default(), capture, EventSink::default())
                .expect("valid detector");
        while detector.poll().expect("valid captured frame") {}
        assert_eq!(detector.into_parts().1 .0.len(), 1);
    }

    #[test]
    fn queue_is_bounded_and_device_loss_clears_audio() {
        let mut capture = BoundedCaptureAdapter::new(1, 1_000, 30, 2).expect("valid capture");
        for timestamp in [0, 20, 40] {
            capture
                .push_interleaved(timestamp, &[0.0; 20])
                .expect("bounded frame");
        }
        assert_eq!(capture.queued_frames(), 2);
        assert_eq!(
            capture
                .next_mono_frame()
                .expect("available")
                .expect("frame")
                .timestamp_ms(),
            20
        );
        capture.set_available(false);
        assert_eq!(capture.queued_frames(), 0);
        assert!(capture.next_mono_frame().is_err());
    }

    #[test]
    fn rejects_unbounded_or_misaligned_capture_buffers() {
        let downmixer = InMemoryDownmixer::new(2, 4).expect("valid downmixer");
        assert!(downmixer.downmix(&[0.0; 6]).is_err());
        assert!(downmixer.downmix(&[0.0; 3]).is_err());
    }
}

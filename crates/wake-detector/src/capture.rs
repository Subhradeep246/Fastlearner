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
        if channels == 0 { return Err(AudioError::InvalidFrame("channel count must be positive")); }
        if max_interleaved_samples < channels {
            return Err(AudioError::InvalidFrame("capture bound is smaller than one sample frame"));
        }
        Ok(Self { channels, max_interleaved_samples })
    }

    /// Averages channels, sanitizes non-finite values, and clamps the result.
    pub fn downmix(&self, interleaved: &[f32]) -> Result<Vec<f32>, AudioError> {
        if interleaved.len() > self.max_interleaved_samples {
            return Err(AudioError::InvalidFrame("capture buffer exceeds configured bound"));
        }
        if interleaved.len() % self.channels != 0 {
            return Err(AudioError::InvalidFrame("interleaved buffer is not channel aligned"));
        }
        let mut mono = Vec::with_capacity(interleaved.len() / self.channels);
        for channel_frame in interleaved.chunks_exact(self.channels) {
            let sum = channel_frame.iter().map(|sample| {
                if sample.is_finite() { sample.clamp(-1.0, 1.0) } else { 0.0 }
            }).sum::<f32>();
            mono.push((sum / self.channels as f32).clamp(-1.0, 1.0));
        }
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
        if sample_rate_hz == 0 { return Err(AudioError::InvalidFrame("sample rate must be positive")); }
        if max_frames == 0 { return Err(AudioError::InvalidFrame("frame queue must be bounded above zero")); }
        Ok(Self {
            downmixer: InMemoryDownmixer::new(channels, max_interleaved_samples)?,
            sample_rate_hz,
            max_frames,
            frames: VecDeque::with_capacity(max_frames),
            available: true,
        })
    }

    /// Downmixes and enqueues one analysis frame; oldest data is dropped at capacity.
    pub fn push_interleaved(&mut self, timestamp_ms: u64, samples: &[f32]) -> Result<(), AudioError> {
        if !self.available {
            return Err(AudioError::CaptureUnavailable("selected microphone is unavailable".into()));
        }
        let mono = self.downmixer.downmix(samples)?;
        if self.frames.len() == self.max_frames { self.frames.pop_front(); }
        self.frames.push_back(AudioFrame::new(timestamp_ms, self.sample_rate_hz, mono));
        Ok(())
    }

    pub fn set_available(&mut self, available: bool) {
        self.available = available;
        if !available { self.frames.clear(); }
    }

    #[must_use]
    pub fn queued_frames(&self) -> usize { self.frames.len() }
}

impl FrameSource for BoundedCaptureAdapter {
    fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError> {
        if !self.available {
            return Err(AudioError::CaptureUnavailable("selected microphone is unavailable".into()));
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
                    let _result = self.capture.push_interleaved(timestamp, &frame);
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

    impl CpalCaptureAdapter {
        /// Opens a selected input device (or the system default) for local capture only.
        pub fn start(device_name: Option<&str>, detector: DetectorConfig) -> Result<Self, AudioError> {
            detector.validate().map_err(|_| AudioError::InvalidFrame("invalid detector configuration"))?;
            let host = cpal::default_host();
            let device = match device_name {
                Some(expected) => host.input_devices()
                    .map_err(unavailable)?
                    .find(|device| device.name().is_ok_and(|name| name == expected))
                    .ok_or_else(|| AudioError::CaptureUnavailable("selected microphone was not found".into()))?,
                None => host.default_input_device()
                    .ok_or_else(|| AudioError::CaptureUnavailable("no default microphone is available".into()))?,
            };
            let supported = device.default_input_config().map_err(unavailable)?;
            let channels = usize::from(supported.channels());
            let sample_rate_hz = supported.sample_rate().0;
            let mono_samples = usize::try_from(
                u64::from(sample_rate_hz) * u64::from(detector.frame_ms) / 1_000,
            ).map_err(|_| AudioError::InvalidFrame("analysis frame size exceeds platform limits"))?;
            let frame_interleaved = mono_samples.checked_mul(channels)
                .ok_or(AudioError::InvalidFrame("analysis frame size exceeds platform limits"))?;
            let inner = Arc::new(Mutex::new(NativeAccumulator {
                capture: BoundedCaptureAdapter::new(channels, sample_rate_hz, frame_interleaved, 8)?,
                pending: Vec::with_capacity(frame_interleaved),
                frame_interleaved,
                frame_ms: u64::from(detector.frame_ms),
                next_timestamp_ms: None,
            }));
            let origin = Instant::now();
            let stream_config: cpal::StreamConfig = supported.clone().into();
            let stream = match supported.sample_format() {
                cpal::SampleFormat::F32 => {
                    let shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[f32], _| push(&shared, &callback_origin, data.iter().copied()),
                        |_error| {}, None,
                    )
                }
                cpal::SampleFormat::I16 => {
                    let shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[i16], _| push(
                            &shared,
                            &callback_origin,
                            data.iter().map(|&sample| f32::from(sample) / f32::from(i16::MAX)),
                        ),
                        |_error| {}, None,
                    )
                }
                cpal::SampleFormat::U16 => {
                    let shared = Arc::clone(&inner);
                    let callback_origin = origin;
                    device.build_input_stream(
                        &stream_config,
                        move |data: &[u16], _| push(
                            &shared,
                            &callback_origin,
                            data.iter().map(|&sample| (f32::from(sample) - 32_768.0) / 32_768.0),
                        ),
                        |_error| {}, None,
                    )
                }
                _ => return Err(AudioError::CaptureUnavailable("microphone sample format is unsupported".into())),
            }.map_err(unavailable)?;
            stream.play().map_err(unavailable)?;
            Ok(Self { stream, inner })
        }
    }

    impl FrameSource for CpalCaptureAdapter {
        fn next_mono_frame(&mut self) -> Result<Option<AudioFrame>, AudioError> {
            let _keep_stream_alive = &self.stream;
            self.inner.lock()
                .map_err(|_| AudioError::CaptureUnavailable("microphone queue lock failed".into()))?
                .capture
                .next_mono_frame()
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

    fn unavailable(error: impl std::fmt::Display) -> AudioError {
        AudioError::CaptureUnavailable(error.to_string())
    }
}

#[cfg(feature = "native-capture")]
pub use native::CpalCaptureAdapter;

#[cfg(test)]
mod tests {
    use crate::FrameSource;

    use super::{BoundedCaptureAdapter, InMemoryDownmixer};

    #[test]
    fn downmixes_stereo_in_memory_and_sanitizes_samples() {
        let downmixer = InMemoryDownmixer::new(2, 8).expect("valid downmixer");
        let mono = downmixer.downmix(&[1.0, -1.0, 0.5, 0.5, f32::NAN, 1.0]).expect("aligned input");
        assert_eq!(mono, vec![0.0, 0.5, 0.5]);
    }

    #[test]
    fn queue_is_bounded_and_device_loss_clears_audio() {
        let mut capture = BoundedCaptureAdapter::new(1, 1_000, 30, 2).expect("valid capture");
        for timestamp in [0, 20, 40] {
            capture.push_interleaved(timestamp, &[0.0; 20]).expect("bounded frame");
        }
        assert_eq!(capture.queued_frames(), 2);
        assert_eq!(capture.next_mono_frame().expect("available").expect("frame").timestamp_ms(), 20);
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

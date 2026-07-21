//! Live, privacy-preserving microphone runtime for double-clap wake detection.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde::Serialize;
use tauri::Emitter;
use wake_detector::capture::CpalCaptureAdapter;
use wake_detector::{DetectorConfig, TransientEvent, WakeDetector, WakeEvent, WakeSink};

use crate::controller::WakeConfig;
use crate::os::OsError;

#[derive(Debug, Clone, Copy, Serialize)]
struct WakePayload {
    at_ms: u64,
}

struct TauriWakeSink {
    app: tauri::AppHandle,
}

impl WakeSink for TauriWakeSink {
    fn emit(&mut self, event: WakeEvent) {
        let _ = self.app.emit(
            "wake://detected",
            WakePayload {
                at_ms: event.detected_at_ms,
            },
        );
    }

    fn emit_transient(&mut self, event: TransientEvent) {
        let _ = self.app.emit(
            "wake://clap",
            WakePayload {
                at_ms: event.detected_at_ms,
            },
        );
    }
}

struct RunningWake {
    stop: Arc<AtomicBool>,
    worker: JoinHandle<()>,
}

/// Owns the native capture worker. Dropping or stopping it closes the stream.
#[derive(Default)]
pub struct WakeRuntime {
    running: Mutex<Option<RunningWake>>,
}

impl WakeRuntime {
    /// Starts capture and waits until the microphone is confirmed open.
    pub fn start(&self, app: tauri::AppHandle, config: WakeConfig) -> Result<(), OsError> {
        config.validate()?;
        self.stop();
        if config.keyboard_only {
            return Ok(());
        }

        let detector_config = DetectorConfig {
            frame_ms: config.frame_ms,
            sensitivity: config.sensitivity,
            min_gap_ms: config.min_gap_ms,
            max_gap_ms: config.max_gap_ms,
            cooldown_ms: config.cooldown_ms,
        };
        let selected_device = config.microphone_device;
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = Arc::clone(&stop);
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let error_app = app.clone();

        let worker = thread::Builder::new()
            .name("fastlearner-wake-detector".into())
            .spawn(move || {
                let source =
                    match CpalCaptureAdapter::start(selected_device.as_deref(), detector_config) {
                        Ok(source) => source,
                        Err(error) => {
                            let message = error.to_string();
                            let _ = ready_tx.send(Err(message.clone()));
                            let _ = error_app.emit("wake://error", message);
                            return;
                        }
                    };
                let mut detector =
                    match WakeDetector::new(detector_config, source, TauriWakeSink { app }) {
                        Ok(detector) => detector,
                        Err(error) => {
                            let message = format!("invalid wake detector configuration: {error:?}");
                            let _ = ready_tx.send(Err(message.clone()));
                            let _ = error_app.emit("wake://error", message);
                            return;
                        }
                    };
                let _ = ready_tx.send(Ok(()));

                while !worker_stop.load(Ordering::Acquire) {
                    match detector.poll() {
                        Ok(true) => {}
                        Ok(false) => thread::sleep(Duration::from_millis(4)),
                        Err(error) => {
                            let message = error.to_string();
                            let _ = error_app.emit("wake://error", message);
                            break;
                        }
                    }
                }
            })
            .map_err(|error| OsError::Unavailable(error.to_string()))?;

        match ready_rx.recv_timeout(Duration::from_secs(4)) {
            Ok(Ok(())) => {
                let mut guard = self
                    .running
                    .lock()
                    .map_err(|_| OsError::Backend("wake runtime lock poisoned".into()))?;
                *guard = Some(RunningWake { stop, worker });
                Ok(())
            }
            Ok(Err(message)) => {
                let _ = worker.join();
                Err(OsError::Unavailable(message))
            }
            Err(error) => {
                stop.store(true, Ordering::Release);
                let _ = worker.join();
                Err(OsError::Unavailable(format!(
                    "microphone startup timed out: {error}"
                )))
            }
        }
    }

    /// Stops capture idempotently and waits for the worker to release the mic.
    pub fn stop(&self) {
        let running = self.running.lock().ok().and_then(|mut guard| guard.take());
        if let Some(running) = running {
            running.stop.store(true, Ordering::Release);
            let _ = running.worker.join();
        }
    }
}

impl Drop for WakeRuntime {
    fn drop(&mut self) {
        self.stop();
    }
}

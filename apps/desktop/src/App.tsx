import { AppShell } from "@fastlearner/ui";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { useCompanion } from "./features/companion";
import "./companion.css";
import "./clap.css";

const VOICE_API = "http://127.0.0.1:8001/v1";
const SESSION_REFRESH_SKEW_MS = 30_000;
let cachedVoiceSession: { token: string; expiresAt: number } | null = null;
let pendingVoiceSession: Promise<string> | null = null;

async function sessionToken(forceRefresh = false): Promise<string> {
  if (forceRefresh) cachedVoiceSession = null;
  if (cachedVoiceSession && cachedVoiceSession.expiresAt - SESSION_REFRESH_SKEW_MS > Date.now()) {
    return cachedVoiceSession.token;
  }
  if (pendingVoiceSession) return pendingVoiceSession;

  const request = fetch(`${VOICE_API}/local/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona: "learner" }),
  }).then(async response => {
    if (!response.ok) throw new Error("Local voice session unavailable");
    const session = (await response.json()) as { token: string; expires_at: string };
    const parsedExpiry = Date.parse(session.expires_at);
    cachedVoiceSession = {
      token: session.token,
      expiresAt: Number.isFinite(parsedExpiry) ? parsedExpiry : Date.now() + 60_000,
    };
    return session.token;
  }).finally(() => {
    pendingVoiceSession = null;
  });
  pendingVoiceSession = request;
  return request;
}

function CompanionLayer() {
  const companion = useCompanion();
  const visible = companion.state.overlay !== "idle";
  const [listening, setListening] = useState(false);
  const [voiceError, setVoiceError] = useState("");
  const [clapSeen, setClapSeen] = useState(false);
  const autoStarted = useRef(false);
  const captureActive = useRef(false);

  async function listenForCommand() {
    if (captureActive.current) return;
    captureActive.current = true;
    setVoiceError(""); setListening(true);
    window.dispatchEvent(new Event("fastlearner:open-assistant"));
    const tokenPromise = sessionToken();
    // Attach immediately: API failure can happen while microphone capture runs.
    void tokenPromise.catch(() => undefined);
    let stream: MediaStream | undefined;
    let context: AudioContext | undefined;
    let recorder: MediaRecorder | undefined;
    let monitor: number | undefined;
    try {
      // Release CPAL before WebView2 opens MediaRecorder. Some Windows drivers
      // expose only one reliable capture client even in shared mode.
      await companion.pause();
      await new Promise(resolve => window.setTimeout(resolve, 140));
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
      const activeRecorder = new MediaRecorder(stream, { mimeType }); recorder = activeRecorder; const chunks: Blob[] = [];
      context = new AudioContext({ latencyHint: "interactive" });
      if (context.state === "suspended") await context.resume().catch(() => undefined);
      const analyser = context.createAnalyser(); analyser.fftSize = 512;
      context.createMediaStreamSource(stream).connect(analyser); const levels = new Uint8Array(analyser.fftSize);
      let heardVoice = false; let voicedFrames = 0; let silentSince = 0; const startedAt = performance.now();
      let noiseFloor = 0.002; const calibration: number[] = [];
      const stopped = new Promise<Blob>((resolve, reject) => {
        activeRecorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
        activeRecorder.onstop = () => resolve(new Blob(chunks, { type: mimeType }));
        activeRecorder.onerror = () => reject(new Error("Microphone recording stopped unexpectedly."));
      });
      activeRecorder.start(250);
      const analyserRunning = context.state === "running";
      const captureLimit = analyserRunning ? 10_000 : 5_500;
      monitor = window.setInterval(() => {
        analyser.getByteTimeDomainData(levels);
        let energy = 0;
        for (let index = 0; index < levels.length; index += 1) {
          energy += ((levels[index]! - 128) / 128) ** 2;
        }
        const rms = Math.sqrt(energy / levels.length);
        const now = performance.now();
        if (now - startedAt < 450) {
          calibration.push(rms);
          const sorted = [...calibration].sort((a, b) => a - b);
          noiseFloor = sorted[Math.floor(sorted.length * 0.25)] ?? noiseFloor;
        }
        const voiceThreshold = Math.max(0.005, noiseFloor * 2.2);
        if (rms > voiceThreshold) { voicedFrames += 1; heardVoice = voicedFrames >= 2; silentSince = 0; }
        else if (heardVoice && !silentSince) silentSince = now;
        if (((silentSince && now - silentSince > 950) || now - startedAt > captureLimit) && activeRecorder.state === "recording") activeRecorder.stop();
      }, 80);
      const audio = await stopped;
      if (audio.size < 200) throw new Error("Microphone produced no audio. Check Windows microphone access.");
      let token = await tokenPromise;
      let response = await fetch(`${VOICE_API}/assistant/transcribe`, { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": mimeType }, body: audio });
      if (response.status === 401) {
        token = await sessionToken(true);
        response = await fetch(`${VOICE_API}/assistant/transcribe`, { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": mimeType }, body: audio });
      }
      if (!response.ok) {
        const problem = await response.json().catch(() => ({})) as { error?: { message?: string }; detail?: string | { message?: string } };
        const detail = problem.error?.message ?? (typeof problem.detail === "string" ? problem.detail : problem.detail?.message);
        throw new Error(detail || "Zipity could not transcribe that audio. Try again.");
      }
      const result = (await response.json()) as { text: string };
      window.dispatchEvent(new CustomEvent("fastlearner:voice-query", { detail: { text: result.text } }));
      companion.confirmAndBeginSpeech(); companion.dismiss();
    } catch (reason) { setVoiceError(reason instanceof Error ? reason.message : "Microphone unavailable"); }
    finally {
      if (monitor !== undefined) window.clearInterval(monitor);
      if (recorder?.state === "recording") recorder.stop();
      stream?.getTracks().forEach(track => track.stop());
      if (context && context.state !== "closed") await context.close().catch(() => undefined);
      await companion.resume(); captureActive.current = false; setListening(false);
    }
  }

  useEffect(() => {
    // Hide local session bootstrap behind normal app startup, not first speech.
    void sessionToken().catch(() => undefined);
  }, []);

  useEffect(() => {
    if (visible && companion.state.wakeSource === "native" && !autoStarted.current) {
      autoStarted.current = true; void listenForCommand();
    }
    if (!visible) autoStarted.current = false;
  }, [visible, companion.state.wakeSource]);

  useEffect(() => {
    let timer = 0; let disposed = false; let unlisten: (() => void) | undefined;
    void listen("wake://clap", () => {
      setClapSeen(true); window.clearTimeout(timer);
      timer = window.setTimeout(() => setClapSeen(false), 950);
    }).then(cleanup => { if (disposed) cleanup(); else unlisten = cleanup; }).catch(() => undefined);
    return () => { disposed = true; window.clearTimeout(timer); unlisten?.(); };
  }, []);

  useEffect(() => {
    let disposed = false; let unlisten: (() => void) | undefined;
    void listen<unknown>("wake://error", event => {
      const payload = event.payload;
      const message = typeof payload === "string" ? payload : "Microphone listener stopped.";
      setVoiceError(`Double-clap listener: ${message}`);
    }).then(cleanup => { if (disposed) cleanup(); else unlisten = cleanup; }).catch(() => undefined);
    return () => { disposed = true; unlisten?.(); };
  }, []);

  function openAssistant() {
    companion.confirmAndBeginSpeech();
    window.dispatchEvent(new Event("fastlearner:open-assistant"));
    companion.dismiss();
  }

  const wakeIssue = voiceError || companion.state.error;
  return <>
    <button className={`jarvis-wake-button ${clapSeen ? "clap-seen" : ""} ${wakeIssue ? "has-warning" : ""}`} onClick={companion.wake} aria-label="Wake Zipity" title={wakeIssue || "Wake Zipity · Ctrl+Shift+Space"}><img src="/zipity-mark.png" alt="" /></button>
    {wakeIssue && !visible && <button className="wake-warning" onClick={companion.wake}><strong>Zipity voice needs attention</strong><span>{wakeIssue}</span></button>}
    {visible && <div className="jarvis-overlay" role="dialog" aria-modal="true" aria-labelledby="jarvis-title" aria-describedby="jarvis-description">
      <div className="jarvis-core" aria-hidden="true"><i /><i /><i /><img src="/zipity-mark.png" alt="" /></div>
      <span className="jarvis-kicker">ZIPITY IS AWAKE</span>
      <h2 id="jarvis-title">Hey, I’m Zipity.</h2>
      <p id="jarvis-description" aria-live="polite">{listening ? "Listening… speak your question." : companion.state.wakeSource === "native" ? "Double clap heard." : "Ready when you are."} What should we work on?</p>
      <div><button className="jarvis-dismiss" onClick={companion.dismiss}>Not now</button><button ref={companion.primaryControlRef} className="jarvis-open" autoFocus onClick={() => void listenForCommand()} disabled={listening}>{listening ? "Listening…" : "Speak to Zipity"}</button><button className="jarvis-open" onClick={openAssistant}>Type instead</button></div>
      {voiceError && <small role="alert">{voiceError}</small>}
      {companion.state.recoverableMessage && <small>{companion.state.recoverableMessage}</small>}
    </div>}
  </>;
}

export function App() {
  return <><AppShell title="Zipity" /><CompanionLayer /></>;
}

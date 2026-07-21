//! The sole desktop module that imports Tauri APIs.
//!
//! Every other React module (features, hooks, components) consumes native
//! capabilities through the `NativeBridge` interface exported here. Keeping the
//! Tauri import surface in one place preserves the platform-neutral boundary the
//! Rust shell already enforces (see `src-tauri/src/os`) and lets the rest of the
//! desktop client run in a plain browser or test runner via the unavailable
//! bridge fallback.

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

// ---------------------------------------------------------------------------
// Domain types (camelCase) mirrored from the Rust command/event contracts.
// ---------------------------------------------------------------------------

/** Platform the running desktop shell targets. */
export type Platform = "mac_os" | "windows" | "linux";

/** Operating-system microphone permission state. */
export type PermissionState = "granted" | "denied" | "prompt" | "unavailable";

/** Discriminated failure kinds surfaced by native adapters (`OsError`). */
export type BridgeErrorKind =
  | "unsupported"
  | "permission_denied"
  | "unavailable"
  | "backend"
  | "invalid_input";

/** Wake detection settings surfaced to and from the shell. */
export interface WakeConfig {
  readonly frameMs: number;
  readonly sensitivity: number;
  readonly minGapMs: number;
  readonly maxGapMs: number;
  readonly cooldownMs: number;
  readonly microphoneDevice: string | null;
  readonly keyboardOnly: boolean;
}

/** Snapshot of the native lifecycle state. */
export interface LifecycleStatus {
  readonly wakePaused: boolean;
  readonly listening: boolean;
  readonly mainWindowVisible: boolean;
  readonly loginAtStartup: boolean;
  readonly shuttingDown: boolean;
  readonly microphonePermission: PermissionState;
  readonly audioRunning: boolean;
}

/** Centered overlay placement on the active display. */
export interface OverlayPlacement {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

/** Platform capability report. */
export interface Capabilities {
  readonly platform: Platform;
  readonly trayAvailable: boolean;
  readonly globalShortcutAvailable: boolean;
  readonly microphonePermission: PermissionState;
  readonly secureStoreAvailable: boolean;
  readonly notificationsAvailable: boolean;
  readonly loginItemAvailable: boolean;
  readonly loginItemMechanism: string;
  readonly audioInputDevices: readonly string[];
  readonly defaultAudioInput: string | null;
  readonly wakeDefaultChord: string;
  readonly recoverableErrors: readonly string[];
}

/** Local wake event. Wake never starts speech capture or an AI request. */
export interface WakeDetected {
  readonly atMs: number | null;
}

/** Background synchronization status surfaced by `sync://state`. */
export type SyncStatus = "idle" | "syncing" | "synced" | "offline" | "error";

export interface SyncState {
  readonly status: SyncStatus;
  readonly pending: number;
  readonly detail: string | null;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** A normalized, safe error raised by the native bridge. */
export class BridgeError extends Error {
  readonly kind: BridgeErrorKind;

  constructor(kind: BridgeErrorKind, message: string) {
    super(message);
    this.name = "BridgeError";
    this.kind = kind;
  }
}

/** Raised when a native-only capability is used outside the Tauri runtime. */
export class BridgeUnavailableError extends BridgeError {
  constructor(capability: string) {
    super("unsupported", `native capability "${capability}" is unavailable outside the desktop shell`);
    this.name = "BridgeUnavailableError";
  }
}

/** Normalizes an unknown rejection value from `invoke` into a `BridgeError`. */
export function normalizeBridgeError(value: unknown): BridgeError {
  if (value instanceof BridgeError) return value;
  if (isRecord(value) && typeof value.kind === "string" && typeof value.message === "string") {
    return new BridgeError(value.kind as BridgeErrorKind, value.message);
  }
  if (value instanceof Error) return new BridgeError("backend", value.message);
  if (typeof value === "string") return new BridgeError("backend", value);
  return new BridgeError("backend", "unknown native error");
}

// ---------------------------------------------------------------------------
// Event names (kept identical to the Rust shell contract).
// ---------------------------------------------------------------------------

export const WAKE_DETECTED_EVENT = "wake://detected";
export const WAKE_STATE_EVENT = "wake://state";
export const PERMISSION_CHANGED_EVENT = "permission://changed";
export const SYNC_STATE_EVENT = "sync://state";

// ---------------------------------------------------------------------------
// Bridge interface consumed by the rest of the desktop client.
// ---------------------------------------------------------------------------

/** Removes a previously registered native event listener. */
export type Unsubscribe = () => void;

/**
 * Native capability surface. The Tauri implementation and the unavailable
 * fallback both satisfy this contract, so features depend on the interface
 * rather than on Tauri directly.
 */
export interface NativeBridge {
  readonly isNative: boolean;
  getCapabilities(): Promise<Capabilities>;
  getWakeConfig(): Promise<WakeConfig>;
  setWakeConfig(config: WakeConfig): Promise<WakeConfig>;
  pauseWake(): Promise<LifecycleStatus>;
  resumeWake(): Promise<LifecycleStatus>;
  openOverlay(): Promise<OverlayPlacement>;
  reportMicrophonePermission(state: PermissionState): Promise<LifecycleStatus>;
  setLoginAtStartup(enabled: boolean): Promise<LifecycleStatus>;
  secureSessionSet(key: string, secret: string): Promise<void>;
  secureSessionClear(key: string): Promise<void>;
  quit(): Promise<void>;
  onWakeDetected(handler: (event: WakeDetected) => void): Promise<Unsubscribe>;
  onWakeState(handler: (status: LifecycleStatus) => void): Promise<Unsubscribe>;
  onPermissionChanged(handler: (state: PermissionState) => void): Promise<Unsubscribe>;
  onSyncState(handler: (state: SyncState) => void): Promise<Unsubscribe>;
}

// ---------------------------------------------------------------------------
// Runtime detection
// ---------------------------------------------------------------------------

/** Whether the client is executing inside the Tauri desktop runtime. */
export function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    ("__TAURI_INTERNALS__" in window || "__TAURI__" in window)
  );
}

// ---------------------------------------------------------------------------
// Wire <-> domain mapping (exported for unit testing).
// ---------------------------------------------------------------------------

interface WireWakeConfig {
  frame_ms: number;
  sensitivity: number;
  min_gap_ms: number;
  max_gap_ms: number;
  cooldown_ms: number;
  microphone_device: string | null;
  keyboard_only: boolean;
}

interface WireLifecycleStatus {
  wake_paused: boolean;
  listening: boolean;
  main_window_visible: boolean;
  login_at_startup: boolean;
  shutting_down: boolean;
  microphone_permission: PermissionState;
  audio_running: boolean;
}

interface WireCapabilities {
  platform: Platform;
  tray_available: boolean;
  global_shortcut_available: boolean;
  microphone_permission: PermissionState;
  secure_store_available: boolean;
  notifications_available: boolean;
  login_item_available: boolean;
  login_item_mechanism: string;
  audio_input_devices: string[];
  default_audio_input: string | null;
  wake_default_chord: string;
  recoverable_errors: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function toWireWakeConfig(config: WakeConfig): WireWakeConfig {
  return {
    frame_ms: config.frameMs,
    sensitivity: config.sensitivity,
    min_gap_ms: config.minGapMs,
    max_gap_ms: config.maxGapMs,
    cooldown_ms: config.cooldownMs,
    microphone_device: config.microphoneDevice,
    keyboard_only: config.keyboardOnly,
  };
}

export function fromWireWakeConfig(wire: WireWakeConfig): WakeConfig {
  return {
    frameMs: wire.frame_ms,
    sensitivity: wire.sensitivity,
    minGapMs: wire.min_gap_ms,
    maxGapMs: wire.max_gap_ms,
    cooldownMs: wire.cooldown_ms,
    microphoneDevice: wire.microphone_device ?? null,
    keyboardOnly: wire.keyboard_only,
  };
}

export function fromWireLifecycleStatus(wire: WireLifecycleStatus): LifecycleStatus {
  return {
    wakePaused: wire.wake_paused,
    listening: wire.listening,
    mainWindowVisible: wire.main_window_visible,
    loginAtStartup: wire.login_at_startup,
    shuttingDown: wire.shutting_down,
    microphonePermission: wire.microphone_permission,
    audioRunning: wire.audio_running,
  };
}

export function fromWireCapabilities(wire: WireCapabilities): Capabilities {
  return {
    platform: wire.platform,
    trayAvailable: wire.tray_available,
    globalShortcutAvailable: wire.global_shortcut_available,
    microphonePermission: wire.microphone_permission,
    secureStoreAvailable: wire.secure_store_available,
    notificationsAvailable: wire.notifications_available,
    loginItemAvailable: wire.login_item_available,
    loginItemMechanism: wire.login_item_mechanism,
    audioInputDevices: wire.audio_input_devices ?? [],
    defaultAudioInput: wire.default_audio_input ?? null,
    wakeDefaultChord: wire.wake_default_chord,
    recoverableErrors: wire.recoverable_errors ?? [],
  };
}

/** Defensively maps a `wake://detected` payload. */
export function parseWakeDetected(payload: unknown): WakeDetected {
  if (isRecord(payload) && typeof payload.at_ms === "number") {
    return { atMs: payload.at_ms };
  }
  return { atMs: null };
}

/** Defensively maps a `permission://changed` payload (string or object). */
export function parsePermissionState(payload: unknown): PermissionState {
  const value = isRecord(payload) ? payload.state : payload;
  if (value === "granted" || value === "denied" || value === "prompt" || value === "unavailable") {
    return value;
  }
  return "unavailable";
}

/** Defensively maps a `sync://state` payload. */
export function parseSyncState(payload: unknown): SyncState {
  const status = isRecord(payload) ? payload.status : payload;
  const normalized: SyncStatus =
    status === "syncing" || status === "synced" || status === "offline" || status === "error"
      ? status
      : "idle";
  const pending = isRecord(payload) && typeof payload.pending === "number" ? payload.pending : 0;
  const detail = isRecord(payload) && typeof payload.detail === "string" ? payload.detail : null;
  return { status: normalized, pending, detail };
}

// ---------------------------------------------------------------------------
// Tauri implementation
// ---------------------------------------------------------------------------

async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  try {
    return await invoke<T>(command, args);
  } catch (error) {
    throw normalizeBridgeError(error);
  }
}

function toUnsubscribe(pending: Promise<UnlistenFn>): Unsubscribe {
  let unlisten: UnlistenFn | null = null;
  let cancelled = false;
  void pending.then((fn) => {
    if (cancelled) {
      fn();
    } else {
      unlisten = fn;
    }
  });
  return () => {
    cancelled = true;
    if (unlisten) unlisten();
  };
}

/** Creates the Tauri-backed native bridge. Only valid inside the desktop shell. */
export function createTauriBridge(): NativeBridge {
  return {
    isNative: true,
    async getCapabilities() {
      return fromWireCapabilities(await call<WireCapabilities>("get_capabilities"));
    },
    async getWakeConfig() {
      return fromWireWakeConfig(await call<WireWakeConfig>("get_wake_config"));
    },
    async setWakeConfig(config) {
      return fromWireWakeConfig(
        await call<WireWakeConfig>("set_wake_config", { config: toWireWakeConfig(config) }),
      );
    },
    async pauseWake() {
      return fromWireLifecycleStatus(await call<WireLifecycleStatus>("pause_wake"));
    },
    async resumeWake() {
      return fromWireLifecycleStatus(await call<WireLifecycleStatus>("resume_wake"));
    },
    async openOverlay() {
      return call<OverlayPlacement>("open_overlay");
    },
    async reportMicrophonePermission(state) {
      return fromWireLifecycleStatus(
        await call<WireLifecycleStatus>("report_microphone_permission", { state }),
      );
    },
    async setLoginAtStartup(enabled) {
      return fromWireLifecycleStatus(
        await call<WireLifecycleStatus>("set_login_at_startup", { enabled }),
      );
    },
    async secureSessionSet(key, secret) {
      await call<void>("secure_session_set", { key, secret });
    },
    async secureSessionClear(key) {
      await call<void>("secure_session_clear", { key });
    },
    async quit() {
      await call<void>("quit");
    },
    onWakeDetected(handler) {
      return Promise.resolve(
        toUnsubscribe(
          listen(WAKE_DETECTED_EVENT, (event) => handler(parseWakeDetected(event.payload))),
        ),
      );
    },
    onWakeState(handler) {
      return Promise.resolve(
        toUnsubscribe(
          listen(WAKE_STATE_EVENT, (event) =>
            handler(fromWireLifecycleStatus(event.payload as WireLifecycleStatus)),
          ),
        ),
      );
    },
    onPermissionChanged(handler) {
      return Promise.resolve(
        toUnsubscribe(
          listen(PERMISSION_CHANGED_EVENT, (event) => handler(parsePermissionState(event.payload))),
        ),
      );
    },
    onSyncState(handler) {
      return Promise.resolve(
        toUnsubscribe(listen(SYNC_STATE_EVENT, (event) => handler(parseSyncState(event.payload)))),
      );
    },
  };
}

// ---------------------------------------------------------------------------
// Unavailable fallback (keyboard/text-only environments: web, tests, no shell)
// ---------------------------------------------------------------------------

const UNAVAILABLE_CAPABILITIES: Capabilities = {
  platform: "linux",
  trayAvailable: false,
  globalShortcutAvailable: false,
  microphonePermission: "unavailable",
  secureStoreAvailable: false,
  notificationsAvailable: false,
  loginItemAvailable: false,
  loginItemMechanism: "unavailable",
  audioInputDevices: [],
  defaultAudioInput: null,
  wakeDefaultChord: "CmdOrCtrl+Shift+Space",
  recoverableErrors: ["running without the native desktop shell"],
};

/**
 * A bridge for non-native contexts. Native-only mutations reject with a typed
 * error while the keyboard/text flow (capabilities, event no-ops) stays usable,
 * satisfying the keyboard-only fallback requirements.
 */
export function createUnavailableBridge(): NativeBridge {
  const noop: Unsubscribe = () => {};
  const reject = (capability: string) => Promise.reject(new BridgeUnavailableError(capability));
  return {
    isNative: false,
    getCapabilities: () => Promise.resolve(UNAVAILABLE_CAPABILITIES),
    getWakeConfig: () => reject("get_wake_config"),
    setWakeConfig: () => reject("set_wake_config"),
    pauseWake: () => reject("pause_wake"),
    resumeWake: () => reject("resume_wake"),
    openOverlay: () => reject("open_overlay"),
    reportMicrophonePermission: () => reject("report_microphone_permission"),
    setLoginAtStartup: () => reject("set_login_at_startup"),
    secureSessionSet: () => reject("secure_session_set"),
    secureSessionClear: () => reject("secure_session_clear"),
    quit: () => reject("quit"),
    onWakeDetected: () => Promise.resolve(noop),
    onWakeState: () => Promise.resolve(noop),
    onPermissionChanged: () => Promise.resolve(noop),
    onSyncState: () => Promise.resolve(noop),
  };
}

let sharedBridge: NativeBridge | null = null;

/** Returns the process-wide native bridge, selecting the runtime implementation. */
export function getNativeBridge(): NativeBridge {
  if (!sharedBridge) {
    sharedBridge = isTauriRuntime() ? createTauriBridge() : createUnavailableBridge();
  }
  return sharedBridge;
}

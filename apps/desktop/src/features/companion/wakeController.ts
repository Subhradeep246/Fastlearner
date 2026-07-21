//! Companion wake controller.
//!
//! Bridges native wake, permission, synchronization, and overlay events to a
//! small, observable state machine that the React overlay renders. It enforces
//! the wake product rules that are independent of the presentation layer:
//!
//! - A wake trigger (native double-clap or keyboard) shows a visible wake
//!   confirmation and never starts speech capture or an AI request on its own
//!   (Requirements 4.6, 5.1).
//! - Speech capture only becomes available after confirmation when microphone
//!   permission is active and keyboard-only mode is off (Requirement 4.7).
//! - Denied/revoked permission or a lost device stops listening and leaves the
//!   keyboard/text flow available (Requirements 4.9, 4.11).
//! - Synchronization state drives an offline indicator while local functions
//!   stay usable (Requirements 5.7, 22.10, 22.12).
//! - The overlay opens centered on the active display (Requirement 5.2) and the
//!   process quits explicitly on request.
//!
//! The controller depends only on the `NativeBridge` interface, so it runs and
//! is tested without Tauri.

import {
  type LifecycleStatus,
  type NativeBridge,
  type OverlayPlacement,
  type PermissionState,
  type SyncState,
  type WakeConfig,
  type WakeDetected,
  normalizeBridgeError,
} from "../../platform/tauri";

/** Overlay presentation states (Requirement 5.1). */
export type OverlayState =
  | "idle"
  | "confirmation"
  | "listening"
  | "thinking"
  | "answer"
  | "offline"
  | "error";

/** Why speech capture could not begin, driving the keyboard/text fallback. */
export type SpeechUnavailableReason = "permission" | "keyboard_only" | "not_confirmed";

/** Result of attempting to begin speech capture after confirmation. */
export type BeginSpeechResult =
  | { readonly started: true }
  | { readonly started: false; readonly reason: SpeechUnavailableReason };

/** Immutable snapshot of the companion state. */
export interface CompanionState {
  readonly overlay: OverlayState;
  readonly wakeSource: "native" | "keyboard" | null;
  readonly awaitingConfirmation: boolean;
  readonly listening: boolean;
  readonly wakePaused: boolean;
  readonly keyboardOnly: boolean;
  readonly permission: PermissionState;
  readonly voiceAvailable: boolean;
  readonly placement: OverlayPlacement | null;
  readonly sync: SyncState;
  readonly recoverableMessage: string | null;
  readonly error: string | null;
}

const INITIAL_STATE: CompanionState = {
  overlay: "idle",
  wakeSource: null,
  awaitingConfirmation: false,
  listening: false,
  wakePaused: false,
  keyboardOnly: false,
  permission: "prompt",
  voiceAvailable: false,
  placement: null,
  sync: { status: "idle", pending: 0, detail: null },
  recoverableMessage: null,
  error: null,
};

type Listener = (state: CompanionState) => void;

function computeVoiceAvailable(permission: PermissionState, keyboardOnly: boolean): boolean {
  return permission === "granted" && !keyboardOnly;
}

/**
 * Observable companion wake controller. Construct with the active
 * `NativeBridge`, call `initialize()` once, and `dispose()` on teardown.
 */
export class CompanionWakeController {
  private state: CompanionState = INITIAL_STATE;
  private readonly listeners = new Set<Listener>();
  private readonly unsubscribers: Array<() => void> = [];
  private generation = 0;
  private initialization: { generation: number; promise: Promise<void> } | null = null;
  private initialized = false;
  private disposed = false;

  constructor(private readonly bridge: NativeBridge) {}

  /** Current immutable state. */
  getState(): CompanionState {
    return this.state;
  }

  /** Subscribes to state changes and returns an unsubscribe callback. */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Loads capabilities and wake configuration and wires native events. Safe to
   * call when running without the native shell: the keyboard/text flow is kept
   * available and errors are surfaced as recoverable rather than thrown.
   */
  initialize(): Promise<void> {
    if (!this.disposed && this.initialized) {
      return Promise.resolve();
    }
    if (!this.disposed && this.initialization) {
      return this.initialization.promise;
    }

    this.disposed = false;
    const generation = ++this.generation;
    const promise = this.initializeGeneration(generation).finally(() => {
      if (this.initialization?.generation !== generation) return;
      this.initialization = null;
      if (this.isGenerationActive(generation)) {
        this.initialized = true;
      }
    });
    this.initialization = { generation, promise };
    return promise;
  }

  private async initializeGeneration(generation: number): Promise<void> {
    try {
      const capabilities = await this.bridge.getCapabilities();
      if (!this.isGenerationActive(generation)) return;
      this.patch({ permission: capabilities.microphonePermission });
      if (capabilities.recoverableErrors.length > 0) {
        this.patch({ recoverableMessage: capabilities.recoverableErrors.join("; ") });
      }
    } catch (error) {
      if (!this.isGenerationActive(generation)) return;
      this.patch({ recoverableMessage: normalizeBridgeError(error).message });
    }

    try {
      const config = await this.bridge.getWakeConfig();
      if (!this.isGenerationActive(generation)) return;
      this.applyConfig(config);
    } catch {
      if (!this.isGenerationActive(generation)) return;
      // No native wake config (e.g. web/test). Keyboard/text flow remains.
    }

    if (!this.isGenerationActive(generation)) return;
    await Promise.all([
      this.registerListener(generation, () =>
        this.bridge.onWakeDetected(() => {
          if (this.isGenerationActive(generation)) {
            void this.wake("native", generation);
          }
        }),
      ),
      this.registerListener(generation, () =>
        this.bridge.onWakeState((status) => {
          if (this.isGenerationActive(generation)) this.applyLifecycleStatus(status);
        }),
      ),
      this.registerListener(generation, () =>
        this.bridge.onPermissionChanged((state) => {
          if (this.isGenerationActive(generation)) {
            void this.applyPermission(state, generation);
          }
        }),
      ),
      this.registerListener(generation, () =>
        this.bridge.onSyncState((state) => {
          if (this.isGenerationActive(generation)) this.applySyncState(state);
        }),
      ),
    ]);
  }

  private async registerListener(
    generation: number,
    register: () => Promise<() => void>,
  ): Promise<void> {
    try {
      const unsubscribe = await register();
      if (!this.isGenerationActive(generation)) {
        this.safeUnsubscribe(unsubscribe);
        return;
      }
      this.unsubscribers.push(unsubscribe);
    } catch (error) {
      if (this.isGenerationActive(generation)) {
        this.patch({ recoverableMessage: normalizeBridgeError(error).message });
      }
    }
  }

  /** Applies a wake configuration snapshot (keyboard-only, device selection). */
  applyConfig(config: WakeConfig): void {
    const keyboardOnly = config.keyboardOnly;
    this.patch({
      keyboardOnly,
      voiceAvailable: computeVoiceAvailable(this.state.permission, keyboardOnly),
    });
  }

  /**
   * Handles a native double-clap wake event. Shows the visible confirmation and
   * positions the overlay; it never starts speech capture or an AI request.
   */
  handleWakeDetected(event: WakeDetected): void {
    // The payload only carries an optional timestamp; wake never starts capture
    // or an AI request, so the event simply drives the visible confirmation.
    void event;
    void this.wake("native");
  }

  /**
   * Triggers the wake flow from a native event or a keyboard shortcut. Opens the
   * centered overlay and enters the confirmation state.
   */
  async wake(source: "native" | "keyboard", generation?: number): Promise<void> {
    if (generation !== undefined && !this.isGenerationActive(generation)) return;
    if (this.state.wakePaused && source === "native") {
      return;
    }
    this.patch({
      overlay: "confirmation",
      wakeSource: source,
      awaitingConfirmation: true,
      listening: false,
      error: null,
    });
    await this.positionOverlay(generation);
  }

  /** Requests centered overlay placement from the shell (Requirement 5.2). */
  async positionOverlay(generation?: number): Promise<OverlayPlacement | null> {
    try {
      const placement = await this.bridge.openOverlay();
      if (generation !== undefined && !this.isGenerationActive(generation)) return null;
      this.patch({ placement });
      return placement;
    } catch {
      return null;
    }
  }

  /**
   * Confirms the wake and begins speech capture when permitted. When voice is
   * unavailable (denied permission or keyboard-only), the overlay stays open for
   * text entry and the reason is reported for the keyboard/text fallback.
   */
  confirmAndBeginSpeech(): BeginSpeechResult {
    if (!this.state.awaitingConfirmation) {
      return { started: false, reason: "not_confirmed" };
    }
    if (this.state.keyboardOnly) {
      this.enterTextEntry();
      return { started: false, reason: "keyboard_only" };
    }
    if (this.state.permission !== "granted") {
      this.enterTextEntry();
      return { started: false, reason: "permission" };
    }
    this.patch({
      overlay: "listening",
      awaitingConfirmation: false,
      listening: true,
    });
    return { started: true };
  }

  /** Keeps the overlay open on the idle text-entry surface (keyboard fallback). */
  private enterTextEntry(): void {
    this.patch({
      overlay: "idle",
      awaitingConfirmation: false,
      listening: false,
    });
  }

  /** Dismisses the overlay back to idle. */
  dismiss(): void {
    this.patch({
      overlay: "idle",
      wakeSource: null,
      awaitingConfirmation: false,
      listening: false,
    });
  }

  /** Pauses wake listening, stopping capture before the paused state is shown. */
  async pause(): Promise<void> {
    try {
      const status = await this.bridge.pauseWake();
      this.applyLifecycleStatus(status);
    } catch (error) {
      this.patch({ error: normalizeBridgeError(error).message });
    }
  }

  /** Resumes wake listening when permitted. */
  async resume(): Promise<void> {
    try {
      const status = await this.bridge.resumeWake();
      this.applyLifecycleStatus(status);
    } catch (error) {
      this.patch({ error: normalizeBridgeError(error).message });
    }
  }

  /** Persists updated wake settings and reflects the returned configuration. */
  async updateSettings(config: WakeConfig): Promise<void> {
    try {
      const applied = await this.bridge.setWakeConfig(config);
      this.applyConfig(applied);
    } catch (error) {
      this.patch({ error: normalizeBridgeError(error).message });
    }
  }

  /**
   * Applies an operating-system permission change. When access is lost the
   * native stream is stopped and the keyboard/text flow remains (Req 4.11, 4.9).
   */
  async applyPermission(state: PermissionState, generation?: number): Promise<void> {
    if (generation !== undefined && !this.isGenerationActive(generation)) return;
    const voiceAvailable = computeVoiceAvailable(state, this.state.keyboardOnly);
    this.patch({ permission: state, voiceAvailable });
    if (state !== "granted") {
      // Ensure any active capture stops and downgrade the listening surface.
      if (this.state.listening) {
        this.patch({ overlay: "idle", listening: false });
      }
      try {
        const status = await this.bridge.reportMicrophonePermission(state);
        if (generation !== undefined && !this.isGenerationActive(generation)) return;
        this.applyLifecycleStatus(status);
      } catch {
        // Reporting failure must not break the keyboard/text flow.
      }
    }
  }

  /** Handles a microphone device becoming unavailable (Requirement 4.11). */
  async handleDeviceLost(): Promise<void> {
    await this.applyPermission("unavailable");
  }

  /** Reflects a native lifecycle status snapshot. */
  applyLifecycleStatus(status: LifecycleStatus): void {
    const overlayEndsListening = !status.listening && this.state.overlay === "listening";
    this.patch({
      wakePaused: status.wakePaused,
      listening: status.listening,
      permission: status.microphonePermission,
      voiceAvailable: computeVoiceAvailable(status.microphonePermission, this.state.keyboardOnly),
      ...(overlayEndsListening ? { overlay: "idle" as const } : {}),
    });
  }

  /** Updates the synchronization indicator (Requirements 5.7, 22.10, 22.12). */
  applySyncState(sync: SyncState): void {
    this.patch({ sync });
  }

  /** Performs explicit quit cleanup and terminates the desktop process. */
  async quit(): Promise<void> {
    try {
      await this.bridge.quit();
    } catch (error) {
      this.patch({ error: normalizeBridgeError(error).message });
    }
  }

  /** Removes all native listeners and subscribers. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.initialized = false;
    this.initialization = null;
    ++this.generation;
    for (const unsubscribe of this.unsubscribers) this.safeUnsubscribe(unsubscribe);
    this.unsubscribers.length = 0;
    this.listeners.clear();
  }

  private isGenerationActive(generation: number): boolean {
    return !this.disposed && this.generation === generation;
  }

  private safeUnsubscribe(unsubscribe: () => void): void {
    try {
      unsubscribe();
    } catch {
      // One faulty native cleanup must not prevent the remaining listeners from
      // being detached.
    }
  }

  private patch(patch: Partial<CompanionState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }
}

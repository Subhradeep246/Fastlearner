import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Capabilities,
  LifecycleStatus,
  NativeBridge,
  OverlayPlacement,
  PermissionState,
  SyncState,
  WakeConfig,
  WakeDetected,
} from "../../platform/tauri";
import { CompanionWakeController } from "./wakeController";

const PLACEMENT: OverlayPlacement = { x: 100, y: 80, width: 460, height: 260 };

function baseCapabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  return {
    platform: "mac_os",
    trayAvailable: true,
    globalShortcutAvailable: true,
    microphonePermission: "granted",
    secureStoreAvailable: true,
    notificationsAvailable: true,
    loginItemAvailable: true,
    loginItemMechanism: "test",
    audioInputDevices: ["Built-in"],
    defaultAudioInput: "Built-in",
    wakeDefaultChord: "CmdOrCtrl+Shift+Space",
    recoverableErrors: [],
    ...overrides,
  };
}

function baseConfig(overrides: Partial<WakeConfig> = {}): WakeConfig {
  return {
    frameMs: 20,
    sensitivity: 0.5,
    minGapMs: 120,
    maxGapMs: 900,
    cooldownMs: 2000,
    microphoneDevice: null,
    keyboardOnly: false,
    ...overrides,
  };
}

function status(overrides: Partial<LifecycleStatus> = {}): LifecycleStatus {
  return {
    wakePaused: false,
    listening: false,
    mainWindowVisible: true,
    loginAtStartup: false,
    shuttingDown: false,
    microphonePermission: "granted",
    audioRunning: false,
    ...overrides,
  };
}

class FakeBridge implements NativeBridge {
  readonly isNative = true;
  capabilities = baseCapabilities();
  config = baseConfig();
  reportedPermissions: PermissionState[] = [];
  quitCalls = 0;
  wakeDetectedHandlers: Array<(event: WakeDetected) => void> = [];
  permissionHandlers: Array<(state: PermissionState) => void> = [];
  syncHandlers: Array<(state: SyncState) => void> = [];

  getCapabilities = vi.fn(() => Promise.resolve(this.capabilities));
  getWakeConfig = vi.fn(() => Promise.resolve(this.config));
  setWakeConfig = vi.fn((config: WakeConfig) => {
    this.config = config;
    return Promise.resolve(config);
  });
  pauseWake = vi.fn(() => Promise.resolve(status({ wakePaused: true, listening: false })));
  resumeWake = vi.fn(() => Promise.resolve(status({ listening: true, audioRunning: true })));
  openOverlay = vi.fn(() => Promise.resolve(PLACEMENT));
  reportMicrophonePermission = vi.fn((state: PermissionState) => {
    this.reportedPermissions.push(state);
    return Promise.resolve(status({ microphonePermission: state, listening: false }));
  });
  setLoginAtStartup = vi.fn(() => Promise.resolve(status()));
  secureSessionSet = vi.fn(() => Promise.resolve());
  secureSessionClear = vi.fn(() => Promise.resolve());
  quit = vi.fn(() => {
    this.quitCalls += 1;
    return Promise.resolve();
  });
  onWakeDetected = vi.fn((handler: (event: WakeDetected) => void) => {
    this.wakeDetectedHandlers.push(handler);
    return Promise.resolve(() => {});
  });
  onWakeState = vi.fn(() => Promise.resolve(() => {}));
  onPermissionChanged = vi.fn((handler: (state: PermissionState) => void) => {
    this.permissionHandlers.push(handler);
    return Promise.resolve(() => {});
  });
  onSyncState = vi.fn((handler: (state: SyncState) => void) => {
    this.syncHandlers.push(handler);
    return Promise.resolve(() => {});
  });
}

describe("CompanionWakeController", () => {
  let bridge: FakeBridge;
  let controller: CompanionWakeController;

  beforeEach(() => {
    bridge = new FakeBridge();
    controller = new CompanionWakeController(bridge);
  });

  it("shows visible confirmation and positions the overlay without starting capture", async () => {
    await controller.wake("keyboard");
    const state = controller.getState();
    expect(state.overlay).toBe("confirmation");
    expect(state.awaitingConfirmation).toBe(true);
    expect(state.listening).toBe(false);
    expect(state.placement).toEqual(PLACEMENT);
    expect(bridge.openOverlay).toHaveBeenCalledTimes(1);
  });

  it("begins speech capture only after confirmation when permission is granted", async () => {
    await controller.initialize();
    await controller.wake("native");
    const result = controller.confirmAndBeginSpeech();
    expect(result).toEqual({ started: true });
    expect(controller.getState().overlay).toBe("listening");
    expect(controller.getState().listening).toBe(true);
  });

  it("rejects speech capture without a prior confirmation", () => {
    const result = controller.confirmAndBeginSpeech();
    expect(result).toEqual({ started: false, reason: "not_confirmed" });
  });

  it("falls back to keyboard/text entry in keyboard-only mode", async () => {
    bridge.config = baseConfig({ keyboardOnly: true });
    await controller.initialize();
    await controller.wake("keyboard");
    const result = controller.confirmAndBeginSpeech();
    expect(result).toEqual({ started: false, reason: "keyboard_only" });
    expect(controller.getState().overlay).toBe("idle");
    expect(controller.getState().listening).toBe(false);
  });

  it("falls back to keyboard/text entry when permission is not granted", async () => {
    bridge.capabilities = baseCapabilities({ microphonePermission: "denied" });
    await controller.initialize();
    await controller.wake("keyboard");
    const result = controller.confirmAndBeginSpeech();
    expect(result).toEqual({ started: false, reason: "permission" });
    expect(controller.getState().voiceAvailable).toBe(false);
  });

  it("stops listening and reports the state when permission is revoked", async () => {
    await controller.initialize();
    await controller.wake("native");
    controller.confirmAndBeginSpeech();
    expect(controller.getState().listening).toBe(true);

    await controller.applyPermission("denied");
    expect(controller.getState().listening).toBe(false);
    expect(controller.getState().overlay).toBe("idle");
    expect(bridge.reportedPermissions).toContain("denied");
  });

  it("stops capture when the microphone device is lost", async () => {
    await controller.initialize();
    await controller.wake("native");
    controller.confirmAndBeginSpeech();

    await controller.handleDeviceLost();
    expect(controller.getState().permission).toBe("unavailable");
    expect(controller.getState().listening).toBe(false);
    expect(bridge.reportedPermissions).toContain("unavailable");
  });

  it("ignores native wake events while paused", async () => {
    await controller.pause();
    expect(controller.getState().wakePaused).toBe(true);
    await controller.wake("native");
    expect(controller.getState().overlay).toBe("idle");
  });

  it("updates the synchronization indicator from native events", async () => {
    await controller.initialize();
    bridge.syncHandlers.forEach((handler) =>
      handler({ status: "offline", pending: 2, detail: "no connection" }),
    );
    expect(controller.getState().sync).toEqual({
      status: "offline",
      pending: 2,
      detail: "no connection",
    });
  });

  it("routes native double-clap events through the confirmation flow", async () => {
    await controller.initialize();
    bridge.wakeDetectedHandlers.forEach((handler) => handler({ atMs: 1234 }));
    // Allow the async wake() microtask chain to settle.
    await Promise.resolve();
    await Promise.resolve();
    expect(controller.getState().overlay).toBe("confirmation");
    expect(controller.getState().wakeSource).toBe("native");
  });

  it("performs explicit quit through the bridge", async () => {
    await controller.quit();
    expect(bridge.quitCalls).toBe(1);
  });

  it("notifies subscribers immediately and on change", async () => {
    const seen: string[] = [];
    const unsubscribe = controller.subscribe((state) => seen.push(state.overlay));
    await controller.wake("keyboard");
    unsubscribe();
    await controller.dismiss();
    expect(seen[0]).toBe("idle");
    expect(seen).toContain("confirmation");
  });
});

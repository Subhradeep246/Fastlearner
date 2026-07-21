import { describe, expect, it } from "vitest";
import {
  BridgeError,
  BridgeUnavailableError,
  createUnavailableBridge,
  fromWireCapabilities,
  fromWireLifecycleStatus,
  fromWireWakeConfig,
  isTauriRuntime,
  normalizeBridgeError,
  parsePermissionState,
  parseSyncState,
  parseWakeDetected,
  toWireWakeConfig,
  type WakeConfig,
} from "./tauri";

const CONFIG: WakeConfig = {
  frameMs: 20,
  sensitivity: 0.6,
  minGapMs: 150,
  maxGapMs: 800,
  cooldownMs: 2000,
  microphoneDevice: "Built-in",
  keyboardOnly: false,
};

describe("wake config wire mapping", () => {
  it("round-trips through the snake_case wire format", () => {
    const wire = toWireWakeConfig(CONFIG);
    expect(wire).toEqual({
      frame_ms: 20,
      sensitivity: 0.6,
      min_gap_ms: 150,
      max_gap_ms: 800,
      cooldown_ms: 2000,
      microphone_device: "Built-in",
      keyboard_only: false,
    });
    expect(fromWireWakeConfig(wire)).toEqual(CONFIG);
  });
});

describe("lifecycle and capability mapping", () => {
  it("maps lifecycle status fields to camelCase", () => {
    const status = fromWireLifecycleStatus({
      wake_paused: true,
      listening: false,
      main_window_visible: true,
      login_at_startup: false,
      shutting_down: false,
      microphone_permission: "granted",
      audio_running: false,
    });
    expect(status.wakePaused).toBe(true);
    expect(status.microphonePermission).toBe("granted");
  });

  it("maps capabilities and defaults missing collections", () => {
    const capabilities = fromWireCapabilities({
      platform: "mac_os",
      tray_available: true,
      global_shortcut_available: true,
      microphone_permission: "prompt",
      secure_store_available: true,
      notifications_available: true,
      login_item_available: true,
      login_item_mechanism: "launch-agent",
      audio_input_devices: ["A", "B"],
      default_audio_input: "A",
      wake_default_chord: "CmdOrCtrl+Shift+Space",
      recoverable_errors: [],
    });
    expect(capabilities.audioInputDevices).toEqual(["A", "B"]);
    expect(capabilities.loginItemMechanism).toBe("launch-agent");
  });
});

describe("event payload parsing", () => {
  it("parses permission payloads as string or object", () => {
    expect(parsePermissionState("denied")).toBe("denied");
    expect(parsePermissionState({ state: "granted" })).toBe("granted");
    expect(parsePermissionState({ nonsense: true })).toBe("unavailable");
  });

  it("parses sync payloads defensively", () => {
    expect(parseSyncState({ status: "offline", pending: 3, detail: "x" })).toEqual({
      status: "offline",
      pending: 3,
      detail: "x",
    });
    expect(parseSyncState("bogus")).toEqual({ status: "idle", pending: 0, detail: null });
  });

  it("parses wake-detected payloads", () => {
    expect(parseWakeDetected({ at_ms: 42 })).toEqual({ atMs: 42 });
    expect(parseWakeDetected(null)).toEqual({ atMs: null });
  });
});

describe("error normalization", () => {
  it("maps the tagged OsError shape", () => {
    const error = normalizeBridgeError({ kind: "permission_denied", message: "mic" });
    expect(error).toBeInstanceOf(BridgeError);
    expect(error.kind).toBe("permission_denied");
    expect(error.message).toBe("mic");
  });

  it("falls back to a backend error for unknown values", () => {
    expect(normalizeBridgeError(123).kind).toBe("backend");
  });
});

describe("runtime selection", () => {
  it("reports no native runtime under the test environment", () => {
    expect(isTauriRuntime()).toBe(false);
  });

  it("keeps capabilities available but rejects native-only mutations in the fallback", async () => {
    const bridge = createUnavailableBridge();
    expect(bridge.isNative).toBe(false);
    const capabilities = await bridge.getCapabilities();
    expect(capabilities.microphonePermission).toBe("unavailable");
    await expect(bridge.pauseWake()).rejects.toBeInstanceOf(BridgeUnavailableError);
  });
});

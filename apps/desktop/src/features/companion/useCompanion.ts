//! React binding for the companion wake controller.
//!
//! Wires native wake, permission, synchronization, and overlay events to React
//! state, moves keyboard focus to the primary control when the overlay opens
//! (Requirement 5.2), and provides an in-app keyboard wake fallback so the flow
//! works without a microphone (Requirements 4.9, 5.9).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getNativeBridge, type NativeBridge } from "../../platform/tauri";
import {
  type BeginSpeechResult,
  type CompanionState,
  CompanionWakeController,
} from "./wakeController";

export interface UseCompanionResult {
  readonly state: CompanionState;
  /** Ref to attach to the overlay's primary keyboard control for focus. */
  readonly primaryControlRef: React.RefObject<HTMLElement | null>;
  /** Triggers the wake flow from a keyboard shortcut or button. */
  wake: () => void;
  /** Confirms the visible wake and begins speech capture when permitted. */
  confirmAndBeginSpeech: () => BeginSpeechResult;
  dismiss: () => void;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  quit: () => Promise<void>;
}

/**
 * Binds a {@link CompanionWakeController} to React. A bridge may be injected for
 * tests; production defaults to the process-wide native bridge.
 */
export function useCompanion(bridge?: NativeBridge): UseCompanionResult {
  const controller = useMemo(
    () => new CompanionWakeController(bridge ?? getNativeBridge()),
    [bridge],
  );
  const [state, setState] = useState<CompanionState>(() => controller.getState());
  const primaryControlRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const unsubscribe = controller.subscribe(setState);
    void controller.initialize();
    return () => {
      unsubscribe();
      controller.dispose();
    };
  }, [controller]);

  // Move focus to the primary control whenever the overlay opens (Req 5.2).
  useEffect(() => {
    if (state.overlay !== "idle" && primaryControlRef.current) {
      primaryControlRef.current.focus();
    }
  }, [state.overlay, state.placement]);

  // In-app keyboard wake fallback: Cmd/Ctrl + Shift + Space.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.code === "Space") {
        event.preventDefault();
        void controller.wake("keyboard");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [controller]);

  const wake = useCallback(() => {
    void controller.wake("keyboard");
  }, [controller]);
  const confirmAndBeginSpeech = useCallback(
    () => controller.confirmAndBeginSpeech(),
    [controller],
  );
  const dismiss = useCallback(() => controller.dismiss(), [controller]);
  const pause = useCallback(() => controller.pause(), [controller]);
  const resume = useCallback(() => controller.resume(), [controller]);
  const quit = useCallback(() => controller.quit(), [controller]);

  return { state, primaryControlRef, wake, confirmAndBeginSpeech, dismiss, pause, resume, quit };
}

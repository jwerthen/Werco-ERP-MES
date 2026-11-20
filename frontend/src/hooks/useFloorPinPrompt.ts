import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FloorPinPrompter, FloorPinRequest } from '../services/badgeSignOn';

/** What the PIN pad renders while a challenge is open. */
export interface FloorPinPromptState {
  /** The previous attempt's refusal, or null on the first ask. */
  error: string | null;
  /** A PIN was submitted and the server has not answered yet. */
  verifying: boolean;
  /** Increments on every ask, so the pad clears its digits for each new attempt. */
  attempt: number;
  /** The sign-on ended on `error` (a 429): the pad shows it, takes no PIN, and only dismisses. */
  final?: boolean;
}

export interface UseFloorPinPrompt {
  /** Null while no challenge is open — render no pad. */
  prompt: FloorPinPromptState | null;
  /** Hand this to `signOnWithBadge`. Stable for the component's lifetime. */
  prompter: FloorPinPrompter;
  /** The pad's submit: answers the open challenge with the PIN. */
  submit: (pin: string) => void;
  /**
   * The pad's Cancel, and the hook for any flow reset (crew-station idle, station
   * lock, unmount): the pending sign-on throws `BadgeSignOnCancelledError`, and a
   * challenge re-asked by an attempt still in flight is answered "cancelled" too,
   * so a reset can never be undone by a late 401 re-opening the pad.
   */
  cancel: () => void;
}

/**
 * React side of the floor-PIN challenge (see services/badgeSignOn.ts).
 *
 * `prompter.requestPin` opens the pad and returns a promise the pad settles:
 * `submit(pin)` resolves it with the PIN (the pad stays up, "verifying", until the
 * helper either re-asks with an error or calls `close`), `cancel()` resolves it with
 * null. One pending challenge at a time — a newer ask settles an older one as
 * cancelled rather than leaving it hanging.
 */
export function useFloorPinPrompt(): UseFloorPinPrompt {
  const [prompt, setPrompt] = useState<FloorPinPromptState | null>(null);
  const resolverRef = useRef<((pin: string | null) => void) | null>(null);
  // True from the first ask until the helper closes the sign-on.
  const openRef = useRef(false);
  // Set by cancel(); cleared when the helper closes the sign-on it belonged to.
  const cancelledRef = useRef(false);
  const attemptRef = useRef(0);
  // The open ask is a final one (submit is meaningless; only a dismissal answers it).
  const finalRef = useRef(false);
  const mountedRef = useRef(true);

  const settle = useCallback((value: string | null) => {
    const resolve = resolverRef.current;
    resolverRef.current = null;
    resolve?.(value);
  }, []);

  const requestPin = useCallback(
    (request: FloorPinRequest) =>
      new Promise<string | null>(resolve => {
        if (cancelledRef.current || !mountedRef.current) {
          resolve(null);
          return;
        }
        settle(null);
        resolverRef.current = resolve;
        openRef.current = true;
        attemptRef.current += 1;
        finalRef.current = request.final === true;
        setPrompt({
          error: request.error,
          verifying: false,
          attempt: attemptRef.current,
          final: request.final === true,
        });
      }),
    [settle]
  );

  const close = useCallback(() => {
    openRef.current = false;
    finalRef.current = false;
    cancelledRef.current = false;
    settle(null);
    if (mountedRef.current) setPrompt(null);
  }, [settle]);

  const submit = useCallback(
    (pin: string) => {
      if (!resolverRef.current || finalRef.current) return;
      setPrompt(current => (current ? { ...current, verifying: true, error: null } : current));
      settle(pin);
    },
    [settle]
  );

  const cancel = useCallback(() => {
    // Only a sign-on that is actually open can be cancelled; with no pad up there
    // is nothing to suppress, and a stale flag would swallow the NEXT challenge.
    if (openRef.current) cancelledRef.current = true;
    settle(null);
    setPrompt(null);
  }, [settle]);

  useEffect(() => {
    mountedRef.current = true;
    cancelledRef.current = false;
    return () => {
      mountedRef.current = false;
      cancelledRef.current = true;
      settle(null);
    };
  }, [settle]);

  const prompter = useMemo<FloorPinPrompter>(() => ({ requestPin, close }), [requestPin, close]);

  return { prompt, prompter, submit, cancel };
}

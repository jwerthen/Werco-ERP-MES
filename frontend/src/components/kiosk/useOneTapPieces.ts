import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * The one-tap `+1 PIECE` state machine — tap once per finished part, and the
 * piece records itself.
 *
 * WHY THIS IS A HOOK AND NOT COMPONENT STATE. The pending delta is production
 * the operator has already committed to; the only thing standing between the tap
 * and the ledger is a short grace period. So it must NOT live in the screen that
 * renders it: a crew-station quantity screen is unmounted by Cancel, by the
 * 90-second idle flow-reset, and by the ghost-guard that pulls a view whose
 * operation left the queue — and a delta sitting in a `setTimeout` inside an
 * unmounting subtree is silently lost production. Callers own this hook at PAGE
 * level and hand the lane its props, so every one of those teardowns is a
 * `flush()`, not a loss.
 *
 * THE THREE QUANTITIES, and why they are three:
 *  - `pending`  — tapped, not yet sent. UNDOABLE. This is the grace period.
 *  - `inFlight` — handed to the server, answer not back yet. NOT undoable: the
 *    row may already exist. Taps that arrive mid-flight buffer into `pending`
 *    behind it rather than being dropped or folded into a request already sent.
 *  - `lastRecorded` — the server said yes. FINAL. The kiosk has no undo for a
 *    posted report (over-count correction is its own screen, its own badge
 *    signature, its own reason), so nothing in this state may imply otherwise.
 * Collapsing any two of them either loses a tap or offers an undo that cannot
 * be honoured.
 *
 * COALESCING. Each tap re-arms the window, so a run of parts coming off a
 * machine lands as ONE report (`+3`) rather than three racing requests — which
 * is also what keeps the undo honest: whatever is still on screen is still
 * undoable. A steady tapper never pauses and so never posts on the timer, which
 * is fine and deliberate: the flush seams below (confirm, screen exit, idle,
 * page unload) all bank it, and the ceiling clamp stops the count running away.
 *
 * FAILURE. A refused post moves `inFlight` BACK into `pending` and stops — it
 * never auto-retries, because a retry loop against a server that is saying no is
 * how you get four reports for one part. The count stays on screen, undoable and
 * retryable, until the operator resolves it.
 */

/**
 * The grace period. 5s: long enough for a gloved operator to register what they
 * just did and reach a 44px control, short enough that a tapper is never
 * confused about whether something is still coming. Measured on the tablet at
 * 1024x768 before settling.
 */
export const ONE_TAP_WINDOW_MS = 5000;
/** How long the green RECORDED confirmation holds before the lane returns to rest. */
export const ONE_TAP_RECORDED_MS = 4000;
/** Countdown repaint interval — drives the depleting bar and the seconds digit. */
const TICK_MS = 100;

export type OneTapPhase =
  /** Nothing tapped, nothing in flight. */
  | 'idle'
  /** Tapped, counting down, UNDOABLE. */
  | 'pending'
  /** Handed to the server. */
  | 'saving'
  /** The server said yes. Final. */
  | 'recorded'
  /** The server said no (or the network did). Count preserved, retryable. */
  | 'failed';

export interface OneTapPiecesOptions {
  /**
   * Post `pieces` as ONE additive production report. Must reject on refusal —
   * the hook reads a resolved promise as "the ledger has it".
   *
   * `keepalive` is set only on the page-unload flush, where the caller should
   * hand it to `fetch` so the request outlives the document.
   */
  post: (pieces: number, opts: { keepalive: boolean }) => Promise<void>;
  /** Render a rejection as operator-readable text (server `detail`, verbatim). */
  toMessage: (err: unknown) => string;
  /** The server accepted `pieces`. Refresh the tally here. */
  onRecorded?: (pieces: number) => void;
  /**
   * The server refused. `pieces` are back in `pending` and still on screen —
   * this is for the toast, not for recovery (the lane owns recovery).
   */
  onFailed?: (pieces: number, message: string, err: unknown) => void;
  /**
   * False while the post cannot succeed at all (offline, no operator session).
   * An armed window does not fire while false and re-arms when it flips true,
   * so a connection that drops between the tap and the post does not burn the
   * delta on a request that was never going to land.
   */
  canPost?: boolean;
  /**
   * What the lane says when a window elapses while `canPost` is false. It must
   * say something: a delta that silently stops counting down, with a RETRY that
   * cannot fire, is a dead end an operator has no way out of.
   */
  blockedMessage?: string;
  windowMs?: number;
}

export interface OneTapPieces {
  phase: OneTapPhase;
  /** Tapped, not yet sent — the number UNDO removes from. */
  pending: number;
  /** Sent, awaiting the server. Not undoable. */
  inFlight: number;
  /** What the server last accepted, while `phase === 'recorded'`. */
  lastRecorded: number;
  /** Milliseconds left in the grace period (0 unless `phase === 'pending'`). */
  remainingMs: number;
  windowMs: number;
  /** Verbatim server `detail` while `phase === 'failed'`. */
  error: string | null;
  /** Everything not yet accepted by the server — what the ceiling must clamp. */
  unbanked: number;
  /** +1. Re-arms the window. */
  tap: () => void;
  /** −1 from `pending` only. Re-arms the window; disarms entirely at zero. */
  undoOne: () => void;
  /**
   * Send now, cancelling the countdown. Safe to call with nothing pending.
   *
   * Resolves once the post has SETTLED, so a caller that is about to invalidate
   * the credential — the single-operator kiosk's idle auto-logout — can bank the
   * delta before taking the token away rather than 401 its own flush.
   */
  flush: (opts?: { keepalive?: boolean }) => Promise<void>;
  /** Re-send after a refusal. */
  retry: () => void;
}

export function useOneTapPieces({
  post,
  toMessage,
  onRecorded,
  onFailed,
  canPost = true,
  blockedMessage = 'Not saved yet — waiting for the connection.',
  windowMs = ONE_TAP_WINDOW_MS,
}: OneTapPiecesOptions): OneTapPieces {
  const [phase, setPhase] = useState<OneTapPhase>('idle');
  const [pending, setPending] = useState(0);
  const [inFlight, setInFlight] = useState(0);
  const [lastRecorded, setLastRecorded] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [remainingMs, setRemainingMs] = useState(0);
  const [armedAt, setArmedAt] = useState<number | null>(null);

  // Mirrors, so the teardown flush and the timer callback read TODAY's numbers
  // rather than whatever was closed over when they were scheduled.
  const pendingRef = useRef(0);
  const inFlightRef = useRef(0);
  const canPostRef = useRef(canPost);
  const blockedMessageRef = useRef(blockedMessage);
  const postRef = useRef(post);
  const toMessageRef = useRef(toMessage);
  const onRecordedRef = useRef(onRecorded);
  const onFailedRef = useRef(onFailed);
  const timerRef = useRef<number | null>(null);
  const recordedTimerRef = useRef<number | null>(null);
  // `arm` is defined below because it schedules `flush`, but `flush` also has to
  // re-arm (a delta buffered behind an in-flight post). One forward ref breaks
  // the cycle without making either of them depend on the other's identity.
  const armRef = useRef<(() => void) | null>(null);

  canPostRef.current = canPost;
  blockedMessageRef.current = blockedMessage;
  postRef.current = post;
  toMessageRef.current = toMessage;
  onRecordedRef.current = onRecorded;
  onFailedRef.current = onFailed;

  const clearTimer = useCallback(() => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setArmedAt(null);
    setRemainingMs(0);
  }, []);

  const setPendingBoth = useCallback((next: number) => {
    pendingRef.current = next;
    setPending(next);
  }, []);

  const setInFlightBoth = useCallback((next: number) => {
    inFlightRef.current = next;
    setInFlight(next);
  }, []);

  /**
   * Hand the whole pending count to the server. Zeroes `pending` SYNCHRONOUSLY
   * before awaiting, so a timer that fires while a flush is already running
   * finds nothing to send — that is the entire double-post guard, and it is why
   * every path (timer, confirm, exit, unload, retry) can call this freely.
   */
  const flush = useCallback(
    (opts?: { keepalive?: boolean }): Promise<void> => {
      clearTimer();
      const pieces = pendingRef.current;
      if (pieces <= 0) return Promise.resolve();
      if (inFlightRef.current > 0) {
        // A post is already carrying a batch, so this delta waits its turn —
        // but `clearTimer()` above has just disarmed it, and bailing here
        // without re-arming is how a buffered count stops counting down and
        // never goes anywhere. The `.then` below re-arms on the normal path;
        // this re-arm is the guard for the abnormal one, where a request hangs
        // and never settles at all.
        armRef.current?.();
        return Promise.resolve();
      }
      if (!canPostRef.current) {
        // Nothing can land right now (offline, or a dead credential). Hold the
        // count and SAY SO — the `canPost` effect below re-arms the moment it
        // becomes postable again. Failing loudly here is what stops the lane
        // stalling at a countdown that reached zero and did nothing.
        setError(blockedMessageRef.current);
        setPhase('failed');
        return Promise.resolve();
      }
      setPendingBoth(0);
      setInFlightBoth(pieces);
      setError(null);
      setPhase('saving');
      return postRef.current(pieces, { keepalive: opts?.keepalive === true })
        .then(() => {
          setInFlightBoth(0);
          setLastRecorded(pieces);
          onRecordedRef.current?.(pieces);
          if (pendingRef.current > 0) {
            // Taps landed while this request was on the wire. They are NOT
            // recorded, so the green confirmation would be a lie — go straight
            // back to a live window and let them bank themselves. This is the
            // path a post slower than the window takes, which is ordinary on
            // shop wifi; without it the buffered delta sits un-banked behind a
            // lane claiming success and a confirm pinned disabled behind it.
            setPhase('pending');
            armRef.current?.();
            return;
          }
          setPhase('recorded');
        })
        .catch((err: unknown) => {
          // Not recorded ⇒ back onto the undoable pile, exactly where the
          // operator left them. Never an auto-retry.
          const message = toMessageRef.current(err);
          setInFlightBoth(0);
          setPendingBoth(pendingRef.current + pieces);
          setError(message);
          setPhase('failed');
          onFailedRef.current?.(pieces, message, err);
        });
    },
    [clearTimer, setPendingBoth, setInFlightBoth]
  );

  /** (Re)start the grace period. */
  const arm = useCallback(() => {
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    const now = Date.now();
    setArmedAt(now);
    setRemainingMs(windowMs);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void flush();
    }, windowMs);
  }, [flush, windowMs]);

  armRef.current = arm;

  const tap = useCallback(() => {
    if (recordedTimerRef.current != null) {
      window.clearTimeout(recordedTimerRef.current);
      recordedTimerRef.current = null;
    }
    setPendingBoth(pendingRef.current + 1);
    setError(null);
    setPhase('pending');
    arm();
  }, [arm, setPendingBoth]);

  const undoOne = useCallback(() => {
    const next = Math.max(0, pendingRef.current - 1);
    setPendingBoth(next);
    setError(null);
    if (next <= 0) {
      clearTimer();
      // Nothing pending and nothing in flight ⇒ no request was ever made.
      setPhase(inFlightRef.current > 0 ? 'saving' : 'idle');
      return;
    }
    setPhase('pending');
    arm();
  }, [arm, clearTimer, setPendingBoth]);

  const retry = useCallback(() => {
    if (pendingRef.current <= 0) return;
    setError(null);
    setPhase('pending');
    arm();
  }, [arm]);

  // Countdown repaint. Only runs while a window is armed.
  useEffect(() => {
    if (armedAt == null) return undefined;
    const interval = window.setInterval(() => {
      setRemainingMs(Math.max(0, windowMs - (Date.now() - armedAt)));
    }, TICK_MS);
    return () => window.clearInterval(interval);
  }, [armedAt, windowMs]);

  // The RECORDED confirmation is a timed state, not a permanent one — it must
  // clear itself so the next tap starts from an unambiguous rest state.
  useEffect(() => {
    if (phase !== 'recorded') return undefined;
    recordedTimerRef.current = window.setTimeout(() => {
      recordedTimerRef.current = null;
      // A tap during the confirmation already moved us on; don't stomp it.
      setPhase((current) => (current === 'recorded' ? 'idle' : current));
    }, ONE_TAP_RECORDED_MS);
    return () => {
      if (recordedTimerRef.current != null) {
        window.clearTimeout(recordedTimerRef.current);
        recordedTimerRef.current = null;
      }
    };
  }, [phase]);

  // Reconnect (or an operator session arriving) re-arms a window that could not
  // fire, so a delta stranded by a dropped connection lands on its own.
  useEffect(() => {
    if (!canPost) return;
    if (pendingRef.current > 0 && timerRef.current == null && inFlightRef.current === 0) {
      setError(null);
      setPhase('pending');
      arm();
    }
  }, [canPost, arm]);

  // Page unload — the one teardown that outlives React. `keepalive` lets the
  // request finish after the document is gone; without it a tab closed inside
  // the grace period is lost production.
  useEffect(() => {
    const onPageHide = () => flush({ keepalive: true });
    window.addEventListener('pagehide', onPageHide);
    return () => window.removeEventListener('pagehide', onPageHide);
  }, [flush]);

  // Hook teardown (the page itself unmounting). Fire and forget: the request
  // does not need the component tree, and there is nothing left to render the
  // answer into.
  useEffect(
    () => () => {
      if (timerRef.current != null) window.clearTimeout(timerRef.current);
      if (recordedTimerRef.current != null) window.clearTimeout(recordedTimerRef.current);
      if (pendingRef.current > 0 && inFlightRef.current === 0 && canPostRef.current) {
        const pieces = pendingRef.current;
        pendingRef.current = 0;
        void postRef.current(pieces, { keepalive: true }).catch(() => {
          /* nothing is mounted to show it — the pagehide/keepalive path is the net */
        });
      }
    },
    []
  );

  return {
    phase,
    pending,
    inFlight,
    lastRecorded,
    remainingMs,
    windowMs,
    error,
    unbanked: pending + inFlight,
    tap,
    undoOne,
    flush,
    retry,
  };
}

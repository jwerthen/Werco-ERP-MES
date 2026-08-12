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
 * WHY THE DELTA CARRIES A BINDING. Outliving the screen is exactly what makes a
 * bare count dangerous. A tapped delta is a claim that a SPECIFIC operator made
 * pieces on a SPECIFIC operation, and the station outlives both: badge tokens
 * expire after five minutes, the idle reset returns to the crew board, and the
 * next person to walk up scans their own badge on their own job. A delta that
 * could not be posted when it was made, and is then allowed to go out under
 * whatever pair happens to be bound when it finally can, does not merely name
 * the wrong person: it credits another operator's TimeEntry, lands against
 * another work order's part and lot, and moves stock on the wrong operation
 * (invariant 6). The row is permanent and reads exactly like a real report.
 *
 * So every delta is stamped with its `binding` AT TAP TIME and may only ever
 * post while that same pair is bound. A mismatch is never resolved by guessing —
 * it goes to `orphaned`, keeps the label naming who and where, and waits for the
 * original pair to come back or for a human to deal with it. Callers must not
 * treat `binding` as cosmetic: it is the whole attribution guarantee.
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
 * FAILURE, and the two kinds of it. A refused post moves `inFlight` BACK into
 * `pending` and stops — it never auto-retries, because a retry loop against a
 * server saying no is how you get four reports for one part. Beyond that, an
 * AMBIGUOUS failure (a network error or a timeout — anything that leaves it
 * unknown whether the row was written) is barred from every AUTOMATIC path as
 * well: the endpoint is purely additive with no idempotency key, so a request
 * that reached the server but whose answer was never seen is counted twice if
 * anything re-sends it on its own. Only a human tapping RETRY may send it again.
 * An explicit HTTP refusal is definitive — nothing was written — so it keeps the
 * automatic path.
 */

/**
 * The (operator, operation) pair a delta belongs to.
 *
 * `key` must change whenever EITHER changes — it is compared, not parsed.
 * `label` is shown to a human when a delta outlives its pair, so it has to name
 * both ("Alice Reed · WO-2026-0142 Op 20"), not just the count.
 */
export interface OneTapBinding<T = unknown> {
  key: string;
  label: string;
  /**
   * Everything the caller needs to POST this delta — the credential and the
   * operation id. It travels WITH the stamp and is handed back to `post`, so
   * the request is addressed by the same object the binding check just
   * validated. Callers must not read a live page ref inside `post`: between a
   * ref assignment and the re-render that follows it, a due timer can flush
   * with the old key satisfying the guard while the new token and operation do
   * the sending.
   */
  target: T;
}

/**
 * Is an HTTP status a DEFINITIVE refusal — the server decided, and wrote
 * nothing — or does it leave the write in doubt?
 *
 * Only a client error qualifies, and not all of those. A 5xx is not a refusal:
 * a 502/503 can come from a proxy that never got an answer from the app, and a
 * **504 is the canonical case where the write may well have committed** and only
 * the response was lost. 408 and 425 say the same thing about a request that may
 * or may not have been processed. Everything else — including no status at all —
 * is unknowable. This matters because the production endpoint is purely additive
 * with no idempotency key, so re-sending a delta that already landed counts the
 * pieces twice on a quality record.
 */
export function isDefinitiveHttpRefusal(status: number | null | undefined): boolean {
  if (typeof status !== 'number') return false;
  if (status === 408 || status === 425) return false;
  return status >= 400 && status < 500;
}

/** A delta the hook could not post and is handing back rather than dropping. */
export interface StrandedOneTapDelta {
  pieces: number;
  key: string;
  label: string;
}

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
  | 'failed'
  /**
   * The delta outlived the pair that produced it. It is HELD: no automatic path
   * will post it, because posting it now would attribute one operator's pieces
   * to another, on another operation. Only the original pair returning can bank
   * it.
   */
  | 'orphaned';

export interface OneTapPiecesOptions<T = unknown> {
  /**
   * The (operator, operation) pair currently bound. Stamped onto a delta at tap
   * time; a delta only posts while its stamp still matches. `null` means nothing
   * is bound — taps are refused and any held delta stays held.
   */
  binding: OneTapBinding<T> | null;
  /**
   * Post `pieces` as ONE additive production report. Must reject on refusal —
   * the hook reads a resolved promise as "the ledger has it".
   *
   * `keepalive` is set only on the page-unload flush, where the caller should
   * hand it to `fetch` so the request outlives the document.
   */
  post: (pieces: number, opts: { keepalive: boolean; binding: OneTapBinding<T> }) => Promise<void>;
  /** Render a rejection as operator-readable text (server `detail`, verbatim). */
  toMessage: (err: unknown) => string;
  /**
   * True when a rejection leaves it UNKNOWN whether the server wrote the row —
   * a network error, an aborted request, a timeout. Those are barred from every
   * automatic re-post. The default treats anything carrying a numeric `status`
   * as a definitive refusal and everything else as ambiguous.
   */
  isAmbiguousFailure?: (err: unknown) => boolean;
  /** The server accepted `pieces`. Refresh the tally here. */
  onRecorded?: (pieces: number) => void;
  /**
   * The server refused. `pieces` are back in `pending` and still on screen —
   * this is for the toast, not for recovery (the lane owns recovery).
   */
  onFailed?: (pieces: number, message: string, err: unknown) => void;
  /**
   * The hook is going away with a delta it cannot post. Persist it — this is the
   * last chance the pieces have to exist anywhere, and dropping them here is the
   * silent loss the runbook promises does not happen.
   */
  onStranded?: (delta: StrandedOneTapDelta) => void;
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
  /** Names the pair the held pieces belong to, whenever any are un-banked. */
  pendingLabel: string | null;
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
  /** Send now, cancelling the countdown. Safe to call with nothing pending. */
  flush: (opts?: { keepalive?: boolean }) => Promise<void>;
  /** Re-send after a refusal. The only path an ambiguous failure may take. */
  retry: () => void;
  /** Give up on a held delta. The caller must have shown what is being lost. */
  discard: () => void;
}

export function useOneTapPieces<T = unknown>({
  binding,
  post,
  toMessage,
  isAmbiguousFailure = (err) => !isDefinitiveHttpRefusal((err as { status?: number } | null)?.status),
  onRecorded,
  onFailed,
  onStranded,
  canPost = true,
  blockedMessage = 'Not saved yet — waiting for the connection.',
  windowMs = ONE_TAP_WINDOW_MS,
}: OneTapPiecesOptions<T>): OneTapPieces {
  const [phase, setPhase] = useState<OneTapPhase>('idle');
  const [pending, setPending] = useState(0);
  const [inFlight, setInFlight] = useState(0);
  const [lastRecorded, setLastRecorded] = useState(0);
  const [pendingLabel, setPendingLabel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [remainingMs, setRemainingMs] = useState(0);
  const [armedAt, setArmedAt] = useState<number | null>(null);

  // Mirrors, so the teardown flush and the timer callback read TODAY's numbers
  // rather than whatever was closed over when they were scheduled.
  const pendingRef = useRef(0);
  const inFlightRef = useRef(0);
  // The stamp. Set on the first tap of a batch, cleared only when the batch is
  // banked or discarded — never rewritten by a new binding arriving.
  const pendingBindingRef = useRef<OneTapBinding<T> | null>(null);
  // The stamp of the delta CURRENTLY ON THE WIRE. `setPendingBoth(0)` clears
  // `pendingBindingRef` when the delta leaves, so without this the flight is a
  // window in which the delta carries no stamp at all — and a failure landing
  // inside it used to be re-stamped from whatever was bound by then, which made
  // the binding check compare the delta against itself and always pass.
  const inFlightBindingRef = useRef<OneTapBinding<T> | null>(null);
  const bindingRef = useRef<OneTapBinding<T> | null>(binding);
  const canPostRef = useRef(canPost);
  const blockedMessageRef = useRef(blockedMessage);
  const ambiguousRef = useRef(false);
  const postRef = useRef(post);
  const toMessageRef = useRef(toMessage);
  const isAmbiguousRef = useRef(isAmbiguousFailure);
  const onRecordedRef = useRef(onRecorded);
  const onFailedRef = useRef(onFailed);
  const onStrandedRef = useRef(onStranded);
  const timerRef = useRef<number | null>(null);
  const recordedTimerRef = useRef<number | null>(null);
  // `arm` is defined below because it schedules `flush`, but `flush` also has to
  // re-arm (a delta buffered behind an in-flight post). One forward ref breaks
  // the cycle without making either of them depend on the other's identity.
  const armRef = useRef<(() => void) | null>(null);

  bindingRef.current = binding;
  canPostRef.current = canPost;
  blockedMessageRef.current = blockedMessage;
  postRef.current = post;
  toMessageRef.current = toMessage;
  isAmbiguousRef.current = isAmbiguousFailure;
  onRecordedRef.current = onRecorded;
  onFailedRef.current = onFailed;
  onStrandedRef.current = onStranded;

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
    if (next <= 0) {
      pendingBindingRef.current = null;
      setPendingLabel(null);
    }
  }, []);

  const setInFlightBoth = useCallback((next: number) => {
    inFlightRef.current = next;
    setInFlight(next);
  }, []);

  /** True while the held delta still belongs to the pair that is bound now. */
  const bindingMatches = useCallback(() => {
    const stamped = pendingBindingRef.current;
    if (stamped == null) return true; // nothing stamped yet — a fresh batch
    return bindingRef.current != null && bindingRef.current.key === stamped.key;
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
      const stamp = pendingBindingRef.current;
      const live = bindingRef.current;
      if (stamp == null || live == null || live.key !== stamp.key) {
        // These pieces belong to somebody else, on some other operation. There
        // is no credential that can post them truthfully right now, and posting
        // them untruthfully is the whole hazard — hold them, named.
        setPhase('orphaned');
        return Promise.resolve();
      }
      if (ambiguousRef.current) {
        // The last attempt may already have been written. No automatic path
        // gets to gamble on that; RETRY clears this flag, nothing else does.
        setPhase('failed');
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
      // `live` has just been proven to carry the stamped key, so it is the same
      // pair the guard checked — and it is what addresses the request. Taking
      // the LIVE object rather than the stored stamp is deliberate and narrow:
      // it lets a re-scan by the SAME operator on the SAME operation refresh an
      // expired token without the key ever changing. The key is immutable; only
      // the credential behind it may be renewed.
      const sending = live;
      inFlightBindingRef.current = sending;
      setPendingBoth(0);
      setInFlightBoth(pieces);
      setError(null);
      setPhase('saving');
      return postRef.current(pieces, { keepalive: opts?.keepalive === true, binding: sending })
        .then(() => {
          inFlightBindingRef.current = null;
          setInFlightBoth(0);
          setLastRecorded(pieces);
          ambiguousRef.current = false;
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
          const sentUnder = inFlightBindingRef.current;
          inFlightBindingRef.current = null;
          ambiguousRef.current = isAmbiguousRef.current(err);
          setInFlightBoth(0);

          const buffered = pendingBindingRef.current;
          if (buffered != null && sentUnder != null && buffered.key !== sentUnder.key) {
            // Taps arrived mid-flight under a DIFFERENT pair, so there is no
            // honest pile to put these back on: merging would produce one report
            // that is wrong for whoever it posted as. Write the failed batch off
            // as its own record and leave the new batch untouched.
            onStrandedRef.current?.({ pieces, key: sentUnder.key, label: sentUnder.label });
            setError(message);
            setPhase('failed');
            onFailedRef.current?.(pieces, message, err);
            return;
          }

          // RESTORE the original stamp rather than re-deriving one. Re-deriving
          // from whatever is bound now is how a delta gets silently re-labelled
          // to the operator and job that happen to be on screen when a hung
          // request finally gives up — after which the binding check compares
          // the delta against itself and waves it through.
          pendingBindingRef.current = sentUnder ?? buffered;
          setPendingBoth(pendingRef.current + pieces);
          setPendingLabel(pendingBindingRef.current?.label ?? null);
          setError(message);

          // The world may have moved while this was on the wire.
          const liveNow = bindingRef.current;
          const stampNow = pendingBindingRef.current;
          if (stampNow == null || liveNow == null || liveNow.key !== stampNow.key) {
            setPhase('orphaned');
          } else {
            setPhase('failed');
          }
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
    const current = bindingRef.current;
    if (current == null) return; // nothing bound — the lane's button is disabled too
    if (pendingRef.current > 0 && !bindingMatches()) {
      // Somebody else's pieces are still held. Merging this tap into them would
      // produce one report that is wrong for whoever it posted as, so the lane
      // stops here and the held delta has to be resolved first.
      setPhase('orphaned');
      return;
    }
    if (recordedTimerRef.current != null) {
      window.clearTimeout(recordedTimerRef.current);
      recordedTimerRef.current = null;
    }
    if (pendingRef.current <= 0) pendingBindingRef.current = current;
    setPendingLabel(pendingBindingRef.current?.label ?? null);
    setPendingBoth(pendingRef.current + 1);
    ambiguousRef.current = false;
    setError(null);
    setPhase('pending');
    arm();
  }, [arm, bindingMatches, setPendingBoth]);

  const undoOne = useCallback(() => {
    const next = Math.max(0, pendingRef.current - 1);
    setPendingBoth(next);
    setError(null);
    if (next <= 0) {
      clearTimer();
      ambiguousRef.current = false;
      // Nothing pending and nothing in flight ⇒ no request was ever made.
      setPhase(inFlightRef.current > 0 ? 'saving' : 'idle');
      return;
    }
    if (!bindingMatches()) {
      setPhase('orphaned');
      return;
    }
    setPhase('pending');
    arm();
  }, [arm, bindingMatches, clearTimer, setPendingBoth]);

  const retry = useCallback(() => {
    if (pendingRef.current <= 0) return;
    // RETRY is a human deciding to send again, which is the ONLY thing allowed
    // to clear the ambiguity bar — but it can never override the binding, since
    // whoever is standing at the kiosk cannot consent on another operator's
    // behalf.
    if (!bindingMatches()) {
      setPhase('orphaned');
      return;
    }
    ambiguousRef.current = false;
    setError(null);
    setPhase('pending');
    arm();
  }, [arm, bindingMatches]);

  /**
   * Write the held delta off. Giving up on it is a decision about real pieces
   * somebody made, so it leaves the SAME record any other way of losing them
   * would — the caller's `onStranded`. Zeroing it in silence here would just be
   * the original defect with a button on it.
   */
  const discard = useCallback(() => {
    clearTimer();
    const pieces = pendingRef.current;
    const stamp = pendingBindingRef.current;
    if (pieces > 0) {
      onStrandedRef.current?.({ pieces, key: stamp?.key ?? '', label: stamp?.label ?? '' });
    }
    ambiguousRef.current = false;
    setPendingBoth(0);
    setError(null);
    setPhase('idle');
  }, [clearTimer, setPendingBoth]);

  /**
   * Bank the delta if it can go, and write it down if it cannot.
   *
   * This is the teardown seam, and it exists because `flush` alone is not one:
   * every un-bankable branch of `flush` sets a phase and returns, which is right
   * while a screen is there to render it and useless when the document is going
   * away. A React effect cleanup does NOT run on page unload, so on a locked
   * shop tablet — which never navigates in-SPA and is recovered by reloading —
   * the unmount path never fires at all. Without this, a reload silently
   * destroys the count.
   *
   * It deliberately does NOT clear `pending`: the page may come back (a hidden
   * tab, a `pagehide` that never becomes an unload), and the caller reconciles
   * the notice against what is still held on the next mount.
   */
  const bankOrRecord = useCallback(
    (opts?: { keepalive?: boolean }) => {
      const pieces = pendingRef.current;
      if (pieces <= 0 || inFlightRef.current > 0) return;
      const stamp = pendingBindingRef.current;
      const live = bindingRef.current;
      const sendable =
        canPostRef.current && !ambiguousRef.current && stamp != null && live != null && live.key === stamp.key;
      if (sendable) {
        void flush(opts);
        return;
      }
      onStrandedRef.current?.({ pieces, key: stamp?.key ?? '', label: stamp?.label ?? '' });
    },
    [flush]
  );

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
  // fire, so a delta stranded by a dropped connection lands on its own — but
  // only when it is still THIS pair's delta and the last failure was definitive.
  const bindingKey = binding?.key ?? null;
  useEffect(() => {
    if (!canPost) return;
    if (pendingRef.current <= 0 || timerRef.current != null || inFlightRef.current > 0) return;
    if (ambiguousRef.current) return;
    if (!bindingMatches()) {
      setPhase('orphaned');
      return;
    }
    setError(null);
    setPhase('pending');
    arm();
  }, [canPost, bindingKey, arm, bindingMatches]);

  // Page unload and backgrounding — the teardowns that outlive React. `keepalive`
  // lets a sendable request finish after the document is gone; anything NOT
  // sendable is written down here rather than waiting for an unmount cleanup
  // that a closing or reloading tab never runs.
  useEffect(() => {
    const onPageHide = () => bankOrRecord({ keepalive: true });
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') bankOrRecord({ keepalive: true });
    };
    window.addEventListener('pagehide', onPageHide);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [bankOrRecord]);

  // Hook teardown (the page itself unmounting). Fire and forget: the request
  // does not need the component tree, and there is nothing left to render the
  // answer into. What it must NOT do is drop a delta it cannot send — that is
  // production disappearing with no row, no notice and no record — so anything
  // unsendable is handed to `onStranded` for the caller to persist.
  useEffect(
    () => () => {
      if (timerRef.current != null) window.clearTimeout(timerRef.current);
      if (recordedTimerRef.current != null) window.clearTimeout(recordedTimerRef.current);
      // A delta still ON THE WIRE at teardown has an unknowable outcome and
      // nothing left to render the answer into, so it gets a record too — it is
      // exactly the case where an operator would otherwise never learn.
      if (inFlightRef.current > 0) {
        const flying = inFlightBindingRef.current;
        onStrandedRef.current?.({
          pieces: inFlightRef.current,
          key: flying?.key ?? '',
          label: flying?.label ?? '',
        });
      }
      const pieces = pendingRef.current;
      if (pieces <= 0) return;
      const stamped = pendingBindingRef.current;
      const live = bindingRef.current;
      const sendable =
        canPostRef.current && !ambiguousRef.current && stamped != null && live != null && live.key === stamped.key;
      pendingRef.current = 0;
      if (sendable && live != null) {
        void postRef.current(pieces, { keepalive: true, binding: live }).catch(() => {
          /* nothing is mounted to show it — onStranded cannot run post-teardown */
        });
        return;
      }
      onStrandedRef.current?.({ pieces, key: stamped?.key ?? '', label: stamped?.label ?? '' });
    },
    []
  );

  return {
    phase,
    pending,
    inFlight,
    lastRecorded,
    pendingLabel,
    remainingMs,
    windowMs,
    error,
    unbanked: pending + inFlight,
    tap,
    undoOne,
    flush,
    retry,
    discard,
  };
}

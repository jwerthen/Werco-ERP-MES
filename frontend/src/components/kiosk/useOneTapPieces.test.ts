/**
 * `useOneTapPieces` — the one-tap `+1 PIECE` state machine.
 *
 * What is pinned here is the arithmetic of an operator's production count, so
 * every assertion below is about a piece that was made: it must be recorded
 * exactly once, or be visibly still theirs to take back. The hook's own
 * docstring names the three quantities and why collapsing any two of them
 * either loses a tap or offers an undo that cannot be honoured; these tests
 * hold that shape from the outside.
 *
 * The properties, and the accident each one prevents:
 *
 *  - UNDO INSIDE THE WINDOW NEVER POSTS. The grace period is the entire reason
 *    a commit-on-tap control is safe; a post that goes out anyway turns the
 *    undo into a lie the correction screen has to clean up.
 *  - COALESCING. Each tap RE-ARMS, so a run of parts lands as ONE report rather
 *    than N racing requests — which is also what keeps the undo honest: whatever
 *    is still on screen is still undoable.
 *  - A REFUSAL PUTS THE PIECES BACK AND STOPS. Never an auto-retry: a retry loop
 *    against a server saying no is how you get four reports for one part.
 *  - OFFLINE PARKS, IT DOES NOT BURN. A window that cannot reach the server does
 *    not fire; reconnecting re-arms it and the delta lands.
 *  - MID-FLIGHT TAPS BUFFER BEHIND THE REQUEST. Never folded into a body already
 *    sent, never dropped.
 *  - `flush()` RESOLVES ONLY ONCE THE POST HAS SETTLED, and is idempotent — that
 *    is what lets the single-operator kiosk bank a delta BEFORE `logout()` takes
 *    the credential away, instead of 401'ing its own flush.
 *  - TEARDOWN IS A FLUSH, NOT A LOSS. Unmount and `pagehide` both post, with
 *    `keepalive` so the request outlives the document — and anything they CANNOT
 *    post is handed back rather than dropped.
 *  - A DELTA ONLY EVER POSTS UNDER THE PAIR THAT MADE IT. The station outlives
 *    both the badge token and the screen, so a count that could not be sent when
 *    it was tapped must never go out under whoever scanned next, on whatever job
 *    they opened. That is not a mis-labelled row: it credits another operator's
 *    TimeEntry and moves stock on another work order's operation.
 *  - AN AMBIGUOUS FAILURE IS NEVER RE-SENT AUTOMATICALLY. The endpoint is
 *    additive with no idempotency key, so a request that may already have landed
 *    is counted twice by anything that re-sends it without a human deciding.
 */

import { renderHook, act } from '@testing-library/react';
import { useOneTapPieces, ONE_TAP_WINDOW_MS, ONE_TAP_RECORDED_MS } from './useOneTapPieces';
import type { OneTapBinding } from './useOneTapPieces';

/** A promise the test resolves/rejects by hand, to hold a post "on the wire". */
function deferred() {
  let resolve: () => void = () => undefined;
  let reject: (err: unknown) => void = () => undefined;
  const promise = new Promise<void>((res, rej) => {
    resolve = () => res();
    reject = rej;
  });
  // The hook always attaches a .catch; nothing here can go unhandled.
  return { promise, resolve, reject };
}

/**
 * Typed exactly like `OneTapPiecesOptions['post']`, so a body that contradicts
 * the real contract is a compile error rather than a passing test.
 */
type TestTarget = { operationId: number };
const makePost = () =>
  jest.fn(
    (_pieces: number, _opts: { keepalive: boolean; binding: OneTapBinding<TestTarget> }): Promise<void> =>
      Promise.resolve()
  );

const toMessage = (err: unknown) => (err instanceof Error ? err.message : String(err));

/**
 * The (operator, operation) pair for the tests that are not ABOUT the binding.
 * It never changes in those, so every delta stays with the pair that made it —
 * which is the only condition under which any of them may post at all.
 */
const BINDING = { key: 'user:7|op:31', label: 'Alice Reed · WO-2026-0142 Op 20', target: { operationId: 31 } };

/** Advance fake timers AND drain the promise chain the timer kicks off. */
const advance = (ms: number) =>
  act(async () => {
    jest.advanceTimersByTime(ms);
  });

describe('useOneTapPieces', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  describe('the grace period', () => {
    it('never posts a tap the operator took back inside the window', async () => {
      // The whole promise of the control: the tap is the commit, the window is
      // the way out of it, and taking that way out reaches the server never.
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());
      expect(result.current.phase).toBe('pending');
      expect(result.current.pending).toBe(1);

      act(() => result.current.undoOne());
      expect(result.current.phase).toBe('idle');
      expect(result.current.pending).toBe(0);
      expect(result.current.unbanked).toBe(0);

      // …and stays never, however long the station sits there.
      await advance(60_000);
      expect(post).not.toHaveBeenCalled();
    });

    it('records a tap left alone, once, when the window elapses', async () => {
      const post = makePost();
      const onRecorded = jest.fn();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING, onRecorded }));

      act(() => result.current.tap());

      // Nothing has gone out yet — the window is still the operator's.
      await advance(ONE_TAP_WINDOW_MS - 100);
      expect(post).not.toHaveBeenCalled();

      await advance(100);
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(1, expect.objectContaining({ keepalive: false }));
      expect(result.current.phase).toBe('recorded');
      expect(result.current.lastRecorded).toBe(1);
      expect(result.current.pending).toBe(0);
      expect(result.current.unbanked).toBe(0);
      expect(onRecorded).toHaveBeenCalledWith(1);

      // RECORDED is a timed confirmation, not a resting state.
      await advance(ONE_TAP_RECORDED_MS);
      expect(result.current.phase).toBe('idle');
    });
  });

  describe('coalescing', () => {
    it('lands a run of parts as ONE report, because each tap re-arms the window', async () => {
      // Five parts coming off a machine 400ms apart. Five requests would race
      // each other and make the undo meaningless; one report is the point.
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());
      for (let i = 0; i < 4; i += 1) {
        await advance(400);
        act(() => result.current.tap());
      }
      // t = 1600ms, five taps in.
      expect(result.current.pending).toBe(5);

      // Well past a single tap's own deadline had the window not re-armed…
      await advance(400);
      expect(post).not.toHaveBeenCalled();
      expect(result.current.pending).toBe(5);
      expect(result.current.phase).toBe('pending');

      // …and the whole run banks together, a full window after the LAST tap.
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(5, expect.objectContaining({ keepalive: false }));
    });

    it('takes back exactly ONE tap and re-arms, so 5 taps and an undo report 4', async () => {
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => {
        for (let i = 0; i < 5; i += 1) result.current.tap();
      });
      expect(result.current.pending).toBe(5);

      // One piece back, 1s before the original deadline.
      await advance(ONE_TAP_WINDOW_MS - 1000);
      act(() => result.current.undoOne());
      expect(result.current.pending).toBe(4);
      expect(result.current.phase).toBe('pending');

      // The undo RE-ARMED: the original deadline passes with nothing sent, so
      // the operator who just corrected the count still has a full window to
      // correct it again.
      await advance(2000);
      expect(post).not.toHaveBeenCalled();

      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(4, expect.objectContaining({ keepalive: false }));
    });
  });

  describe('refusal', () => {
    it('puts the pieces back, keeps them undoable, and NEVER retries on its own', async () => {
      const post = makePost();
      const onFailed = jest.fn();
      post.mockRejectedValueOnce(new Error('Operation is on hold'));
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING, onFailed }));

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledTimes(1);
      expect(result.current.phase).toBe('failed');
      // Not recorded ⇒ back onto the undoable pile, exactly where they were.
      expect(result.current.pending).toBe(1);
      expect(result.current.inFlight).toBe(0);
      expect(result.current.unbanked).toBe(1);
      expect(result.current.error).toBe('Operation is on hold');
      expect(onFailed).toHaveBeenCalledWith(1, 'Operation is on hold', expect.any(Error));

      // The property that keeps one refused part from becoming four reports:
      // no timer, anywhere, ever sends this again by itself.
      await advance(120_000);
      expect(post).toHaveBeenCalledTimes(1);

      // Only an explicit RETRY re-arms — and then it posts normally.
      act(() => result.current.retry());
      expect(result.current.phase).toBe('pending');
      expect(result.current.error).toBeNull();

      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(2);
      expect(post).toHaveBeenLastCalledWith(1, expect.objectContaining({ keepalive: false }));
      expect(result.current.phase).toBe('recorded');
    });
  });

  describe('offline', () => {
    it('parks an armed window rather than burning the delta on a doomed request', async () => {
      const post = makePost();
      const { result, rerender } = renderHook(
        ({ canPost }: { canPost: boolean }) => useOneTapPieces({ post, toMessage, binding: BINDING, canPost }),
        { initialProps: { canPost: false } }
      );

      act(() => result.current.tap());
      act(() => result.current.tap());

      // The window elapses with no connection: nothing goes out, nothing is lost.
      await advance(ONE_TAP_WINDOW_MS * 4);
      expect(post).not.toHaveBeenCalled();
      expect(result.current.pending).toBe(2);
      expect(result.current.unbanked).toBe(2);

      // Reconnect re-arms the stranded window on its own…
      rerender({ canPost: true });
      expect(post).not.toHaveBeenCalled();

      // …and the delta lands whole — nothing was dropped in between.
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(2, expect.objectContaining({ keepalive: false }));
      expect(result.current.phase).toBe('recorded');
    });
  });

  describe('taps arriving while a post is in flight', () => {
    it('buffers them behind the request instead of dropping them or folding them in', async () => {
      // The request already sent carries what the operator had committed when it
      // left. Folding later taps into it would report pieces the server was
      // never told about under a body that was already on the wire; dropping
      // them is lost production. They queue.
      const post = makePost();
      const first = deferred();
      post.mockReturnValueOnce(first.promise);
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenNthCalledWith(1, 1, expect.objectContaining({ keepalive: false }));
      expect(result.current.phase).toBe('saving');
      expect(result.current.inFlight).toBe(1);

      // Two more parts come off while the server is still thinking.
      act(() => {
        result.current.tap();
        result.current.tap();
      });
      expect(result.current.pending).toBe(2);
      expect(result.current.inFlight).toBe(1);
      // The first request still carries ONE — it was not amended in place.
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenNthCalledWith(1, 1, expect.objectContaining({ keepalive: false }));

      await act(async () => {
        first.resolve();
      });
      expect(result.current.inFlight).toBe(0);

      // The buffered pair goes out as its own report.
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(2);
      expect(post).toHaveBeenNthCalledWith(2, 2, expect.objectContaining({ keepalive: false }));
      expect(result.current.unbanked).toBe(0);
    });

    it('still banks them when the request outlives their own window', async () => {
      // The case above is the fast server: the post came back before the
      // buffered taps' window ran out. This is shop wifi — the post takes
      // LONGER than the 5s window, so the buffered delta's timer fires while
      // the first request is still on the wire and finds a flush it cannot
      // perform. That must not be where the count stops: nothing re-arms it
      // afterwards, so the operator is left looking at a lane that says the
      // pieces are recorded (they are not), with RECORD pinned disabled behind
      // an un-banked count that no tap of theirs will shift.
      const post = makePost();
      const slow = deferred();
      post.mockReturnValueOnce(slow.promise);
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);
      expect(result.current.inFlight).toBe(1);

      // Two more parts, and their window elapses with the first still in flight.
      act(() => {
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);

      // Nothing new could go out — correctly — but the two are still the
      // operator's, and the lane must not be claiming otherwise.
      expect(post).toHaveBeenCalledTimes(1);
      expect(result.current.pending).toBe(2);
      expect(result.current.unbanked).toBe(3); // 2 waiting + the 1 still on the wire
      expect(result.current.phase).not.toBe('recorded');

      await act(async () => {
        slow.resolve();
      });

      // The moment the wire is free the buffered pair is armed again and lands,
      // with no further tap and no screen exit to prompt it.
      expect(result.current.phase).toBe('pending');
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(2);
      expect(post).toHaveBeenNthCalledWith(2, 2, expect.objectContaining({ keepalive: false }));
      expect(result.current.unbanked).toBe(0);
      expect(result.current.phase).toBe('recorded');
    });
  });

  describe('flush', () => {
    it('resolves only once the post has SETTLED, so a caller can bank before dropping the token', async () => {
      // This is the single-operator kiosk's idle auto-logout: it flushes, and
      // only then calls logout(). If flush resolved early the credential would
      // be gone before the request went out and the operator's pieces would
      // 401 into nothing.
      const post = makePost();
      const inFlight = deferred();
      post.mockReturnValueOnce(inFlight.promise);
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => {
        result.current.tap();
        result.current.tap();
      });

      let settled = false;
      let flushed: Promise<void> = Promise.resolve();
      act(() => {
        flushed = result.current.flush().then(() => {
          settled = true;
        });
      });

      // It went out immediately — flush cancels the countdown.
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(2, expect.objectContaining({ keepalive: false }));

      await act(async () => {
        await Promise.resolve();
      });
      expect(settled).toBe(false);

      await act(async () => {
        inFlight.resolve();
        await flushed;
      });
      expect(settled).toBe(true);
      expect(result.current.phase).toBe('recorded');
    });

    it('is idempotent — a second call while the first is on the wire does not double-post', async () => {
      const post = makePost();
      const inFlight = deferred();
      post.mockReturnValueOnce(inFlight.promise);
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());

      // Deliberately NOT awaited — this one is still on the wire, which is the
      // window in which a double-post would happen.
      let firstFlush: Promise<void> = Promise.resolve();
      act(() => {
        firstFlush = result.current.flush();
      });
      expect(post).toHaveBeenCalledTimes(1);

      // Two teardown seams can fire back to back (Cancel and the view-change
      // effect, say). The second must find nothing to send — `flush` zeroes
      // `pending` synchronously before it awaits, which is the whole guard.
      await act(async () => {
        await result.current.flush();
      });
      expect(post).toHaveBeenCalledTimes(1);

      await act(async () => {
        inFlight.resolve();
        await firstFlush;
      });
      expect(post).toHaveBeenCalledTimes(1);
      expect(result.current.lastRecorded).toBe(1);
    });

    it('is safe with nothing pending', async () => {
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      await act(async () => {
        await result.current.flush();
      });
      expect(post).not.toHaveBeenCalled();
      expect(result.current.phase).toBe('idle');
    });
  });

  describe('teardown', () => {
    it('posts a pending delta when the hook unmounts, with keepalive', async () => {
      // A delta sitting in a setTimeout inside an unmounting subtree is silently
      // lost production. Unmount is a flush.
      const post = makePost();
      const { result, unmount } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => {
        result.current.tap();
        result.current.tap();
        result.current.tap();
      });
      expect(post).not.toHaveBeenCalled();

      unmount();

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(3, expect.objectContaining({ keepalive: true }));
    });

    it('posts a pending delta on pagehide, with keepalive so it outlives the document', async () => {
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => {
        result.current.tap();
        result.current.tap();
      });

      await act(async () => {
        window.dispatchEvent(new Event('pagehide'));
      });

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(2, expect.objectContaining({ keepalive: true }));
    });

    it('does not post on unmount when there is nothing pending', () => {
      const post = makePost();
      const { unmount } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      unmount();
      expect(post).not.toHaveBeenCalled();
    });
  });

  /**
   * WHO the pieces belong to, and WHICH operation they were made on.
   *
   * A tapped delta is a claim about a specific operator working a specific
   * operation. The station outlives both: badge tokens die after five minutes,
   * the idle flow-reset returns to the crew board, and the next person to walk
   * up scans their own badge on their own job. If a delta that could not be
   * posted under the pair that produced it is allowed to go out under whatever
   * pair happens to be bound when it finally can, the row is not merely
   * mis-attributed — it credits another operator's TimeEntry, lands on another
   * work order's part and lot, and moves stock against the wrong operation
   * (invariant 6). It is permanent and indistinguishable from a real report.
   *
   * So the pair is stamped AT TAP TIME and the delta may only ever post under
   * that same pair. Anything else is held, named, and left for a human.
   */
  describe('binding — the delta belongs to the pair that produced it', () => {
    const ALICE_OP20 = { key: 'user:7|op:31', label: 'Alice Reed · WO-2026-0142 Op 20', target: { operationId: 31 } };
    const BOB_OP10 = { key: 'user:9|op:44', label: 'Bob Tran · WO-2026-0199 Op 10', target: { operationId: 44 } };

    /** Renders with controllable binding + canPost, as the pages drive them. */
    const renderBound = (post: ReturnType<typeof makePost>, onStranded?: jest.Mock) =>
      renderHook(
        ({ binding, canPost }: { binding: typeof ALICE_OP20 | null; canPost: boolean }) =>
          useOneTapPieces({ post, toMessage, binding, canPost, onStranded }),
        { initialProps: { binding: ALICE_OP20 as typeof ALICE_OP20 | null, canPost: false } }
      );

    it('never posts a parked delta under a different operator on a different operation', async () => {
      // The reachable sequence: Alice taps past her 5-minute token, the 401
      // parks the count, the 90s idle flow-reset takes the screen away, and Bob
      // walks up to a DIFFERENT job and scans. Nothing about that scan says the
      // parked pieces are his.
      const post = makePost();
      const { result, rerender } = renderBound(post);

      act(() => {
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).not.toHaveBeenCalled();

      rerender({ binding: BOB_OP10, canPost: true });
      await advance(ONE_TAP_WINDOW_MS * 3);

      expect(post).not.toHaveBeenCalled();
      expect(result.current.phase).toBe('orphaned');
      // Held, and it still says whose they are and where they came from.
      expect(result.current.pending).toBe(2);
      expect(result.current.pendingLabel).toBe(ALICE_OP20.label);
    });

    it('banks it the moment the ORIGINAL pair is bound again', async () => {
      // The recovery that IS legitimate: Alice re-scans, on the same job.
      const post = makePost();
      const { result, rerender } = renderBound(post);

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).not.toHaveBeenCalled();

      rerender({ binding: ALICE_OP20, canPost: true });
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(1, expect.objectContaining({ keepalive: false }));
      expect(result.current.unbanked).toBe(0);
    });

    it('will not merge a new operator’s taps into the held count', async () => {
      // Two operators' pieces must never end up in one report — whoever it
      // posted as, it would be wrong for the other.
      const post = makePost();
      const { result, rerender } = renderBound(post);

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      rerender({ binding: BOB_OP10, canPost: true });
      act(() => result.current.tap());

      expect(result.current.pending).toBe(1);
      expect(result.current.pendingLabel).toBe(ALICE_OP20.label);
      expect(post).not.toHaveBeenCalled();
    });

    it('hands a delta it cannot post to onStranded rather than dropping it on unmount', async () => {
      // The teardown post is gated on being able to post at all, so a parked
      // count used to vanish here with no row, no notice and no record — while
      // the runbook told operators it is always banked. If it cannot be sent it
      // must at least be handed back, named, for the caller to persist.
      const post = makePost();
      const onStranded = jest.fn();
      const { result, unmount } = renderBound(post, onStranded);

      act(() => {
        result.current.tap();
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);

      unmount();

      expect(post).not.toHaveBeenCalled();
      expect(onStranded).toHaveBeenCalledWith({ pieces: 3, key: ALICE_OP20.key, label: ALICE_OP20.label });
    });
  });

  /**
   * The stamp has to survive the REQUEST, not just the tap.
   *
   * A delta handed to the server is the most exposed it ever is: the operator
   * has moved on, the screen may be gone, and the answer can take longer than
   * anything on the kiosk waits for. If the stamp is dropped for the duration of
   * the flight and re-derived when the request finally fails, the delta is
   * re-labelled with whoever is bound at THAT moment — and the binding check
   * then compares the delta against itself and passes. No second operator is
   * even required: one person, one hung request, and a move to another job is
   * enough to post their pieces against a work order they never touched.
   */
  describe('a binding change INSIDE the in-flight window', () => {
    const ANN_WO_A = { key: 'user:7|op:31', label: 'Ann Diaz · WO-A Op 20', target: { token: 'tok-A', operationId: 31 } };
    const ANN_WO_B = { key: 'user:7|op:55', label: 'Ann Diaz · WO-B Op 30', target: { token: 'tok-B', operationId: 55 } };

    const renderMoving = (post: ReturnType<typeof makePost>) =>
      renderHook(
        ({ binding }: { binding: typeof ANN_WO_A }) => useOneTapPieces({ post, toMessage, binding }),
        { initialProps: { binding: ANN_WO_A } }
      );

    it('orphans a delta whose request fails after the operator moved to another job', async () => {
      const post = makePost();
      const hung = deferred();
      post.mockReturnValueOnce(hung.promise);
      const { result, rerender } = renderMoving(post);

      act(() => {
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);

      // Ann gives up waiting, goes back to the board and scans onto WO-B.
      rerender({ binding: ANN_WO_B });

      // ...and only THEN does the original request finally fail.
      await act(async () => {
        hung.reject(new TypeError('Failed to fetch'));
      });

      // The two pieces are WO-A's. Nothing may send them to WO-B.
      expect(result.current.phase).toBe('orphaned');
      expect(result.current.pendingLabel).toBe(ANN_WO_A.label);

      await act(async () => {
        void result.current.flush();
      });
      await advance(ONE_TAP_WINDOW_MS * 3);
      expect(post).toHaveBeenCalledTimes(1);
    });

    it('posts with the stamped pair, never with whatever is bound at send time', async () => {
      // The credential and the operation must come from the same object the
      // guard checked, so a page ref that moved on cannot steer the request.
      const post = makePost();
      const { result } = renderMoving(post);

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledWith(1, expect.objectContaining({ binding: ANN_WO_A }));
    });
  });

  describe('an AMBIGUOUS failure is never re-posted on its own', () => {
    it.each([
      ['502 from a proxy', 502],
      ['503 while the API restarts', 503],
      ['504 — the canonical write-may-have-committed case', 504],
      ['408 request timeout', 408],
    ])('treats %s as ambiguous, so no automatic path re-sends it', async (_name, status) => {
      // Every non-OK response is wrapped in an error carrying a status, so
      // "there is a status" cannot mean "the server refused before writing".
      // A 504 in particular is the case where the row most likely DID commit,
      // and this endpoint is additive with no idempotency key.
      const post = makePost();
      post.mockRejectedValueOnce(Object.assign(new Error(`HTTP ${status}`), { status }));
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);

      await act(async () => {
        void result.current.flush();
      });
      await advance(ONE_TAP_WINDOW_MS * 2);
      expect(post).toHaveBeenCalledTimes(1);
    });

    it('holds a delta whose request may already have reached the server', async () => {
      // "Never auto-retries" only ever covered the in-lane timer. A network
      // error or a timeout leaves it UNKNOWN whether the row was written — and
      // the endpoint is purely additive with no idempotency key, so a second
      // send counts the pieces twice. Every automatic path (screen exit, idle,
      // lock, pagehide, reconnect) must therefore leave it alone; only a human
      // tapping RETRY may send it again.
      const post = makePost();
      post.mockRejectedValueOnce(new TypeError('Failed to fetch'));
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, binding: BINDING }));

      act(() => {
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledTimes(1);
      expect(result.current.phase).toBe('failed');
      expect(result.current.pending).toBe(2);

      // An automatic flush — this is Cancel, the idle reset, the ghost-guard,
      // Lock station and pagehide, all of which call flush() with no human
      // deciding anything.
      await act(async () => {
        void result.current.flush();
      });
      await advance(ONE_TAP_WINDOW_MS * 2);
      expect(post).toHaveBeenCalledTimes(1);

      // The operator, who can see the lane, decides.
      act(() => result.current.retry());
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(2);
      expect(post).toHaveBeenNthCalledWith(2, 2, expect.objectContaining({ keepalive: false }));
    });

    it('still lets an explicit server refusal take the automatic path', async () => {
      // A 4xx is definitive: the server refused BEFORE writing anything, so
      // re-sending cannot double-count. Only the unknowable case is barred.
      const post = makePost();
      const refusal = Object.assign(new Error('Quantity (9) cannot exceed quantity ordered (8)'), { status: 400 });
      post.mockRejectedValueOnce(refusal);
      const { result } = renderHook(() =>
        useOneTapPieces({ post, toMessage, binding: BINDING, isAmbiguousFailure: (err) => !(err as { status?: number }).status })
      );

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(1);

      await act(async () => {
        void result.current.flush();
      });
      expect(post).toHaveBeenCalledTimes(2);
    });
  });

  /**
   * Losing the pieces is allowed to happen. Losing them QUIETLY is not.
   *
   * The runbook and the operator role card both promise that tapped pieces are
   * either banked or written down. A teardown that drops an un-bankable delta
   * with no request and no record breaks that promise in the one situation an
   * operator cannot see — and a React effect cleanup does not run on page
   * unload, which is exactly how a locked shop tablet ends its session.
   */
  describe('an un-bankable delta is written down, not dropped', () => {
    const HELD = { key: 'user:7|op:31', label: 'Alice Reed · WO-2026-0142 Op 20', target: { operationId: 31 } };

    const renderUnbankable = (onStranded: jest.Mock) =>
      renderHook(() =>
        useOneTapPieces({ post: makePost(), toMessage, binding: HELD, canPost: false, onStranded })
      );

    it('records it on pagehide — the path a closed or reloaded tablet actually takes', async () => {
      const onStranded = jest.fn();
      const { result } = renderUnbankable(onStranded);

      act(() => {
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);

      await act(async () => {
        window.dispatchEvent(new Event('pagehide'));
      });

      expect(onStranded).toHaveBeenCalledWith({ pieces: 2, key: HELD.key, label: HELD.label });
    });

    it('records it when the tab is hidden', async () => {
      const onStranded = jest.fn();
      const { result } = renderUnbankable(onStranded);

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      await act(async () => {
        Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
        document.dispatchEvent(new Event('visibilitychange'));
      });

      expect(onStranded).toHaveBeenCalledWith({ pieces: 1, key: HELD.key, label: HELD.label });
    });

    it('records what DISCARD writes off, rather than zeroing it in silence', async () => {
      // Giving up on a held delta is a decision about real production. It needs
      // to leave the same trace as any other way of losing it.
      const onStranded = jest.fn();
      const { result } = renderUnbankable(onStranded);

      act(() => {
        result.current.tap();
        result.current.tap();
        result.current.tap();
      });
      await advance(ONE_TAP_WINDOW_MS);

      act(() => result.current.discard());

      expect(onStranded).toHaveBeenCalledWith({ pieces: 3, key: HELD.key, label: HELD.label });
      expect(result.current.unbanked).toBe(0);
    });
  });
});

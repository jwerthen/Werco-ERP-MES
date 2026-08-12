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
 *    `keepalive` so the request outlives the document.
 */

import { renderHook, act } from '@testing-library/react';
import { useOneTapPieces, ONE_TAP_WINDOW_MS, ONE_TAP_RECORDED_MS } from './useOneTapPieces';

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
const makePost = () => jest.fn((_pieces: number, _opts: { keepalive: boolean }): Promise<void> => Promise.resolve());

const toMessage = (err: unknown) => (err instanceof Error ? err.message : String(err));

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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, onRecorded }));

      act(() => result.current.tap());

      // Nothing has gone out yet — the window is still the operator's.
      await advance(ONE_TAP_WINDOW_MS - 100);
      expect(post).not.toHaveBeenCalled();

      await advance(100);
      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(1, { keepalive: false });
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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      expect(post).toHaveBeenCalledWith(5, { keepalive: false });
    });

    it('takes back exactly ONE tap and re-arms, so 5 taps and an undo report 4', async () => {
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      expect(post).toHaveBeenCalledWith(4, { keepalive: false });
    });
  });

  describe('refusal', () => {
    it('puts the pieces back, keeps them undoable, and NEVER retries on its own', async () => {
      const post = makePost();
      const onFailed = jest.fn();
      post.mockRejectedValueOnce(new Error('Operation is on hold'));
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage, onFailed }));

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
      expect(post).toHaveBeenLastCalledWith(1, { keepalive: false });
      expect(result.current.phase).toBe('recorded');
    });
  });

  describe('offline', () => {
    it('parks an armed window rather than burning the delta on a doomed request', async () => {
      const post = makePost();
      const { result, rerender } = renderHook(
        ({ canPost }: { canPost: boolean }) => useOneTapPieces({ post, toMessage, canPost }),
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
      expect(post).toHaveBeenCalledWith(2, { keepalive: false });
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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

      act(() => result.current.tap());
      await advance(ONE_TAP_WINDOW_MS);

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenNthCalledWith(1, 1, { keepalive: false });
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
      expect(post).toHaveBeenNthCalledWith(1, 1, { keepalive: false });

      await act(async () => {
        first.resolve();
      });
      expect(result.current.inFlight).toBe(0);

      // The buffered pair goes out as its own report.
      await advance(ONE_TAP_WINDOW_MS);
      expect(post).toHaveBeenCalledTimes(2);
      expect(post).toHaveBeenNthCalledWith(2, 2, { keepalive: false });
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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      expect(post).toHaveBeenNthCalledWith(2, 2, { keepalive: false });
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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      expect(post).toHaveBeenCalledWith(2, { keepalive: false });

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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

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
      const { result, unmount } = renderHook(() => useOneTapPieces({ post, toMessage }));

      act(() => {
        result.current.tap();
        result.current.tap();
        result.current.tap();
      });
      expect(post).not.toHaveBeenCalled();

      unmount();

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(3, { keepalive: true });
    });

    it('posts a pending delta on pagehide, with keepalive so it outlives the document', async () => {
      const post = makePost();
      const { result } = renderHook(() => useOneTapPieces({ post, toMessage }));

      act(() => {
        result.current.tap();
        result.current.tap();
      });

      await act(async () => {
        window.dispatchEvent(new Event('pagehide'));
      });

      expect(post).toHaveBeenCalledTimes(1);
      expect(post).toHaveBeenCalledWith(2, { keepalive: true });
    });

    it('does not post on unmount when there is nothing pending', () => {
      const post = makePost();
      const { unmount } = renderHook(() => useOneTapPieces({ post, toMessage }));

      unmount();
      expect(post).not.toHaveBeenCalled();
    });
  });
});

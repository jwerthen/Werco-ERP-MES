/**
 * useWakeLock — station-surface screen wake lock.
 *
 * jsdom ships no Wake Lock API, so `navigator.wakeLock` is mocked to prove the
 * lifecycle: request on mount, re-acquire on visibilitychange (browsers
 * auto-release hidden tabs' locks), release on unmount — and that the hook is
 * a clean no-op where the API is absent (plain-HTTP LAN kiosks: the API only
 * exists in secure contexts, and that no-op is by design).
 */
import { renderHook, act, waitFor } from '@testing-library/react';
import { useWakeLock } from './useWakeLock';

interface MockSentinel {
  release: jest.Mock;
}

function installWakeLock() {
  const sentinels: MockSentinel[] = [];
  const request = jest.fn().mockImplementation(async () => {
    const sentinel: MockSentinel = { release: jest.fn().mockResolvedValue(undefined) };
    sentinels.push(sentinel);
    return sentinel;
  });
  Object.defineProperty(navigator, 'wakeLock', {
    value: { request },
    configurable: true,
  });
  return { request, sentinels };
}

describe('useWakeLock', () => {
  afterEach(() => {
    // Remove the own-property mocks so jsdom's defaults come back.
    Reflect.deleteProperty(navigator, 'wakeLock');
    Reflect.deleteProperty(document, 'visibilityState');
    jest.restoreAllMocks();
  });

  it('requests a screen wake lock on mount', async () => {
    const { request } = installWakeLock();

    renderHook(() => useWakeLock());

    await waitFor(() => expect(request).toHaveBeenCalledWith('screen'));
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('re-acquires the lock when the tab becomes visible again', async () => {
    const { request } = installWakeLock();
    renderHook(() => useWakeLock());
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));

    // jsdom reports visibilityState 'visible'; firing the event simulates the
    // return from hidden (where the browser auto-released the lock).
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });

  it('does not re-acquire while the tab is hidden', async () => {
    const { request } = installWakeLock();
    renderHook(() => useWakeLock());
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));

    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      configurable: true,
    });
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    expect(request).toHaveBeenCalledTimes(1);
  });

  it('releases the lock and removes the listener on unmount', async () => {
    const { request, sentinels } = installWakeLock();
    const { unmount } = renderHook(() => useWakeLock());
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));

    unmount();

    expect(sentinels[0].release).toHaveBeenCalledTimes(1);

    // The listener is gone: further visibility flips request nothing.
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('is a clean no-op when the Wake Lock API is absent', () => {
    // No navigator.wakeLock installed (jsdom default).
    const addSpy = jest.spyOn(document, 'addEventListener');

    const { unmount } = renderHook(() => useWakeLock());

    expect(addSpy).not.toHaveBeenCalledWith('visibilitychange', expect.any(Function));
    unmount(); // must not throw
  });

  it('swallows a denied request (low battery is normal) and still unmounts cleanly', async () => {
    const request = jest.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'));
    Object.defineProperty(navigator, 'wakeLock', {
      value: { request },
      configurable: true,
    });

    const { unmount } = renderHook(() => useWakeLock());
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));

    unmount(); // no sentinel to release; must not throw
  });

  it('coalesces overlapping requests — a visibility flip mid-request cannot double-acquire', async () => {
    let resolveFirst!: (sentinel: MockSentinel) => void;
    const sentinel: MockSentinel = { release: jest.fn().mockResolvedValue(undefined) };
    const request = jest
      .fn()
      .mockImplementation(() => new Promise<MockSentinel>((resolve) => (resolveFirst = resolve)));
    Object.defineProperty(navigator, 'wakeLock', {
      value: { request },
      configurable: true,
    });

    const { unmount } = renderHook(() => useWakeLock());
    expect(request).toHaveBeenCalledTimes(1);

    // Mount request still in flight — the flip must not fire a second one
    // (the loser's lock would be overwritten and leak past unmount).
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(request).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst(sentinel);
    });
    expect(request).toHaveBeenCalledTimes(1);

    unmount();
    expect(sentinel.release).toHaveBeenCalledTimes(1);
  });

  it('never leaks a sentinel across rapid visibility flips and unmount', async () => {
    const { request, sentinels } = installWakeLock();
    const { unmount } = renderHook(() => useWakeLock());
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));

    // Three visible flips in a row, each awaited so a fresh sentinel is
    // issued (an in-flight one would be coalesced — covered above). Each
    // re-acquire release-before-overwrites its predecessor.
    for (const expected of [2, 3, 4]) {
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await waitFor(() => expect(request).toHaveBeenCalledTimes(expected));
    }

    unmount();

    // EVERY issued sentinel ends released exactly once: the first three by
    // the overwrite, the last by the unmount cleanup.
    expect(sentinels).toHaveLength(4);
    for (const sentinel of sentinels) {
      expect(sentinel.release).toHaveBeenCalledTimes(1);
    }
  });

  it('releases a lock that resolves after unmount (the cancelled in-flight request)', async () => {
    let resolveRequest!: (sentinel: MockSentinel) => void;
    const sentinel: MockSentinel = { release: jest.fn().mockResolvedValue(undefined) };
    const request = jest
      .fn()
      .mockImplementation(() => new Promise<MockSentinel>((resolve) => (resolveRequest = resolve)));
    Object.defineProperty(navigator, 'wakeLock', {
      value: { request },
      configurable: true,
    });

    const { unmount } = renderHook(() => useWakeLock());
    expect(request).toHaveBeenCalledTimes(1);

    // Unmount before the browser answers — nothing to release yet.
    unmount();
    expect(sentinel.release).not.toHaveBeenCalled();

    // The late-resolving lock must be released by the cancelled branch.
    await act(async () => {
      resolveRequest(sentinel);
    });
    expect(sentinel.release).toHaveBeenCalledTimes(1);
  });
});

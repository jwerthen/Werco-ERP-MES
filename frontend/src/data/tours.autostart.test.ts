/**
 * shouldAutoStartGettingStarted — gating for the first-login tour auto-start
 * wired into the app shell (Layout). Verifies the once-per-user behavior and
 * the guards (auth, kiosk, completion, prior dismissal).
 */

import { shouldAutoStartGettingStarted, gettingStartedAutostartKey } from './tours';

function makeStore(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    _map: map,
  };
}

const notComplete = () => false;

describe('shouldAutoStartGettingStarted', () => {
  it('starts for an authenticated, non-kiosk user who has not seen it', () => {
    const store = makeStore();
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(true);
    // It records the attempt so the next call is a no-op.
    expect(store.getItem(gettingStartedAutostartKey(7))).toBe('1');
  });

  it('does not re-trigger on a second call (dismissal/skip does not reset)', () => {
    const store = makeStore();
    const args = { userKey: 7, isKiosk: false, isTourComplete: notComplete, storage: store };
    expect(shouldAutoStartGettingStarted(args)).toBe(true);
    expect(shouldAutoStartGettingStarted(args)).toBe(false);
  });

  it('stays suppressed across a fresh mount after dismissal — the flag persists, completion does not', () => {
    // Models the real dismissal path: the user skips the tour (which does NOT
    // mark it complete, so isTourComplete remains false), then reloads. A new
    // pure call against the same persisted store must stay false. If gating
    // hung on completion alone, this would wrongly re-fire on every reload.
    const store = makeStore();
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(true);
    // Second "session" — same persisted store, tour still not complete.
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(false);
  });

  it('does not start when unauthenticated', () => {
    expect(
      shouldAutoStartGettingStarted({ userKey: null, isKiosk: false, isTourComplete: notComplete, storage: makeStore() })
    ).toBe(false);
  });

  it('does not start in kiosk mode', () => {
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: true, isTourComplete: notComplete, storage: makeStore() })
    ).toBe(false);
  });

  it('does not start when the tour is already complete', () => {
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: false, isTourComplete: () => true, storage: makeStore() })
    ).toBe(false);
  });

  it('is keyed per user — a different user still gets the tour', () => {
    const store = makeStore({ [gettingStartedAutostartKey(7)]: '1' });
    expect(
      shouldAutoStartGettingStarted({ userKey: 7, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(false);
    expect(
      shouldAutoStartGettingStarted({ userKey: 99, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(true);
  });

  it('keys on a string userKey (email fallback when id is absent)', () => {
    // Layout passes `user?.id ?? user?.email`, so a string key must work and
    // must not collide with a numeric id of a different user.
    const store = makeStore();
    expect(
      shouldAutoStartGettingStarted({
        userKey: 'rosa@werco.test',
        isKiosk: false,
        isTourComplete: notComplete,
        storage: store,
      })
    ).toBe(true);
    expect(store.getItem(gettingStartedAutostartKey('rosa@werco.test'))).toBe('1');
    // Re-fire suppressed for that string key…
    expect(
      shouldAutoStartGettingStarted({
        userKey: 'rosa@werco.test',
        isKiosk: false,
        isTourComplete: notComplete,
        storage: store,
      })
    ).toBe(false);
    // …but a different identity is unaffected.
    expect(
      shouldAutoStartGettingStarted({ userKey: 'sam@werco.test', isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(true);
  });

  it('treats userKey 0 as authenticated (guard is null/undefined, not falsy)', () => {
    // A user id of 0 is a valid identity; only null/undefined means "no user".
    const store = makeStore();
    expect(
      shouldAutoStartGettingStarted({ userKey: 0, isKiosk: false, isTourComplete: notComplete, storage: store })
    ).toBe(true);
  });

  it('does not start when userKey is undefined (no user loaded yet)', () => {
    expect(
      shouldAutoStartGettingStarted({
        userKey: undefined,
        isKiosk: false,
        isTourComplete: notComplete,
        storage: makeStore(),
      })
    ).toBe(false);
  });

  it('still starts (fails open) when storage throws — onboarding is not blocked by private-mode storage', () => {
    // localStorage can throw on read/write (Safari private mode, quota). The
    // gate swallows the error and lets the tour run this session rather than
    // blocking onboarding entirely.
    const throwingStore = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('SecurityError');
      },
    };
    expect(
      shouldAutoStartGettingStarted({
        userKey: 7,
        isKiosk: false,
        isTourComplete: notComplete,
        storage: throwingStore,
      })
    ).toBe(true);
  });

  it('persists the attempt to the real localStorage when no storage is injected', () => {
    // Exercises the default-storage branch (storage arg omitted) so the
    // production path that the Layout effect actually uses is covered too.
    localStorage.removeItem(gettingStartedAutostartKey(1234));
    expect(
      shouldAutoStartGettingStarted({ userKey: 1234, isKiosk: false, isTourComplete: notComplete })
    ).toBe(true);
    expect(localStorage.getItem(gettingStartedAutostartKey(1234))).toBe('1');
    // Second call, same default storage — suppressed.
    expect(
      shouldAutoStartGettingStarted({ userKey: 1234, isKiosk: false, isTourComplete: notComplete })
    ).toBe(false);
    localStorage.removeItem(gettingStartedAutostartKey(1234));
  });
});

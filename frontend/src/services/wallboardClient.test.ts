/**
 * wallboardClient — display-token capture/scrub + storage (A0.5).
 *
 * The display token travels in the URL FRAGMENT (#token=…) so the 365-day
 * credential never reaches server access logs; ?token= is accepted only as a
 * legacy fallback for already-printed URLs. Both forms must be scrubbed from
 * the address bar after capture.
 */

import {
  captureWallboardTokenFromUrl,
  clearWallboardToken,
  getWallboardToken,
} from './wallboardClient';

const STORAGE_KEY = 'wallboard_token';

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, '', '/wallboard');
});

describe('captureWallboardTokenFromUrl', () => {
  it('captures #token= from the fragment and scrubs it from the URL', () => {
    window.history.replaceState(null, '', '/wallboard#token=frag-jwt');

    captureWallboardTokenFromUrl();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('frag-jwt');
    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain('frag-jwt');
  });

  it('keeps other fragment params and the query string while scrubbing the token', () => {
    window.history.replaceState(null, '', '/wallboard?dept=weld#token=frag-jwt&foo=1');

    captureWallboardTokenFromUrl();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('frag-jwt');
    expect(window.location.search).toBe('?dept=weld');
    expect(window.location.hash).toBe('#foo=1');
    expect(window.location.href).not.toContain('frag-jwt');
  });

  it('falls back to legacy ?token= (already-printed URLs) and scrubs it too', () => {
    window.history.replaceState(null, '', '/wallboard?token=query-jwt&dept=weld');

    captureWallboardTokenFromUrl();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('query-jwt');
    expect(window.location.search).toBe('?dept=weld');
    expect(window.location.href).not.toContain('query-jwt');
  });

  it('prefers the fragment token when both forms are present and scrubs both', () => {
    window.history.replaceState(null, '', '/wallboard?token=query-jwt#token=frag-jwt');

    captureWallboardTokenFromUrl();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('frag-jwt');
    expect(window.location.href).not.toContain('jwt');
  });

  it('does nothing when no token is present and preserves a non-token hash', () => {
    window.history.replaceState(null, '', '/wallboard#section');

    captureWallboardTokenFromUrl();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.location.hash).toBe('#section');
  });
});

describe('getWallboardToken / clearWallboardToken', () => {
  it('prefers the display token and falls back to the user session token', () => {
    sessionStorage.setItem('token', 'user-jwt');
    expect(getWallboardToken()).toBe('user-jwt');

    sessionStorage.setItem(STORAGE_KEY, 'display-jwt');
    expect(getWallboardToken()).toBe('display-jwt');
  });

  it('clearWallboardToken removes only the display token, never the user session token', () => {
    sessionStorage.setItem(STORAGE_KEY, 'display-jwt');
    sessionStorage.setItem('token', 'user-jwt');

    clearWallboardToken();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem('token')).toBe('user-jwt');
  });
});

/**
 * wallboardClient — display-token capture/scrub + storage + setup-code claim (A0.5).
 *
 * The display token travels in the URL FRAGMENT (#token=…) so the 365-day
 * credential never reaches server access logs; ?token= is accepted only as a
 * legacy fallback for already-printed URLs. Both forms must be scrubbed from
 * the address bar after capture.
 *
 * The /tv pairing flow instead claims an 8-char setup code against the PUBLIC
 * claim endpoint and persists the returned token in localStorage
 * ('wallboard_token_persist' + 'wallboard_dept') — acceptable because the
 * credential never rides in a URL and revocation is server-checked every poll.
 */

import {
  captureWallboardTokenFromUrl,
  claimDisplayCode,
  clearWallboardToken,
  getPersistedWallboardDept,
  getStoredDisplayToken,
  getWallboardToken,
  normalizeSetupCode,
  persistWallboardToken,
} from './wallboardClient';

const STORAGE_KEY = 'wallboard_token';
const PERSIST_KEY = 'wallboard_token_persist';
const DEPT_KEY = 'wallboard_dept';

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
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

describe('token resolution and clearing', () => {
  it('resolves sessionStorage wallboard_token → localStorage persist → user session token', () => {
    sessionStorage.setItem('token', 'user-jwt');
    expect(getWallboardToken()).toBe('user-jwt');

    localStorage.setItem(PERSIST_KEY, 'claimed-jwt');
    expect(getWallboardToken()).toBe('claimed-jwt');

    sessionStorage.setItem(STORAGE_KEY, 'display-jwt');
    expect(getWallboardToken()).toBe('display-jwt');
  });

  it('getStoredDisplayToken NEVER falls back to the user session token', () => {
    sessionStorage.setItem('token', 'user-jwt');
    expect(getStoredDisplayToken()).toBeNull();

    localStorage.setItem(PERSIST_KEY, 'claimed-jwt');
    expect(getStoredDisplayToken()).toBe('claimed-jwt');

    sessionStorage.setItem(STORAGE_KEY, 'display-jwt');
    expect(getStoredDisplayToken()).toBe('display-jwt');
  });

  it('persistWallboardToken stores the token + dept in localStorage', () => {
    persistWallboardToken('claimed-jwt', 'weld');

    expect(localStorage.getItem(PERSIST_KEY)).toBe('claimed-jwt');
    expect(localStorage.getItem(DEPT_KEY)).toBe('weld');
    expect(getPersistedWallboardDept()).toBe('weld');
  });

  it('persistWallboardToken with no dept clears any previously stored dept', () => {
    persistWallboardToken('old-jwt', 'weld');
    persistWallboardToken('new-jwt', null);

    expect(localStorage.getItem(PERSIST_KEY)).toBe('new-jwt');
    expect(localStorage.getItem(DEPT_KEY)).toBeNull();
    expect(getPersistedWallboardDept()).toBeNull();
  });

  it('clearWallboardToken removes the session capture AND the persisted claim + dept, never the user session token', () => {
    sessionStorage.setItem(STORAGE_KEY, 'display-jwt');
    localStorage.setItem(PERSIST_KEY, 'claimed-jwt');
    localStorage.setItem(DEPT_KEY, 'weld');
    sessionStorage.setItem('token', 'user-jwt');

    clearWallboardToken();

    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(PERSIST_KEY)).toBeNull();
    expect(localStorage.getItem(DEPT_KEY)).toBeNull();
    expect(sessionStorage.getItem('token')).toBe('user-jwt');
  });
});

describe('storage unavailable (private mode / locked-down kiosk)', () => {
  // Every helper is try/catch-wrapped so a TV browser with storage disabled
  // degrades (re-pair after reboot) instead of white-screening the board.
  const storageThrows = () => {
    throw new DOMException('blocked', 'SecurityError');
  };

  beforeEach(() => {
    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(storageThrows);
    jest.spyOn(Storage.prototype, 'setItem').mockImplementation(storageThrows);
    jest.spyOn(Storage.prototype, 'removeItem').mockImplementation(storageThrows);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('read helpers return null instead of throwing', () => {
    expect(getStoredDisplayToken()).toBeNull();
    expect(getPersistedWallboardDept()).toBeNull();
    expect(getWallboardToken()).toBeNull();
  });

  it('write/clear/capture helpers are no-ops instead of throwing', () => {
    window.history.replaceState(null, '', '/wallboard#token=frag-jwt');

    expect(() => persistWallboardToken('claimed-jwt', 'weld')).not.toThrow();
    expect(() => clearWallboardToken()).not.toThrow();
    expect(() => captureWallboardTokenFromUrl()).not.toThrow();
  });
});

describe('normalizeSetupCode', () => {
  it('uppercases and strips spaces and dashes', () => {
    expect(normalizeSetupCode('ab-cd 12ef')).toBe('ABCD12EF');
    expect(normalizeSetupCode(' AbCd-12-eF ')).toBe('ABCD12EF');
    expect(normalizeSetupCode('ABCD12EF')).toBe('ABCD12EF');
  });
});

describe('claimDisplayCode', () => {
  const realFetch = global.fetch;
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    global.fetch = realFetch;
  });

  it('POSTs the normalized code (no auth header) and returns the parsed claim', async () => {
    const claim = { token: 'claimed-jwt', dept: 'weld', label: 'North TV', expires_at: '2027-01-01T00:00:00Z' };
    fetchMock.mockResolvedValue({ ok: true, json: async () => claim });

    await expect(claimDisplayCode('ab-cd 12ef')).resolves.toEqual(claim);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/auth/display-token/claim');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ code: 'ABCD12EF' });
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('throws CLAIM_REJECTED on any non-OK response (expired/used/unknown are one generic 404)', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: 'Not found' }) });

    await expect(claimDisplayCode('ABCD12EF')).rejects.toThrow('CLAIM_REJECTED');
  });

  it('throws NETWORK when the server is unreachable', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(claimDisplayCode('ABCD12EF')).rejects.toThrow('NETWORK');
  });
});

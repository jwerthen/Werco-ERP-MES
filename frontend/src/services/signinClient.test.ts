/**
 * signinClient — isolated PIN-station fetch helper for the visitor tablet.
 *
 * The station token is a scoped `type="signin"` credential held in sessionStorage
 * and attached ONLY to the calls this helper makes — it must never enter the
 * global axios client (whose 401 interceptor force-redirects to /login). These
 * tests pin: token storage isolation, the Bearer-attach behavior, and the
 * verbatim-`detail` error surfacing (incl. the 409 sign-out disambiguation body).
 */

import {
  SigninApiError,
  clearStationToken,
  getStationToken,
  postSignIn,
  postSignOut,
  setStationToken,
  stationLogin,
} from './signinClient';

const STORAGE_KEY = 'visitor_signin_token';

function mockJsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  const { ok = true, status = ok ? 200 : 400 } = init;
  return {
    ok,
    status,
    json: async () => body,
  } as unknown as Response;
}

let fetchMock: jest.Mock;

beforeEach(() => {
  sessionStorage.clear();
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  jest.resetAllMocks();
});

describe('station token storage', () => {
  it('set/get/clear round-trip via sessionStorage', () => {
    expect(getStationToken()).toBeNull();
    setStationToken('signin-jwt');
    expect(getStationToken()).toBe('signin-jwt');
    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('signin-jwt');
    clearStationToken();
    expect(getStationToken()).toBeNull();
  });

  it('clearStationToken leaves the user session token untouched', () => {
    sessionStorage.setItem('token', 'user-jwt');
    setStationToken('signin-jwt');
    clearStationToken();
    expect(sessionStorage.getItem('token')).toBe('user-jwt');
  });
});

describe('stationLogin', () => {
  it('stores the minted token on success', async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ token: 'minted-jwt', station_label: 'Lobby', expires_in: 86400 })
    );

    const res = await stationLogin(1, '1234');

    expect(res.station_label).toBe('Lobby');
    expect(getStationToken()).toBe('minted-jwt');
    // The login call itself carries no Authorization header.
    const [, init] = fetchMock.mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it('throws SigninApiError with the verbatim detail string on a bad PIN (401)', async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ detail: 'Incorrect station PIN.' }, { ok: false, status: 401 }));

    await expect(stationLogin(1, '0000')).rejects.toMatchObject({
      name: 'SigninApiError',
      status: 401,
      message: 'Incorrect station PIN.',
    });
    // No token persisted on failure.
    expect(getStationToken()).toBeNull();
  });
});

describe('postSignIn', () => {
  it('attaches the station token as a Bearer header', async () => {
    setStationToken('signin-jwt');
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ id: 7, visitor_name: 'Jane' }));

    await postSignIn({ visitor_name: 'Jane', purpose: 'meeting', safety_acknowledged: true });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/visitor-logs/sign-in');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer signin-jwt');
  });

  it('surfaces the server detail verbatim on rejection', async () => {
    setStationToken('signin-jwt');
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ detail: 'Safety acknowledgment is required.' }, { ok: false, status: 422 })
    );

    await expect(
      postSignIn({ visitor_name: 'Jane', purpose: 'meeting', safety_acknowledged: false })
    ).rejects.toMatchObject({ status: 422, message: 'Safety acknowledgment is required.' });
  });
});

describe('postSignOut', () => {
  it('returns the updated row on a clean sign-out', async () => {
    setStationToken('signin-jwt');
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ id: 7, status: 'signed_out' }));

    const row = await postSignOut({ name: 'Jane' });
    expect(row.status).toBe('signed_out');
  });

  it('carries the 409 disambiguation body (message + matches) on SigninApiError.detail', async () => {
    setStationToken('signin-jwt');
    const detail = {
      message: 'Multiple open visits found.',
      matches: [
        { id: 11, visitor_company: 'Acme', signed_in_at: '2026-06-30T12:00:00' },
        { id: 12, visitor_company: 'Globex', signed_in_at: '2026-06-30T13:30:00' },
      ],
    };
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ detail }, { ok: false, status: 409 }));

    let caught: unknown = null;
    try {
      await postSignOut({ name: 'Jane' });
    } catch (e) {
      caught = e;
    }

    // NB: assert on shape, not `instanceof` — `class SigninApiError extends Error`
    // loses its prototype identity under the test transpile target (the classic
    // ES5 built-in-subclass gotcha), so `instanceof` is unreliable here even
    // though the runtime object is a real SigninApiError.
    const e = caught as SigninApiError;
    expect(e?.name).toBe('SigninApiError');
    expect(e.status).toBe(409);
    // The object detail is preserved so the caller can render the picker.
    expect(e.detail).toEqual(detail);
    // The human message falls back to the object's `message` field.
    expect(e.message).toBe('Multiple open visits found.');
  });
});

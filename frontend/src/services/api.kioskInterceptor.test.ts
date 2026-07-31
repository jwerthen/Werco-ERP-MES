/**
 * The axios 401 interceptor's kiosk guard (Kiosk Foundry redesign, decision 13).
 *
 * A dead session on an office page hard-redirects to /login. On the /kiosk
 * surface that redirect would strand an unattended shop terminal on the office
 * login form, so BOTH hard-redirect sites (the refresh-failure branch and the
 * plain-401 branch) now route through `redirectToLoginUnlessKiosk`: on a
 * /kiosk path the session is still cleared (logout() → sessionStorage wiped +
 * `werco:auth-token-changed` dispatched, which flips AuthContext to
 * signed-out → OperatorKiosk falls back to its badge screen) but NO navigation
 * happens and the caller's promise rejects.
 *
 * Test mechanics: axios is mocked at the module boundary (the
 * api.laserNest.test.ts pattern) so the real ApiService registers its response
 * interceptor against our stub — we capture the rejection handler and invoke
 * it directly. jsdom's window.location cannot be stubbed (non-configurable),
 * so the real path is set via history.replaceState and an ATTEMPTED
 * navigation is observed through jsdom's virtual-console signature:
 * console.error("Error: Not implemented: navigation (except hash changes)").
 */

const responseUse = jest.fn();
const rawAxiosPost = jest.fn();

const mockAxiosInstance = {
  get: jest.fn(),
  post: jest.fn(),
  put: jest.fn(),
  patch: jest.fn(),
  delete: jest.fn(),
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: responseUse },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: rawAxiosPost },
    create,
  };
});

import api from './api';

type RejectionHandler = (error: unknown) => Promise<unknown>;

/** The error-side handler ApiService registered on the response interceptor. */
function rejectionHandler(): RejectionHandler {
  expect(responseUse).toHaveBeenCalled();
  return responseUse.mock.calls[0][1] as RejectionHandler;
}

function http401(url: string) {
  return {
    response: { status: 401, data: { detail: 'Could not validate credentials' } },
    config: { url },
  };
}

const NAV_NOT_IMPLEMENTED = /Not implemented: navigation/;

function navigationAttempts(spy: jest.SpyInstance): number {
  return spy.mock.calls.filter((call) => call.some((arg) => NAV_NOT_IMPLEMENTED.test(String(arg)))).length;
}

describe('api response interceptor — kiosk 401 guard', () => {
  let errorSpy: jest.SpyInstance;

  beforeEach(() => {
    sessionStorage.clear();
    // Force the no-refresh-token 401 branch by default; individual tests
    // reinstate a refresh token to exercise the refresh-failure branch.
    (api as unknown as { refreshToken: string | null }).refreshToken = null;
    errorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    errorSpy.mockRestore();
    window.history.replaceState(null, '', '/');
  });

  it('401 on a /kiosk path: clears the session, dispatches the token event, rejects — and does NOT navigate', async () => {
    window.history.replaceState(null, '', '/kiosk?kiosk=1&work_center_id=7');
    sessionStorage.setItem('token', 'dead-jwt');
    const tokenEvents = jest.fn();
    window.addEventListener('werco:auth-token-changed', tokenEvents);

    const error = http401('/shop-floor/my-active-job');
    await expect(rejectionHandler()(error)).rejects.toBe(error);

    // Session cleared + AuthContext notified (the badge-screen fallback path).
    expect(sessionStorage.getItem('token')).toBeNull();
    expect(tokenEvents).toHaveBeenCalled();
    // The load-bearing assertion: no navigation was even ATTEMPTED.
    expect(navigationAttempts(errorSpy)).toBe(0);
    expect(window.location.pathname).toBe('/kiosk');

    window.removeEventListener('werco:auth-token-changed', tokenEvents);
  });

  it('401 on a non-kiosk path: the hard redirect to /login is preserved', async () => {
    window.history.replaceState(null, '', '/dashboard');
    sessionStorage.setItem('token', 'dead-jwt');

    const error = http401('/work-orders/');
    await expect(rejectionHandler()(error)).rejects.toBe(error);

    expect(sessionStorage.getItem('token')).toBeNull();
    // jsdom cannot actually navigate; the attempt itself proves the redirect.
    expect(navigationAttempts(errorSpy)).toBe(1);
  });

  it('refresh-failure branch on a /kiosk path: rejects with the refresh error, still no navigation', async () => {
    window.history.replaceState(null, '', '/kiosk?kiosk=1&station=3');
    sessionStorage.setItem('token', 'dead-jwt');
    (api as unknown as { refreshToken: string | null }).refreshToken = 'stale-refresh';
    const refreshError = new Error('refresh dead');
    rawAxiosPost.mockRejectedValueOnce(refreshError);

    const error = http401('/shop-floor/work-center-queue/7');
    await expect(rejectionHandler()(error)).rejects.toBe(refreshError);

    expect(sessionStorage.getItem('token')).toBeNull();
    expect(navigationAttempts(errorSpy)).toBe(0);
  });

  it('401 from an auth endpoint neither logs out nor redirects (the login form owns that error)', async () => {
    window.history.replaceState(null, '', '/login');
    sessionStorage.setItem('token', 'still-valid');

    const error = http401('/auth/login');
    await expect(rejectionHandler()(error)).rejects.toBe(error);

    expect(sessionStorage.getItem('token')).toBe('still-valid');
    expect(navigationAttempts(errorSpy)).toBe(0);
  });
});

/**
 * `floor_pin_reauth_required` in the axios client (PR D review fixes).
 *
 * The server answers 401 `{"code": "floor_pin_reauth_required", ...}` on ANY request once a
 * session's sign-on no longer meets the floor-PIN rule (a badge + PIN session whose PIN was
 * reset, cleared or promoted away; a badge-only one once a PIN is required) — and refresh
 * answers the same code. So the client must treat it as "sign in again": clear the session
 * on the same path a refused refresh takes, leave the server's message for the sign-in
 * screen (utils/signInNotice), and NEVER try a refresh for it.
 *
 * Mechanics as in api.kioskInterceptor.test.ts: axios is mocked at the module boundary, the
 * real ApiService registers its interceptor against the stub, and an attempted navigation
 * is observed through jsdom's "Not implemented: navigation" virtual-console error.
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
import { FLOOR_PIN_REAUTH_CODE, FLOOR_PIN_REAUTH_MESSAGE } from './badgeSignOn';
import { consumeSignInNotice } from '../utils/signInNotice';

type RejectionHandler = (error: unknown) => Promise<unknown>;

function rejectionHandler(): RejectionHandler {
  expect(responseUse).toHaveBeenCalled();
  return responseUse.mock.calls[0][1] as RejectionHandler;
}

const SERVER_MESSAGE = 'Sign in again with your badge and PIN.';

function reauth401(url: string, detail: unknown = { code: FLOOR_PIN_REAUTH_CODE, message: SERVER_MESSAGE }) {
  return { response: { status: 401, data: { detail } }, config: { url } };
}

const NAV_NOT_IMPLEMENTED = /Not implemented: navigation/;
type ConsoleErrorSpy = jest.SpyInstance<void, unknown[]>;
const navigationAttempts = (spy: ConsoleErrorSpy) =>
  spy.mock.calls.filter(call => call.some(arg => NAV_NOT_IMPLEMENTED.test(String(arg)))).length;

const setRefreshToken = (value: string | null) => {
  (api as unknown as { refreshToken: string | null }).refreshToken = value;
};

describe('api response interceptor — floor_pin_reauth_required', () => {
  let errorSpy: ConsoleErrorSpy;

  beforeEach(() => {
    sessionStorage.clear();
    rawAxiosPost.mockReset();
    setRefreshToken(null);
    errorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    errorSpy.mockRestore();
    window.history.replaceState(null, '', '/');
  });

  it('on any request: ends the session with the message and never tries a refresh', async () => {
    window.history.replaceState(null, '', '/work-orders/42');
    sessionStorage.setItem('token', 'pin-session');
    setRefreshToken('still-has-a-refresh-token');

    const error = reauth401('/work-orders/42');
    await expect(rejectionHandler()(error)).rejects.toBe(error);

    expect(rawAxiosPost).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('token')).toBeNull();
    expect(navigationAttempts(errorSpy)).toBe(1);
    expect(consumeSignInNotice()).toBe(SERVER_MESSAGE);
  });

  it('on a /kiosk path: clears the session for the badge screen, leaves the message, no navigation', async () => {
    window.history.replaceState(null, '', '/kiosk?work_center_id=7');
    sessionStorage.setItem('token', 'pin-session');
    setRefreshToken('still-has-a-refresh-token');
    const tokenEvents = jest.fn();
    window.addEventListener('werco:auth-token-changed', tokenEvents);

    const error = reauth401('/shop-floor/my-active-job');
    await expect(rejectionHandler()(error)).rejects.toBe(error);

    expect(rawAxiosPost).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('token')).toBeNull();
    expect(tokenEvents).toHaveBeenCalled();
    expect(navigationAttempts(errorSpy)).toBe(0);
    expect(consumeSignInNotice()).toBe(SERVER_MESSAGE);
    window.removeEventListener('werco:auth-token-changed', tokenEvents);
  });

  it('a refresh refused with the code ends the session the same way (and does not retry)', async () => {
    window.history.replaceState(null, '', '/dashboard');
    sessionStorage.setItem('token', 'expired-access');
    setRefreshToken('badge-only-refresh');
    const refreshError = reauth401('/auth/refresh');
    rawAxiosPost.mockRejectedValueOnce(refreshError);

    const expired = {
      response: { status: 401, data: { detail: 'Could not validate credentials' } },
      config: { url: '/work-orders/' },
    };
    await expect(rejectionHandler()(expired)).rejects.toBe(refreshError);

    expect(rawAxiosPost).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem('token')).toBeNull();
    expect(navigationAttempts(errorSpy)).toBe(1);
    expect(consumeSignInNotice()).toBe(SERVER_MESSAGE);
  });

  it('with no server text, the notice is our own copy', async () => {
    window.history.replaceState(null, '', '/kiosk');
    sessionStorage.setItem('token', 'pin-session');

    await expect(rejectionHandler()(reauth401('/users/me', { code: FLOOR_PIN_REAUTH_CODE }))).rejects.toBeDefined();

    expect(consumeSignInNotice()).toBe(FLOOR_PIN_REAUTH_MESSAGE);
    // Shown once.
    expect(consumeSignInNotice()).toBeNull();
  });

  it('an ordinary 401 leaves no notice', async () => {
    window.history.replaceState(null, '', '/kiosk');
    sessionStorage.setItem('token', 'dead-jwt');

    const plain = {
      response: { status: 401, data: { detail: 'Could not validate credentials' } },
      config: { url: '/users/me' },
    };
    await expect(rejectionHandler()(plain)).rejects.toBe(plain);

    expect(consumeSignInNotice()).toBeNull();
  });
});

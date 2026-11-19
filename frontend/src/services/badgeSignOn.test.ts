/**
 * badgeSignOn — the ONE client implementation of "badge sign-on with the floor-PIN
 * challenge" (AS9100D 8.5.2), shared by the desk Login page, the solo kiosk and every
 * crew-station scan.
 *
 * Pinned here:
 *  - the challenge loop: a 401 `floor_pin_required` asks for a PIN and resends badge +
 *    PIN; a 401 on a PIN-bearing attempt stays on the pad with the ONE uniform text
 *    "Invalid badge or PIN" whatever the server said (no lock tier is ever revealed);
 *    success returns the attempt's own result;
 *  - cancel throws `BadgeSignOnCancelledError` (and always takes the pad down);
 *  - rethrow semantics: every error that is not the challenge -- 403 `floor_pin_not_set`
 *    or `floor_pin_reset_required`, a plain bad badge, a network failure -- reaches the
 *    caller UNWRAPPED (same object), before or after a PIN; so does a first-try 429;
 *  - a 429 AFTER a PIN (the per-IP PIN throttle) is shown on the pad as a final ask, then
 *    ends as `BadgeSignOnEndedError` (a cancellation: the pad already said why);
 *  - a badge-only attempt passes NO argument, so the two real transports
 *    (`api.loginWithEmployeeId`, `kioskStationClient.mintBadgeToken`) send no `pin` key.
 */

const mockPost = jest.fn();

jest.mock('axios', () => {
  const instance = {
    get: jest.fn(),
    post: (...args: unknown[]) => mockPost(...args),
    put: jest.fn(),
    patch: jest.fn(),
    delete: jest.fn(),
    defaults: { headers: { common: {} as Record<string, string> } },
    interceptors: { request: { use: jest.fn() }, response: { use: jest.fn() } },
  };
  const create = jest.fn(() => instance);
  return { __esModule: true, default: { create, post: jest.fn() }, create };
});

import api from './api';
import { KioskApiError, mintBadgeToken, setStationToken } from './kioskStationClient';
import {
  BadgeSignOnCancelledError,
  BadgeSignOnEndedError,
  FLOOR_PIN_NOT_SET_CODE,
  FLOOR_PIN_REAUTH_CODE,
  FLOOR_PIN_REAUTH_MESSAGE,
  FLOOR_PIN_REQUIRED_CODE,
  FLOOR_PIN_RESET_REQUIRED_CODE,
  FloorPinPrompter,
  FloorPinRequest,
  INVALID_BADGE_OR_PIN,
  badgeSignOnErrorMessage,
  floorPinErrorCode,
  floorPinReauthMessage,
  isBadgeSignOnCancelled,
  isFloorPinNotSet,
  isFloorPinReauthRequired,
  isFloorPinRequired,
  isFloorPinResetRequired,
  isValidFloorPinFormat,
  signOnWithBadge,
} from './badgeSignOn';

const CHALLENGE_DETAIL = { code: FLOOR_PIN_REQUIRED_CODE, message: 'Enter your PIN.' };
const NOT_SET_DETAIL = {
  code: FLOOR_PIN_NOT_SET_CODE,
  message: 'No PIN is set for this badge. Ask a supervisor to set one.',
};
const RESET_REQUIRED_DETAIL = {
  code: FLOOR_PIN_RESET_REQUIRED_CODE,
  message: 'Your role changed. Ask an Admin to set a new floor PIN.',
};
const REAUTH_DETAIL = { code: FLOOR_PIN_REAUTH_CODE, message: 'Sign in again with your badge and PIN.' };
const THROTTLED = 'Too many failed sign-in attempts — wait a few minutes';

/** An Axios-shaped error (what api.loginWithEmployeeId rejects with). */
function axiosError(status: number, detail?: unknown): Error & { response: { status: number; data: unknown } } {
  const err = new Error(`Request failed with status code ${status}`) as Error & {
    response: { status: number; data: unknown };
  };
  err.response = { status, data: detail === undefined ? {} : { detail } };
  return err;
}

/** A KioskApiError (what kioskStationClient.mintBadgeToken rejects with). */
function kioskError(status: number, detail: unknown, message = ''): KioskApiError {
  return new KioskApiError(status, detail, message);
}

/** A scripted prompter: answers each ask from `answers` in order and records what it was asked. */
function scriptedPrompter(answers: Array<string | null>) {
  const asks: FloorPinRequest[] = [];
  const requestPin = jest.fn(async (request: FloorPinRequest) => {
    asks.push({ ...request });
    if (answers.length === 0) throw new Error('prompter asked more times than scripted');
    return answers.shift() as string | null;
  });
  const close = jest.fn();
  const prompter: FloorPinPrompter = { requestPin, close };
  return { prompter, requestPin, close, asks };
}

describe('isValidFloorPinFormat', () => {
  it.each(['0912', '12345', '830416', '0000'])('accepts %s (shape only; the server owns strength)', pin => {
    expect(isValidFloorPinFormat(pin)).toBe(true);
  });

  it.each(['', '123', '1234567', '12a4', ' 1234', '1234 ', '12 34', '١٢٣٤', '1234\n'])('refuses %j', pin => {
    expect(isValidFloorPinFormat(pin)).toBe(false);
  });
});

describe('error classification', () => {
  it('reads the challenge from both error shapes', () => {
    expect(isFloorPinRequired(axiosError(401, CHALLENGE_DETAIL))).toBe(true);
    expect(isFloorPinRequired(kioskError(401, CHALLENGE_DETAIL, 'Enter your PIN.'))).toBe(true);
    expect(floorPinErrorCode(kioskError(401, CHALLENGE_DETAIL))).toBe(FLOOR_PIN_REQUIRED_CODE);
  });

  it('is not the challenge without BOTH the 401 and the code', () => {
    expect(isFloorPinRequired(axiosError(401, 'Invalid employee ID'))).toBe(false);
    expect(isFloorPinRequired(axiosError(403, CHALLENGE_DETAIL))).toBe(false);
    expect(isFloorPinRequired(axiosError(401, { code: 'something_else' }))).toBe(false);
    expect(isFloorPinRequired(axiosError(401, [CHALLENGE_DETAIL]))).toBe(false);
    expect(isFloorPinRequired(new Error('Network Error'))).toBe(false);
    expect(isFloorPinRequired(null)).toBe(false);
    expect(isFloorPinRequired(undefined)).toBe(false);
  });

  it('reads floor_pin_not_set only on a 403', () => {
    expect(isFloorPinNotSet(axiosError(403, NOT_SET_DETAIL))).toBe(true);
    expect(isFloorPinNotSet(kioskError(403, NOT_SET_DETAIL))).toBe(true);
    expect(isFloorPinNotSet(axiosError(401, NOT_SET_DETAIL))).toBe(false);
  });

  it('reads floor_pin_reset_required only on a 403, and shows its message', () => {
    expect(isFloorPinResetRequired(axiosError(403, RESET_REQUIRED_DETAIL))).toBe(true);
    expect(isFloorPinResetRequired(kioskError(403, RESET_REQUIRED_DETAIL))).toBe(true);
    expect(isFloorPinResetRequired(axiosError(401, RESET_REQUIRED_DETAIL))).toBe(false);
    expect(isFloorPinResetRequired(axiosError(403, NOT_SET_DETAIL))).toBe(false);
    expect(badgeSignOnErrorMessage(axiosError(403, RESET_REQUIRED_DETAIL), 'fallback')).toBe(
      RESET_REQUIRED_DETAIL.message
    );
  });

  it('reads floor_pin_reauth_required only on a 401, with its message or our own', () => {
    expect(isFloorPinReauthRequired(axiosError(401, REAUTH_DETAIL))).toBe(true);
    expect(isFloorPinReauthRequired(axiosError(403, REAUTH_DETAIL))).toBe(false);
    expect(isFloorPinReauthRequired(axiosError(401, 'Could not validate credentials'))).toBe(false);
    expect(floorPinReauthMessage(axiosError(401, REAUTH_DETAIL))).toBe(REAUTH_DETAIL.message);
    expect(floorPinReauthMessage(axiosError(401, { code: FLOOR_PIN_REAUTH_CODE }))).toBe(FLOOR_PIN_REAUTH_MESSAGE);
  });

  it('turns a refusal into display text and never renders an object', () => {
    expect(badgeSignOnErrorMessage(axiosError(401, 'Invalid badge or PIN'), 'fallback')).toBe('Invalid badge or PIN');
    expect(badgeSignOnErrorMessage(axiosError(403, NOT_SET_DETAIL), 'fallback')).toBe(NOT_SET_DETAIL.message);
    expect(badgeSignOnErrorMessage(kioskError(401, null, 'Invalid badge'), 'fallback')).toBe('Invalid badge');
    expect(badgeSignOnErrorMessage(kioskError(500, { code: 'x' }, ''), 'fallback')).toBe('fallback');
    expect(badgeSignOnErrorMessage({}, 'fallback')).toBe('fallback');
    expect(badgeSignOnErrorMessage(new BadgeSignOnCancelledError(), 'fallback')).toBeNull();
    const shown = badgeSignOnErrorMessage(axiosError(403, NOT_SET_DETAIL), 'fallback');
    expect(shown).not.toContain('[object');
  });

  it('cancellation survives instanceof', () => {
    const err = new BadgeSignOnCancelledError();
    expect(err).toBeInstanceOf(BadgeSignOnCancelledError);
    expect(err).toBeInstanceOf(Error);
    expect(isBadgeSignOnCancelled(err)).toBe(true);
    expect(isBadgeSignOnCancelled(new Error('PIN entry cancelled.'))).toBe(false);
  });
});

describe('signOnWithBadge', () => {
  it('a badge with no PIN signs in on the first try with no argument and never opens the pad', async () => {
    const attempt = jest.fn(async (..._pin: [] | [string]) => 'session');
    const { prompter, requestPin, close } = scriptedPrompter([]);

    await expect(signOnWithBadge(attempt, prompter)).resolves.toBe('session');

    expect(attempt).toHaveBeenCalledTimes(1);
    expect(attempt.mock.calls[0]).toEqual([]);
    expect(requestPin).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it('answers the challenge with the PIN, returns the result and closes the pad', async () => {
    const attempt = jest.fn().mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL)).mockResolvedValueOnce('session');
    const { prompter, asks, close } = scriptedPrompter(['0912']);

    await expect(signOnWithBadge(attempt, prompter)).resolves.toBe('session');

    expect(attempt.mock.calls).toEqual([[], ['0912']]);
    expect(asks).toEqual([{ error: null }]);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('a wrong PIN keeps the pad up with the refusal, and the next PIN signs in', async () => {
    const attempt = jest
      .fn()
      .mockRejectedValueOnce(kioskError(401, CHALLENGE_DETAIL, 'Enter your PIN.'))
      .mockRejectedValueOnce(kioskError(401, INVALID_BADGE_OR_PIN, INVALID_BADGE_OR_PIN))
      .mockRejectedValueOnce(kioskError(401, INVALID_BADGE_OR_PIN, INVALID_BADGE_OR_PIN))
      .mockResolvedValueOnce({ access_token: 'op' });
    const { prompter, asks, close } = scriptedPrompter(['1111', '2222', '0912']);

    await expect(signOnWithBadge(attempt, prompter)).resolves.toEqual({ access_token: 'op' });

    expect(attempt.mock.calls).toEqual([[], ['1111'], ['2222'], ['0912']]);
    expect(asks).toEqual([{ error: null }, { error: INVALID_BADGE_OR_PIN }, { error: INVALID_BADGE_OR_PIN }]);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['the challenge again', () => kioskError(401, CHALLENGE_DETAIL, 'Enter your PIN.')],
    ['a lock-tier text', () => axiosError(401, 'PIN locked for 2 hours')],
    ['a structured lock refusal', () => axiosError(401, { code: 'floor_pin_locked', message: 'Locked for 7 days' })],
  ])('a PIN refused with %s re-asks with the uniform text, never the server one', async (_label, makeError) => {
    const attempt = jest
      .fn()
      .mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL))
      .mockRejectedValueOnce(makeError())
      .mockResolvedValueOnce('ok');
    const { prompter, asks } = scriptedPrompter(['1111', '0912']);

    await signOnWithBadge(attempt, prompter);

    expect(asks[1]).toEqual({ error: INVALID_BADGE_OR_PIN });
  });

  it('a 401 with no usable text re-asks with the uniform refusal', async () => {
    const attempt = jest
      .fn()
      .mockRejectedValueOnce(kioskError(401, CHALLENGE_DETAIL))
      .mockRejectedValueOnce(kioskError(401, null, ''))
      .mockResolvedValueOnce('ok');
    const { prompter, asks } = scriptedPrompter(['1111', '0912']);

    await signOnWithBadge(attempt, prompter);

    expect(asks[1]).toEqual({ error: INVALID_BADGE_OR_PIN });
  });

  it('Cancel throws BadgeSignOnCancelledError, sends no PIN and closes the pad', async () => {
    const attempt = jest.fn().mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL));
    const { prompter, close } = scriptedPrompter([null]);

    const outcome = signOnWithBadge(attempt, prompter);

    await expect(outcome).rejects.toBeInstanceOf(BadgeSignOnCancelledError);
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('Cancel after a wrong PIN still throws the cancellation, not the refusal', async () => {
    const attempt = jest
      .fn()
      .mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL))
      .mockRejectedValueOnce(axiosError(401, INVALID_BADGE_OR_PIN));
    const { prompter, close } = scriptedPrompter(['1111', null]);

    const caught = await signOnWithBadge(attempt, prompter).catch((err: unknown) => err);

    expect(isBadgeSignOnCancelled(caught)).toBe(true);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['403 floor_pin_not_set', () => axiosError(403, NOT_SET_DETAIL)],
    ['403 floor_pin_reset_required', () => axiosError(403, RESET_REQUIRED_DETAIL)],
    ['403 kiosk floor_pin_reset_required', () => kioskError(403, RESET_REQUIRED_DETAIL, RESET_REQUIRED_DETAIL.message)],
    ['401 unknown badge', () => axiosError(401, 'Invalid employee ID')],
    ['401 kiosk invalid badge', () => kioskError(401, 'Invalid badge', 'Invalid badge')],
    ['429 throttled', () => axiosError(429, 'Too many failed sign-in attempts')],
    ['network failure', () => new Error('Network Error')],
  ])('rethrows a first-try %s unwrapped without opening the pad', async (_label, makeError) => {
    const err = makeError();
    const attempt = jest.fn().mockRejectedValueOnce(err);
    const { prompter, requestPin, close } = scriptedPrompter([]);

    await expect(signOnWithBadge(attempt, prompter)).rejects.toBe(err);

    expect(requestPin).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it.each([
    ['403 floor_pin_not_set', () => kioskError(403, NOT_SET_DETAIL, NOT_SET_DETAIL.message)],
    ['403 floor_pin_reset_required', () => axiosError(403, RESET_REQUIRED_DETAIL)],
    ['500', () => axiosError(500, 'boom')],
    ['network failure', () => new Error('Network Error')],
  ])('a %s after a PIN ends the sign-on unwrapped and closes the pad', async (_label, makeError) => {
    const err = makeError();
    const attempt = jest.fn().mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL)).mockRejectedValueOnce(err);
    const { prompter, requestPin, close } = scriptedPrompter(['0912']);

    await expect(signOnWithBadge(attempt, prompter)).rejects.toBe(err);

    expect(requestPin).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('a 429 after a PIN is shown on the pad as a final ask, then ends as a cancellation', async () => {
    const throttled = axiosError(429, THROTTLED);
    const attempt = jest.fn().mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL)).mockRejectedValueOnce(throttled);
    // The final ask is answered by the person dismissing the pad (null).
    const { prompter, asks, close } = scriptedPrompter(['0912', null]);

    const caught = await signOnWithBadge(attempt, prompter).catch((err: unknown) => err);

    expect(asks).toEqual([{ error: null }, { error: THROTTLED, final: true }]);
    expect(attempt).toHaveBeenCalledTimes(2);
    expect(caught).toBeInstanceOf(BadgeSignOnEndedError);
    expect(isBadgeSignOnCancelled(caught)).toBe(true);
    expect((caught as BadgeSignOnEndedError).refusal).toBe(throttled);
    expect(badgeSignOnErrorMessage(caught, 'fallback')).toBeNull();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('a 429 with no text still ends on the pad with our own words, and sends no further PIN', async () => {
    const attempt = jest
      .fn()
      .mockRejectedValueOnce(kioskError(401, CHALLENGE_DETAIL))
      .mockRejectedValueOnce(kioskError(429, null, ''));
    // Even a (misbehaving) prompter that answers the final ask with a PIN gets no new attempt.
    const { prompter, asks } = scriptedPrompter(['0912', '1357']);

    await expect(signOnWithBadge(attempt, prompter)).rejects.toBeInstanceOf(BadgeSignOnEndedError);

    expect(asks[1]).toEqual({ error: 'Too many failed sign-in attempts — wait a few minutes.', final: true });
    expect(attempt).toHaveBeenCalledTimes(2);
  });

  it('closes the pad even when the prompter itself fails', async () => {
    const attempt = jest.fn().mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL));
    const boom = new Error('prompter broke');
    const prompter: FloorPinPrompter = { requestPin: jest.fn().mockRejectedValue(boom), close: jest.fn() };

    await expect(signOnWithBadge(attempt, prompter)).rejects.toBe(boom);
    expect(prompter.close).toHaveBeenCalledTimes(1);
  });
});

describe('the real transports through signOnWithBadge', () => {
  describe('api.loginWithEmployeeId (POST /auth/employee-login)', () => {
    beforeEach(() => mockPost.mockReset());

    it('a badge-only request carries no pin key; the challenge answer carries it', async () => {
      mockPost
        .mockRejectedValueOnce(axiosError(401, CHALLENGE_DETAIL))
        .mockResolvedValueOnce({ status: 200, data: { access_token: 'a', refresh_token: 'r' }, headers: {} });
      const { prompter } = scriptedPrompter(['0912']);

      await signOnWithBadge((...pin) => api.loginWithEmployeeId('4217', ...pin), prompter);

      expect(mockPost).toHaveBeenCalledTimes(2);
      const [firstUrl, firstBody] = mockPost.mock.calls[0];
      const [, secondBody] = mockPost.mock.calls[1];
      expect(firstUrl).toBe('/auth/employee-login');
      expect(firstBody).toEqual({ employee_id: '4217' });
      expect(Object.prototype.hasOwnProperty.call(firstBody, 'pin')).toBe(false);
      expect(secondBody).toEqual({ employee_id: '4217', pin: '0912' });
    });
  });

  describe('kioskStationClient.mintBadgeToken (POST /auth/kiosk-badge-token)', () => {
    const originalFetch = global.fetch;
    let fetchMock: jest.Mock;

    const jsonResponse = (body: unknown, status = 200) =>
      ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;

    beforeEach(() => {
      sessionStorage.clear();
      setStationToken('station-token');
      fetchMock = jest.fn();
      global.fetch = fetchMock as typeof fetch;
    });

    afterEach(() => {
      global.fetch = originalFetch;
      sessionStorage.clear();
    });

    it('a badge-only scan carries no pin key; the challenge answer carries it', async () => {
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: CHALLENGE_DETAIL }, 401))
        .mockResolvedValueOnce(jsonResponse({ detail: INVALID_BADGE_OR_PIN }, 401))
        .mockResolvedValueOnce(jsonResponse({ access_token: 'op-token', user: { id: 13 } }));
      const { prompter, asks } = scriptedPrompter(['1111', '0912']);

      const minted = await signOnWithBadge((...pin) => mintBadgeToken('E013', ...pin), prompter);

      expect(minted.access_token).toBe('op-token');
      const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string));
      expect(bodies).toEqual([
        { employee_id: 'E013' },
        { employee_id: 'E013', pin: '1111' },
        { employee_id: 'E013', pin: '0912' },
      ]);
      expect('pin' in bodies[0]).toBe(false);
      expect(asks).toEqual([{ error: null }, { error: INVALID_BADGE_OR_PIN }]);
    });

    it('a 403 floor_pin_not_set reaches the caller as the KioskApiError the page already handles', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse({ detail: NOT_SET_DETAIL }, 403));
      const { prompter, requestPin } = scriptedPrompter([]);

      const caught = await signOnWithBadge((...pin) => mintBadgeToken('E013', ...pin), prompter).catch(
        (err: unknown) => err
      );

      expect(caught).toBeInstanceOf(KioskApiError);
      expect((caught as KioskApiError).status).toBe(403);
      expect(isFloorPinNotSet(caught)).toBe(true);
      expect((caught as KioskApiError).message).toBe(NOT_SET_DETAIL.message);
      expect(requestPin).not.toHaveBeenCalled();
    });
  });
});

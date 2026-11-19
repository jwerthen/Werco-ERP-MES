/**
 * Badge sign-on with the floor-PIN challenge (AS9100D 8.5.2) — the ONE client
 * implementation, shared by every surface that exchanges a badge / employee ID
 * for a session:
 *  - POST /auth/employee-login: the desk Login page (Badge Only mode) and the solo
 *    operator kiosk (KioskBadgeLogin), both through AuthContext.loginWithEmployeeId;
 *  - POST /auth/kiosk-badge-token: every crew-station scan (kioskStationClient.mintBadgeToken).
 *
 * The protocol is server-driven and identical on both routes:
 *  1. Submit the badge alone. A user with no PIN (and a company that does not
 *     require one) signs in exactly as before — this module adds nothing.
 *  2. A 401 whose `detail.code` is `floor_pin_required` is a CHALLENGE, not a
 *     failure: ask the person for their PIN and resubmit badge + PIN.
 *  3. A 401 on a PIN-bearing attempt ("Invalid badge or PIN" — uniform, it also
 *     covers a locked PIN) keeps the pad up with that text for another try.
 *  4. A 403 whose `detail.code` is `floor_pin_not_set` (the company requires a PIN
 *     and this badge has none) is thrown to the caller; its `detail.message` is the
 *     text to show (`badgeSignOnErrorMessage`).
 *  5. Cancel (or an idle reset) throws `BadgeSignOnCancelledError`, which callers
 *     treat as "back to the badge scan" — never as an error to display.
 *
 * Errors are re-thrown UNWRAPPED so every caller's existing status checks
 * (`instanceof KioskApiError`, `err.response.status`) keep working. Both error
 * shapes are read duck-typed: an Axios error (`response.status` /
 * `response.data.detail`) and a `KioskApiError` (`status` / `detail`).
 *
 * The PIN value is only ever passed straight to the `attempt` callback — it is
 * never logged, stored or put in an error.
 */

export const FLOOR_PIN_MIN_LENGTH = 4;
export const FLOOR_PIN_MAX_LENGTH = 6;
/** 401 detail code: this badge has a PIN and the request carried none. */
export const FLOOR_PIN_REQUIRED_CODE = 'floor_pin_required';
/** 403 detail code: the company requires a PIN and this badge has none. */
export const FLOOR_PIN_NOT_SET_CODE = 'floor_pin_not_set';
/** The server's uniform refusal for a wrong / locked PIN (shown when it sends no text). */
export const INVALID_BADGE_OR_PIN = 'Invalid badge or PIN';

const FLOOR_PIN_PATTERN = new RegExp(`^\\d{${FLOOR_PIN_MIN_LENGTH},${FLOOR_PIN_MAX_LENGTH}}$`);

/** Client-side shape check only (digits, 4–6). The server owns the strength rules. */
export function isValidFloorPinFormat(pin: string): boolean {
  return FLOOR_PIN_PATTERN.test(pin);
}

/** What the PIN pad is asked to show for one prompt. */
export interface FloorPinRequest {
  /** The refusal from the previous attempt (e.g. "Invalid badge or PIN"), or null on the first ask. */
  error: string | null;
}

/**
 * The UI half of the challenge. `useFloorPinPrompt` is the React implementation;
 * the helper only needs these two calls.
 */
export interface FloorPinPrompter {
  /** Show (or re-show) the pad. Resolves with the PIN, or null when the person cancels. */
  requestPin: (request: FloorPinRequest) => Promise<string | null>;
  /** The sign-on is over (signed in, refused or cancelled): take the pad down. */
  close: () => void;
}

/** Thrown when the person cancels the PIN pad (or an idle reset clears it). Not an error to show. */
export class BadgeSignOnCancelledError extends Error {
  constructor() {
    super('PIN entry cancelled.');
    // Keep `instanceof` working when transpiled to ES5 (ts-jest) — see KioskApiError.
    Object.setPrototypeOf(this, BadgeSignOnCancelledError.prototype);
    this.name = 'BadgeSignOnCancelledError';
  }
}

export function isBadgeSignOnCancelled(err: unknown): err is BadgeSignOnCancelledError {
  return err instanceof BadgeSignOnCancelledError;
}

/** HTTP status from an Axios error or a KioskApiError; undefined for a network failure. */
function errorStatus(err: unknown): number | undefined {
  const e = err as { status?: unknown; response?: { status?: unknown } } | null | undefined;
  const status = e?.response?.status ?? e?.status;
  return typeof status === 'number' ? status : undefined;
}

/** The parsed `detail` from an Axios error (`response.data.detail`) or a KioskApiError (`detail`). */
function errorDetail(err: unknown): unknown {
  const e = err as { detail?: unknown; response?: { data?: { detail?: unknown } } } | null | undefined;
  if (e?.response) return e.response.data?.detail;
  return e?.detail;
}

/** The structured `detail.code`, when the refusal carries one. */
export function floorPinErrorCode(err: unknown): string | null {
  const detail = errorDetail(err);
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    return typeof code === 'string' ? code : null;
  }
  return null;
}

/** True for the 401 PIN challenge (`floor_pin_required`). */
export function isFloorPinRequired(err: unknown): boolean {
  return errorStatus(err) === 401 && floorPinErrorCode(err) === FLOOR_PIN_REQUIRED_CODE;
}

/** True for the 403 "company requires a PIN and this badge has none" refusal. */
export function isFloorPinNotSet(err: unknown): boolean {
  return errorStatus(err) === 403 && floorPinErrorCode(err) === FLOOR_PIN_NOT_SET_CODE;
}

/**
 * The text to show for a failed badge sign-on: a string `detail` verbatim, a
 * structured detail's `message` (floor_pin_not_set / floor_pin_required), else the
 * error's own message, else `fallback`. Never renders a raw object. Returns null for
 * a cancellation, which is not an error.
 */
export function badgeSignOnErrorMessage(err: unknown, fallback: string): string | null {
  if (isBadgeSignOnCancelled(err)) return null;
  const detail = errorDetail(err);
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  // A KioskApiError's message is already the server text; an Axios error's
  // generic "Request failed with status code N" is only used when nothing better exists.
  const message = (err as { message?: unknown } | null | undefined)?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

/** The pad text after a refused PIN attempt. */
function retryMessage(err: unknown): string {
  return badgeSignOnErrorMessage(err, INVALID_BADGE_OR_PIN) ?? INVALID_BADGE_OR_PIN;
}

/**
 * Run one badge sign-on, answering the PIN challenge through `prompter`.
 *
 * `attempt` is called with NO argument for the badge-only first try and with the
 * PIN for each challenge answer, so callers forward it with a rest parameter —
 * `(...pin) => mintBadgeToken(badgeId, ...pin)` — and a badge-only request stays
 * byte-identical to the pre-PIN one.
 */
export async function signOnWithBadge<T>(
  attempt: (...pin: [] | [pin: string]) => Promise<T>,
  prompter: FloorPinPrompter
): Promise<T> {
  try {
    return await attempt();
  } catch (err) {
    if (!isFloorPinRequired(err)) throw err;
  }

  let error: string | null = null;
  try {
    for (;;) {
      const pin = await prompter.requestPin({ error });
      if (pin == null) throw new BadgeSignOnCancelledError();
      try {
        return await attempt(pin);
      } catch (err) {
        // Any 401 on a PIN-bearing attempt is a refusal of the badge + PIN pair
        // (wrong, locked, or the challenge again): stay on the pad and say so.
        // Everything else (403 floor_pin_not_set, 429, network) ends the sign-on.
        if (errorStatus(err) !== 401) throw err;
        error = retryMessage(err);
      }
    }
  } finally {
    prompter.close();
  }
}

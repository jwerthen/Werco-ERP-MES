/**
 * A one-shot message for the next sign-in screen.
 *
 * When the server ends a session itself — `floor_pin_reauth_required`: the session's
 * sign-on no longer meets the floor-PIN rule — the API client clears the session and
 * the person lands on a sign-in screen: the Login page (a hard redirect) or, on the
 * kiosk, the badge screen (no navigation at all). Neither can read the refused
 * response, so the client leaves the reason here and the screen shows it once.
 *
 * sessionStorage, not the URL: it survives the redirect in this tab only and never
 * lands in history or a server log. The value is server-chosen fixed text, never
 * personal data. Every access is guarded — storage can be unavailable.
 */

const STORAGE_KEY = 'werco:sign_in_notice';

/** The `reason` the Login redirect carries when a session ended on `floor_pin_reauth_required`. */
export const PIN_REAUTH_REASON = 'pin_reauth';

/** Leave `message` for the next sign-in screen (replaces any earlier one). */
export function setSignInNotice(message: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, message);
  } catch {
    // storage unavailable — the screen falls back to its own copy, if it has one
  }
}

/** Read and remove the waiting message, or null. Each message is shown once. */
export function consumeSignInNotice(): string | null {
  try {
    const message = sessionStorage.getItem(STORAGE_KEY);
    if (message !== null) sessionStorage.removeItem(STORAGE_KEY);
    return message && message.trim() ? message : null;
  } catch {
    return null;
  }
}

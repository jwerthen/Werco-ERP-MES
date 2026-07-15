/**
 * Dedicated fetch helper for the TV wallboard (A0.5).
 *
 * DELIBERATELY ISOLATED from services/api.ts:
 *  - The display token must NEVER enter the global axios auth state — it is a
 *    scoped credential that only the wallboard endpoint accepts, and the
 *    shared client's 401 interceptor force-redirects to /login (fatal on an
 *    unattended TV).
 *  - The token is attached ONLY to GET /shop-floor/wallboard requests made
 *    through this helper.
 *  - The claim endpoint (POST /auth/display-token/claim) is PUBLIC by design
 *    (a TV pairing itself has no credential yet), so it also lives here on
 *    plain fetch — never the axios client.
 *
 * Token resolution order:
 *  1. `#token=<display-jwt>` URL FRAGMENT (fragments never leave the browser,
 *     so the credential can't land in server access logs). Persisted to
 *     sessionStorage and scrubbed from the address bar.
 *  2. `?token=<display-jwt>` query param — legacy fallback for already-printed
 *     URLs only; new links use the fragment. Also scrubbed.
 *  3. sessionStorage 'wallboard_token' (a previously URL-provided display token).
 *  4. localStorage 'wallboard_token_persist' (a display token claimed via a
 *     /tv setup code — see below).
 *  5. sessionStorage 'token' (a logged-in user's access token — the endpoint
 *     accepts either).
 *
 * WHY localStorage persistence is acceptable for claimed tokens: with the /tv
 * setup-code flow the credential never rides in a URL (nothing to bookmark,
 * print, or leak into access logs — strictly better than the bookmarked
 * #token= URL the docs used to recommend), and revocation is server-checked on
 * every 30s poll, so a stored-but-revoked token dies within one poll cycle.
 * Persisting it means the TV survives reboots/power cycles without re-pairing.
 */

import type { DisplayCodeClaim, WallboardResponse } from '../types/wallboard';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
/** sessionStorage — display token captured from a #token= / ?token= URL. */
const STORAGE_KEY = 'wallboard_token';
/** localStorage — display token claimed via a /tv setup code (survives reboot). */
const PERSIST_KEY = 'wallboard_token_persist';
/** localStorage — the dept the claimed display is pinned to (for /tv → /wallboard?dept=). */
const DEPT_KEY = 'wallboard_dept';

/** Capture #token= (preferred) or legacy ?token= into sessionStorage and scrub the URL. */
export function captureWallboardTokenFromUrl(): void {
  try {
    // Preferred: URL fragment — it never reaches the server, so the
    // long-lived display credential can't end up in access logs.
    const rawHash = window.location.hash.replace(/^#/, '');
    const hashParams = new URLSearchParams(rawHash);
    const hashToken = hashParams.get('token');

    // Legacy fallback: ?token= on already-printed URLs (scrubbed too).
    const queryParams = new URLSearchParams(window.location.search);
    const queryToken = queryParams.get('token');

    const token = hashToken || queryToken;
    if (!token) return;

    sessionStorage.setItem(STORAGE_KEY, token);

    if (hashToken) hashParams.delete('token');
    if (queryToken) queryParams.delete('token');
    const query = queryParams.toString();
    // Leave the hash untouched unless the token was in it.
    const newHash = hashToken ? hashParams.toString() : rawHash;
    const cleaned = `${window.location.pathname}${query ? `?${query}` : ''}${newHash ? `#${newHash}` : ''}`;
    window.history.replaceState(null, '', cleaned);
  } catch {
    // sessionStorage unavailable (rare kiosk configs) — fall through; the
    // fetch will fail with 401 and the page shows the "no token" guidance.
  }
}

/**
 * Persist a display token claimed via a /tv setup code. localStorage, not
 * sessionStorage, so the TV survives reboots without re-pairing (see the
 * header comment for why that's acceptable for this credential).
 */
export function persistWallboardToken(token: string, dept?: string | null): void {
  try {
    localStorage.setItem(PERSIST_KEY, token);
    if (dept) {
      localStorage.setItem(DEPT_KEY, dept);
    } else {
      localStorage.removeItem(DEPT_KEY);
    }
  } catch {
    // Storage unavailable — the claim response is still used in-memory by the
    // caller for this page load; the TV just re-pairs after a reboot.
  }
}

/**
 * Drop every stored display credential (e.g. after the server rejects it as
 * revoked/expired) — the sessionStorage capture AND the localStorage
 * persisted claim + its dept. NEVER the logged-in user's session token.
 */
export function clearWallboardToken(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(PERSIST_KEY);
    localStorage.removeItem(DEPT_KEY);
  } catch {
    // Storage unavailable — nothing to clear.
  }
}

/**
 * The stored DISPLAY token only (URL-captured or setup-code-claimed) —
 * explicitly excludes the logged-in user's session token. /tv uses this to
 * decide "already paired → go straight to /wallboard" without treating a
 * signed-in admin's browser as a paired TV.
 */
export function getStoredDisplayToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || localStorage.getItem(PERSIST_KEY);
  } catch {
    return null;
  }
}

/** The dept stored alongside a claimed display token (null when unpinned). */
export function getPersistedWallboardDept(): string | null {
  try {
    return localStorage.getItem(DEPT_KEY);
  } catch {
    return null;
  }
}

export function getWallboardToken(): string | null {
  try {
    return getStoredDisplayToken() || sessionStorage.getItem('token');
  } catch {
    return null;
  }
}

/** Uppercase and strip whitespace + dash variants (incl. smart-punctuation
 * en/em dashes a TV keyboard may emit) — codes are case-insensitive and shown
 * grouped XXXX-XXXX. Mirrors the backend's `_normalize_setup_code`. */
export function normalizeSetupCode(raw: string): string {
  return raw.replace(/[\s‐-―-]/g, '').toUpperCase();
}

/**
 * Claim a one-time setup code for a display token (POST /auth/display-token/claim).
 * PUBLIC endpoint — no auth header. Throws:
 *  - Error('NETWORK') when the server can't be reached at all;
 *  - Error('CLAIM_REJECTED') on any non-OK response (the server answers a
 *    generic 404 for expired/used/unknown codes — indistinguishable by design).
 */
export async function claimDisplayCode(code: string): Promise<DisplayCodeClaim> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/auth/display-token/claim`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({ code: normalizeSetupCode(code) }),
    });
  } catch {
    throw new Error('NETWORK');
  }
  if (!response.ok) {
    throw new Error('CLAIM_REJECTED');
  }
  return (await response.json()) as DisplayCodeClaim;
}

export async function fetchWallboard(dept?: string | null): Promise<WallboardResponse> {
  const token = getWallboardToken();
  if (!token) {
    throw new Error('NO_TOKEN');
  }
  const query = dept ? `?dept=${encodeURIComponent(dept)}` : '';
  const response = await fetch(`${API_BASE_URL}/shop-floor/wallboard${query}`, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${token}`,
      'X-Requested-With': 'XMLHttpRequest',
    },
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error('UNAUTHORIZED');
  }
  if (!response.ok) {
    throw new Error(`HTTP_${response.status}`);
  }
  return (await response.json()) as WallboardResponse;
}

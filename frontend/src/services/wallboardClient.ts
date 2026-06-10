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
 *
 * Token resolution order:
 *  1. `#token=<display-jwt>` URL FRAGMENT (preferred — fragments never leave
 *     the browser, so the 365-day credential can't land in server access
 *     logs). Persisted to sessionStorage and scrubbed from the address bar.
 *  2. `?token=<display-jwt>` query param — legacy fallback for already-printed
 *     URLs only; new links use the fragment. Also scrubbed.
 *  3. sessionStorage 'wallboard_token' (a previously provided display token).
 *  4. sessionStorage 'token' (a logged-in user's access token — the endpoint
 *     accepts either).
 */

import type { WallboardResponse } from '../types/wallboard';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
const STORAGE_KEY = 'wallboard_token';

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

/** Drop the stored display token (e.g. after the server rejects it as revoked/expired). */
export function clearWallboardToken(): void {
  try {
    // Only the display token — never the logged-in user's session token.
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // sessionStorage unavailable — nothing to clear.
  }
}

export function getWallboardToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || sessionStorage.getItem('token');
  } catch {
    return null;
  }
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

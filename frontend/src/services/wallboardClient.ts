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
 *  1. `?token=<display-jwt>` URL param (then persisted to sessionStorage and
 *     stripped from the address bar so it doesn't linger in screenshots).
 *  2. sessionStorage 'wallboard_token' (a previously provided display token).
 *  3. sessionStorage 'token' (a logged-in user's access token — the endpoint
 *     accepts either).
 */

import type { WallboardResponse } from '../types/wallboard';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
const STORAGE_KEY = 'wallboard_token';

/** Capture ?token= from the URL into sessionStorage and scrub the URL. */
export function captureWallboardTokenFromUrl(): void {
  try {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (token) {
      sessionStorage.setItem(STORAGE_KEY, token);
      params.delete('token');
      const query = params.toString();
      const cleaned = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
      window.history.replaceState(null, '', cleaned);
    }
  } catch {
    // sessionStorage unavailable (rare kiosk configs) — fall through; the
    // fetch will fail with 401 and the page shows the "no token" guidance.
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

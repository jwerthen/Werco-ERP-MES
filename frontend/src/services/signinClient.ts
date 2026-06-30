/**
 * Dedicated fetch helper for the visitor sign-in tablet.
 *
 * DELIBERATELY ISOLATED from services/api.ts (a wallboardClient.ts twin):
 *  - The station token is a scoped `type="signin"` credential that ONLY the two
 *    visitor write endpoints (sign-in, sign-out) accept. It must NEVER enter the
 *    global axios instance — that client's 401 interceptor force-redirects to
 *    /login, which is fatal on an unattended lobby tablet.
 *  - The token is attached ONLY to the calls made through this helper.
 *
 * Unlike the wallboard (whose token arrives as a one-time URL fragment), the
 * station token is MINTED by PIN: the tablet POSTs {station_id, pin} to
 * /station-login and receives a 24h token, which we hold in sessionStorage.
 * There is no token-in-URL capture step here.
 */

import type {
  StationLoginResponse,
  VisitorLogResponse,
  VisitorSignInRequest,
  VisitorSignOutRequest,
} from '../types/visitor';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
const STORAGE_KEY = 'visitor_signin_token';

/** A sign-in/sign-out call that the server actively refused (401/403/404/409). */
export class SigninApiError extends Error {
  status: number;
  /** Parsed `detail` from the JSON body (string, object, or null). */
  detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = 'SigninApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** The current station token, if a PIN session is active. */
export function getStationToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Persist the minted station token (called after a successful /station-login). */
export function setStationToken(token: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, token);
  } catch {
    // sessionStorage unavailable (rare tablet configs) — the session simply
    // won't persist across reloads; writes still work for this page life.
  }
}

/** Drop the station token — "Lock station" or a server rejection (revoked/expired). */
export function clearStationToken(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing to clear
  }
}

/**
 * Pull `detail` from a non-OK JSON response. Returns `{ detail, message }` where
 * `message` is a human string (string detail verbatim, else JSON-stringified,
 * else an HTTP fallback) — surfaced verbatim to the operator per the kiosk rule.
 */
async function readError(response: Response): Promise<{ detail: unknown; message: string }> {
  let detail: unknown = null;
  try {
    const body = await response.json();
    detail = (body as { detail?: unknown })?.detail ?? body;
  } catch {
    // non-JSON body — fall through to the HTTP fallback
  }
  if (typeof detail === 'string' && detail.trim()) {
    return { detail, message: detail };
  }
  if (detail && typeof detail === 'object') {
    const maybeMessage = (detail as { message?: unknown }).message;
    if (typeof maybeMessage === 'string' && maybeMessage.trim()) {
      return { detail, message: maybeMessage };
    }
    try {
      return { detail, message: JSON.stringify(detail) };
    } catch {
      // fall through
    }
  }
  return { detail, message: `Request failed (HTTP ${response.status}).` };
}

const JSON_HEADERS = {
  'Content-Type': 'application/json',
  // CSRF defense: a custom header cross-origin requests cannot set.
  'X-Requested-With': 'XMLHttpRequest',
};

function authHeaders(): Record<string, string> {
  const token = getStationToken();
  return token ? { ...JSON_HEADERS, Authorization: `Bearer ${token}` } : { ...JSON_HEADERS };
}

/**
 * POST /station-login — PUBLIC. Verifies the shared PIN against the station and,
 * on success, mints + stores the 24h `type="signin"` token. Throws SigninApiError
 * on a bad station/PIN (401) so the keypad can show the rejection.
 */
export async function stationLogin(stationId: number, pin: string): Promise<StationLoginResponse> {
  const response = await fetch(`${API_BASE_URL}/visitor-logs/station-login`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ station_id: stationId, pin }),
  });
  if (!response.ok) {
    const { detail, message } = await readError(response);
    throw new SigninApiError(response.status, detail, message);
  }
  const data = (await response.json()) as StationLoginResponse;
  setStationToken(data.token);
  return data;
}

/** POST /sign-in — auth: station token. → 201 VisitorLogResponse. */
export async function postSignIn(payload: VisitorSignInRequest): Promise<VisitorLogResponse> {
  const response = await fetch(`${API_BASE_URL}/visitor-logs/sign-in`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const { detail, message } = await readError(response);
    throw new SigninApiError(response.status, detail, message);
  }
  return (await response.json()) as VisitorLogResponse;
}

/**
 * POST /sign-out — auth: station token. Body is one of {visitor_log_id} OR {name}.
 * A 409 (name ambiguity) carries `detail.matches` for a picker; callers should
 * read `err.detail` on a 409 and re-POST with {visitor_log_id}.
 */
export async function postSignOut(payload: VisitorSignOutRequest): Promise<VisitorLogResponse> {
  const response = await fetch(`${API_BASE_URL}/visitor-logs/sign-out`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const { detail, message } = await readError(response);
    throw new SigninApiError(response.status, detail, message);
  }
  return (await response.json()) as VisitorLogResponse;
}

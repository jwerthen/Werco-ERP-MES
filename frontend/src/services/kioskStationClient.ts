/**
 * Dedicated fetch helper for the crew-station kiosk (multi-operator terminal).
 *
 * DELIBERATELY ISOLATED from services/api.ts (a signinClient.ts twin) — this is
 * the ONLY module that touches station or operator tokens:
 *  - The STATION token is a scoped `type="kiosk"` credential (24h, PIN-minted
 *    via /shop-floor/kiosk-stations/station-login) that ONLY the roster queue
 *    read and the badge-token mint accept. It must NEVER enter the global axios
 *    instance — that client's 401 interceptor force-redirects to /login, which
 *    is fatal on an unattended shop terminal. It lives in sessionStorage under
 *    its own key.
 *  - OPERATOR tokens are 5-minute `scope="kiosk"` access tokens minted per
 *    badge scan (no refresh token). They live IN MEMORY ONLY — passed
 *    explicitly to each labor mutation as an Authorization header and never
 *    persisted anywhere.
 *  - A 401 on a STATION-authed read means the station is revoked/expired: we
 *    clear the stored token so the page falls back to the PIN screen (never a
 *    /login redirect). A 401 from the badge mint is NOT treated as a dead
 *    station — the backend returns a uniform 401 for an unknown/invalid badge,
 *    so clearing there would lock the whole station on one mistyped badge; if
 *    the station token really is dead the next 10s queue poll 401s and locks.
 */

import type {
  KioskBadgeTokenResponse,
  KioskClosedTimeEntry,
  KioskCrewQueueResponse,
  KioskStationLoginResponse,
  KioskStationSummary,
} from '../types/kioskStation';
import type {
  OperationStepRecord,
  OperationStepRecordInput,
  OperationStepSupersedeInput,
  OperationStepsView,
  QualityHoldInput,
  QualityHoldResult,
  StepAttachmentResult,
} from '../types/processSheet';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
const STORAGE_KEY = 'kiosk_station_token';
// Station identity (id/label/work-center) cached beside the token so a page
// reload can resume the crew board without re-entering the PIN. Not a secret.
const STATION_INFO_KEY = 'kiosk_station_info';

/** A kiosk call the server actively refused (400/401/403/404/409). */
export class KioskApiError extends Error {
  status: number;
  /** Parsed `detail` from the JSON body (string, object, or null). */
  detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    // Restore the prototype chain so `instanceof KioskApiError` holds even
    // when the class is transpiled to ES5 (ts-jest) — the well-known
    // extends-Error caveat. The 401→PIN-screen routing depends on it.
    Object.setPrototypeOf(this, KioskApiError.prototype);
    this.name = 'KioskApiError';
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

/** Persist the minted station token (called after a successful station-login). */
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
    sessionStorage.removeItem(STATION_INFO_KEY);
  } catch {
    // nothing to clear
  }
}

/** The station identity cached at PIN login (survives reloads with the token). */
export function getStoredStation(): KioskStationSummary | null {
  try {
    const raw = sessionStorage.getItem(STATION_INFO_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as KioskStationSummary;
    return typeof parsed?.id === 'number' && typeof parsed?.work_center_id === 'number' ? parsed : null;
  } catch {
    return null;
  }
}

function setStoredStation(station: KioskStationSummary): void {
  try {
    sessionStorage.setItem(STATION_INFO_KEY, JSON.stringify(station));
  } catch {
    // best effort — the PIN screen simply reappears after a reload
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

function bearerHeaders(token: string | null): Record<string, string> {
  return token ? { ...JSON_HEADERS, Authorization: `Bearer ${token}` } : { ...JSON_HEADERS };
}

async function throwOnError(response: Response): Promise<never> {
  const { detail, message } = await readError(response);
  throw new KioskApiError(response.status, detail, message);
}

// ---------------------------------------------------------------------------
// Station tier
// ---------------------------------------------------------------------------

/**
 * POST /shop-floor/kiosk-stations/station-login — PUBLIC, rate-limited.
 * Verifies the shared PIN against the station and, on success, mints + stores
 * the 24h `type="kiosk"` station token. Throws KioskApiError on a bad
 * station/PIN (401) so the keypad can show the rejection.
 */
export async function stationLogin(stationId: number, pin: string): Promise<KioskStationLoginResponse> {
  const response = await fetch(`${API_BASE_URL}/shop-floor/kiosk-stations/station-login`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ station_id: stationId, pin }),
  });
  if (!response.ok) await throwOnError(response);
  const data = (await response.json()) as KioskStationLoginResponse;
  setStationToken(data.access_token);
  if (data.station) setStoredStation(data.station);
  return data;
}

/**
 * GET /shop-floor/work-center-queue/{id} — auth: station token. Returns the
 * queue enriched with per-item rosters + `server_time` + station identity.
 * A 401 clears the stored token (revoked/expired station → PIN screen).
 */
export async function getQueue(workCenterId: number): Promise<KioskCrewQueueResponse> {
  const response = await fetch(`${API_BASE_URL}/shop-floor/work-center-queue/${workCenterId}`, {
    method: 'GET',
    headers: bearerHeaders(getStationToken()),
  });
  if (!response.ok) {
    if (response.status === 401) clearStationToken();
    await throwOnError(response);
  }
  return (await response.json()) as KioskCrewQueueResponse;
}

/**
 * POST /auth/kiosk-badge-token — auth: station token. Exchanges a badge scan
 * for a 5-minute `scope="kiosk"` operator access token (memory only). A 401
 * here is a bad badge (uniform server message) — it does NOT lock the station.
 */
export async function mintBadgeToken(employeeId: string): Promise<KioskBadgeTokenResponse> {
  const response = await fetch(`${API_BASE_URL}/auth/kiosk-badge-token`, {
    method: 'POST',
    headers: bearerHeaders(getStationToken()),
    body: JSON.stringify({ employee_id: employeeId }),
  });
  if (!response.ok) await throwOnError(response);
  return (await response.json()) as KioskBadgeTokenResponse;
}

// ---------------------------------------------------------------------------
// Operator tier — the EXISTING shop-floor endpoints, called with the freshly
// minted operator token as an explicit Authorization header. The operator IS
// current_user server-side, so audit attribution and gating work unchanged.
// Every mutation body must carry source:"kiosk" (A0.1 adoption telemetry).
// ---------------------------------------------------------------------------

async function operatorFetch<T>(operatorToken: string, method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: bearerHeaders(operatorToken),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) await throwOnError(response);
  return (await response.json()) as T;
}

/** GET /shop-floor/my-active-job as the badge-identified operator. */
export async function getMyActiveJob(operatorToken: string): Promise<{
  active_jobs?: Array<{
    time_entry_id: number;
    operation_id?: number;
    work_order_number?: string;
    work_center_name?: string;
    operation_name?: string;
    clock_in?: string;
  }>;
}> {
  return operatorFetch(operatorToken, 'GET', '/shop-floor/my-active-job');
}

/** POST /shop-floor/clock-in — JOIN a job as the badge-identified operator. */
export async function clockIn(
  operatorToken: string,
  data: { work_order_id: number; operation_id: number; work_center_id: number; entry_type: string; source: string }
): Promise<unknown> {
  return operatorFetch(operatorToken, 'POST', '/shop-floor/clock-in', data);
}

/** POST /shop-floor/clock-out/{time_entry_id} — LEAVE (close own entry). */
export async function clockOut(
  operatorToken: string,
  timeEntryId: number,
  data: { quantity_produced: number; quantity_scrapped?: number; scrap_reason?: string; source: string }
): Promise<unknown> {
  return operatorFetch(operatorToken, 'POST', `/shop-floor/clock-out/${timeEntryId}`, data);
}

/** POST /shop-floor/operations/{id}/production — additive quantity report. */
export async function reportProduction(
  operatorToken: string,
  operationId: number,
  data: {
    quantity_complete_delta?: number;
    quantity_scrapped_delta?: number;
    scrap_reason?: string;
    source: string;
  }
): Promise<unknown> {
  return operatorFetch(operatorToken, 'POST', `/shop-floor/operations/${operationId}/production`, data);
}

/**
 * POST /shop-floor/operations/{id}/complete — auto-closes ALL operators' open
 * entries; the response's `closed_time_entries` names who was clocked out.
 */
export async function completeOperation(
  operatorToken: string,
  operationId: number,
  data: { quantity_complete: number; source: string }
): Promise<{ closed_time_entries?: KioskClosedTimeEntry[] }> {
  return operatorFetch(operatorToken, 'POST', `/shop-floor/operations/${operationId}/complete`, data);
}

/** PUT /shop-floor/operations/{id}/hold — files the structured blocker. */
export async function holdOperation(
  operatorToken: string,
  operationId: number,
  data: { category: string; severity: string; note?: string; source: string }
): Promise<unknown> {
  return operatorFetch(operatorToken, 'PUT', `/shop-floor/operations/${operationId}/hold`, data);
}

// ---------------------------------------------------------------------------
// Process Sheets capture (PR 3/4) — snapshot steps + append-only step records.
// All paths live under /shop-floor on purpose: the badge-minted operator
// token is path-fenced there, so reads AND writes use the OPERATOR token (the
// station token is honored only by the queue read + badge mint). Crew-station
// calls never send `source` — a kiosk-scoped badge token is authoritative and
// the server records "kiosk" regardless of any hint.
// ---------------------------------------------------------------------------

/** GET /shop-floor/operations/{id}/steps — snapshot steps + live records + completeness. */
export async function getOperationSteps(operatorToken: string, operationId: number): Promise<OperationStepsView> {
  return operatorFetch(operatorToken, 'GET', `/shop-floor/operations/${operationId}/steps`);
}

/** POST .../steps/{step_id}/records — capture ONE type-shaped value (409 OUT_OF_TOLERANCE = no row). */
export async function recordOperationStep(
  operatorToken: string,
  operationId: number,
  stepId: number,
  data: OperationStepRecordInput
): Promise<OperationStepRecord> {
  return operatorFetch(operatorToken, 'POST', `/shop-floor/operations/${operationId}/steps/${stepId}/records`, data);
}

/** POST .../records/{record_id}/supersede — correction path (reason required; already-corrected 409s). */
export async function supersedeOperationStepRecord(
  operatorToken: string,
  operationId: number,
  stepId: number,
  recordId: number,
  data: OperationStepSupersedeInput
): Promise<OperationStepRecord> {
  return operatorFetch(
    operatorToken,
    'POST',
    `/shop-floor/operations/${operationId}/steps/${stepId}/records/${recordId}/supersede`,
    data
  );
}

/**
 * POST .../steps/{step_id}/quality-hold — the one-tap OOT escape hatch (PR 4).
 * Atomically files an IN_PROCESS NCR + QUALITY_HOLD blocker, flips the
 * operation ON_HOLD, and closes open time entries. The refused measurement
 * lands on the NCR — it is never stored as a step record.
 */
export async function raiseStepQualityHold(
  operatorToken: string,
  operationId: number,
  stepId: number,
  data: QualityHoldInput
): Promise<QualityHoldResult> {
  return operatorFetch(
    operatorToken,
    'POST',
    `/shop-floor/operations/${operationId}/steps/${stepId}/quality-hold`,
    data
  );
}

/**
 * POST .../steps/{step_id}/attachment — PHOTO/FILE evidence upload (multipart).
 * This is the ONLY evidence path the kiosk may use: /documents/upload is
 * outside the operator-token fence (403). Upload first, then create the record
 * with the returned document_id as attachment_document_id. No Content-Type
 * header here — the browser sets the multipart boundary itself.
 */
export async function uploadOperationStepAttachment(
  operatorToken: string,
  operationId: number,
  stepId: number,
  file: File
): Promise<StepAttachmentResult> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(
    `${API_BASE_URL}/shop-floor/operations/${operationId}/steps/${stepId}/attachment`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${operatorToken}`,
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: formData,
    }
  );
  if (!response.ok) await throwOnError(response);
  return (await response.json()) as StepAttachmentResult;
}

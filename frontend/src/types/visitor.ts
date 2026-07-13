/**
 * Types for the visitor sign-in tablet feature (all under /api/v1/visitor-logs).
 *
 * Two surfaces share these contracts:
 *  - the standalone tablet (VisitorSignIn.tsx) via the isolated `signinClient.ts`
 *    fetch helper (PIN-minted `type="signin"` token, sessionStorage-only), and
 *  - the authenticated admin page (VisitorLog.tsx) via the normal `api` client.
 *
 * Visitor & host names are CUI/PII — they never cross an external boundary.
 */

/** purpose ∈ meeting|delivery|contractor|interview|audit|other */
export type VisitorPurpose = 'meeting' | 'delivery' | 'contractor' | 'interview' | 'audit' | 'other';

/** status ∈ signed_in|signed_out */
export type VisitorStatus = 'signed_in' | 'signed_out';

/** A single visitor-log row (sign-in or sign-out response, and list rows). */
export interface VisitorLogResponse {
  id: number;
  visitor_name: string;
  visitor_company: string | null;
  visitor_phone: string | null;
  host_name: string | null;
  host_user_id: number | null;
  purpose: VisitorPurpose;
  purpose_note: string | null;
  safety_acknowledged: boolean;
  status: VisitorStatus;
  signed_in_at: string;
  signed_out_at: string | null;
  signin_station_id: number | null;
  station_label: string | null;
  /**
   * Non-null iff this row was back-entered by staff after the fact (via the
   * admin "Add visit" flow), rather than captured live at the lobby tablet.
   * Lets the log badge staff-entered rows.
   */
  entered_by_user_id: number | null;
}

export interface VisitorLogListResponse {
  items: VisitorLogResponse[];
  total: number;
}

/** Body for POST /sign-in (auth: signin station token OR staff token). */
export interface VisitorSignInRequest {
  visitor_name: string;
  visitor_company?: string | null;
  visitor_phone?: string | null;
  host_name?: string | null;
  purpose: VisitorPurpose;
  /** Required when purpose === 'other'. */
  purpose_note?: string | null;
  /** The safety/NDA acknowledgment checkbox — must be true to submit. */
  safety_acknowledged: boolean;
}

/**
 * Body for POST /manual — staff back-entry of an offline visit with its ACTUAL
 * past times (ADMIN/MANAGER; NOT the station token). Times are UTC ISO-8601 with
 * a trailing 'Z'. Server enforces: signed_in_at required + in the past;
 * signed_out_at (if given) on/after signed_in_at + in the past; purpose_note
 * required when purpose === 'other'; safety_acknowledged must be true.
 */
export interface VisitorManualEntryRequest {
  visitor_name: string;
  visitor_company?: string | null;
  host_name?: string | null;
  purpose: VisitorPurpose;
  purpose_note?: string | null;
  safety_acknowledged: boolean;
  /** Actual sign-in time (UTC ISO 'Z', must be in the past). */
  signed_in_at: string;
  /** Actual sign-out time (UTC ISO 'Z'); omit if still on-site. */
  signed_out_at?: string | null;
}

/**
 * Body for POST /sign-out. Exactly one of `visitor_log_id` or `name` is required.
 * The name path can 409 with a disambiguation picker (see VisitorSignOut409).
 */
export interface VisitorSignOutRequest {
  visitor_log_id?: number;
  name?: string;
}

/** A single candidate in a sign-out name-ambiguity (409) picker. */
export interface VisitorSignOutMatch {
  id: number;
  visitor_company: string | null;
  signed_in_at: string;
}

/** 409 body returned by POST /sign-out when a name matches >1 open visit. */
export interface VisitorSignOut409 {
  message: string;
  matches: VisitorSignOutMatch[];
}

// ---- Station management (admin) ----

/** Body for POST /station-login (PUBLIC, rate-limited). pin = 4–8 digits. */
export interface StationLoginRequest {
  station_id: number;
  pin: string;
}

/** Response from POST /station-login. token is a `type="signin"` JWT (24h). */
export interface StationLoginResponse {
  token: string;
  station_label: string;
  /** Token TTL in seconds. */
  expires_in: number;
}

/** A sign-in station record (PIN never echoed). */
export interface SigninStationResponse {
  id: number;
  label: string;
  revoked: boolean;
  revoked_at: string | null;
  revoked_by: number | null;
  last_used_at: string | null;
  created_by: number | null;
  created_at: string;
}

export interface SigninStationListResponse {
  stations: SigninStationResponse[];
}

/** Body for POST /stations. */
export interface SigninStationCreate {
  label: string;
  pin: string;
}

/** Body for POST /stations/{id}/reset-pin. */
export interface StationResetPinRequest {
  pin: string;
}

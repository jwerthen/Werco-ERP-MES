/**
 * Types for the crew-station kiosk (shared multi-operator work-center terminal).
 *
 * Two surfaces share these contracts:
 *  - the standalone crew kiosk (CrewStationKiosk.tsx) via the isolated
 *    `kioskStationClient.ts` fetch helper (PIN-minted `type="kiosk"` station
 *    token, sessionStorage-only; badge-minted 5-minute operator tokens held in
 *    memory only), and
 *  - the authenticated admin management modal (opened from Work Centers) via
 *    the normal `api` client (list/create/reset-PIN/revoke).
 */

import type { KioskCrewQueueItem } from '../components/kiosk/kioskConstants';
import type { KioskQueueWorkCenter } from './index';
import type { ScrapReasonCodeOption } from './scrapReason';

// ---- Station tier (PIN → 24h scoped station token) ----

/** Body for POST /shop-floor/kiosk-stations/station-login (PUBLIC, rate-limited). */
export interface KioskStationLoginRequest {
  station_id: number;
  pin: string;
}

/** The station identity echoed by station-login and the queue read. */
export interface KioskStationSummary {
  id: number;
  label: string;
  work_center_id: number;
  work_center_code: string | null;
  work_center_name: string | null;
}

/** Response from POST /shop-floor/kiosk-stations/station-login. */
export interface KioskStationLoginResponse {
  /** Scoped `type="kiosk"` JWT (24h) — honored ONLY by the roster queue read and the badge mint. */
  access_token: string;
  station: KioskStationSummary;
}

/**
 * Response from GET /shop-floor/work-center-queue/{id} when called with a
 * station token: the existing queue rows enriched with per-item rosters, plus
 * `server_time` (timer skew correction) and the station identity.
 */
export interface KioskCrewQueueResponse {
  queue: KioskCrewQueueItem[];
  /** UTC ISO server clock at response time — anchor for honest elapsed timers. */
  server_time: string;
  station: KioskStationSummary;
  /**
   * ACTIVE scrap reason codes for the crew-station scrap picker (Lean Phase 1).
   * Rides this station-authed read because the kiosk's scoped tokens cannot
   * reach GET /quality/scrap-reason-codes (path fence). display_order-then-code
   * sorted server-side; [] = tenant has no codes -> legacy SCRAP_REASONS
   * fallback. Optional so a pre-Lean backend payload cannot crash the board.
   */
  scrap_reason_codes?: ScrapReasonCodeOption[];
  /**
   * The queue's work center (Kiosk Foundry Redesign, backend B3) — feeds the
   * kiosk top bar. Optional so pre-redesign payloads still typecheck.
   */
  work_center?: KioskQueueWorkCenter | null;
}

// ---- Operator tier (badge → 5-minute scope:"kiosk" access token) ----

/** Response from POST /auth/kiosk-badge-token (station-token-gated). */
export interface KioskBadgeTokenResponse {
  /** 5-minute `scope="kiosk"` access token — memory only, NEVER persisted. No refresh token. */
  access_token: string;
  user: {
    id: number;
    full_name: string;
    employee_id: string | null;
  };
}

/** Per-operator entry closed by POST /shop-floor/operations/{id}/complete. */
export interface KioskClosedTimeEntry {
  time_entry_id: number;
  user_id: number;
  /** Server emits null when the entry's user record is missing. */
  operator_name: string | null;
}

// ---- Admin management (authed app, normal api client) ----

/** A kiosk station record (PIN never echoed). */
export interface KioskStationResponse {
  id: number;
  label: string;
  work_center_id: number;
  work_center_code: string | null;
  work_center_name: string | null;
  revoked: boolean;
  revoked_at: string | null;
  revoked_by: number | null;
  last_used_at: string | null;
  created_by: number | null;
  created_at: string;
}

export interface KioskStationListResponse {
  stations: KioskStationResponse[];
}

/** Body for POST /shop-floor/kiosk-stations. pin = 4–8 digits. */
export interface KioskStationCreate {
  label: string;
  pin: string;
  work_center_id: number;
}

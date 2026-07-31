/**
 * Notification inbox + catalog types.
 *
 * These mirror the backend response contracts in
 * `backend/app/schemas/notification.py` (NotificationResponse,
 * NotificationListResponse, UnreadCountResponse, CatalogEntryResponse) and the
 * shared `PaginationMeta` from `backend/app/core/pagination.py`. Datetimes arrive
 * as UTC ISO-8601 with a trailing `Z` and are rendered in shop-local Central time.
 */

export type NotificationSeverity = 'info' | 'warning' | 'critical';

/** One in-app inbox row for the current user (matches `NotificationResponse`). */
export interface NotificationItem {
  id: number;
  event_key: string;
  severity: string; // one of NotificationSeverity, kept loose for forward-compat
  title: string;
  body?: string | null;
  /** Relative in-app route this notification deep-links to (e.g. `/work-orders/42`). */
  link?: string | null;
  related_type?: string | null;
  related_id?: number | null;
  is_read: boolean;
  read_at?: string | null;
  created_at: string;
}

/** Offset/limit pagination metadata (matches backend `PaginationMeta`). */
export interface PaginationMeta {
  page: number;
  page_size: number;
  total_count: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
}

/** Paged inbox response (matches `NotificationListResponse`). */
export interface NotificationListResponse {
  items: NotificationItem[];
  pagination: PaginationMeta;
}

/** Query params for the paged inbox endpoint. */
export interface NotificationListParams {
  page?: number;
  pageSize?: number;
  /** `true` = only unread, `false` = only read, omit = all. */
  unread?: boolean;
  category?: string;
  severity?: string;
}

/** One catalog entry driving the settings matrix (matches `CatalogEntryResponse`). */
export interface NotificationCatalogEntry {
  event_key: string;
  label: string;
  description: string;
  category: string;
  severity: string;
  default_channels: string[];
  mandatory_channel?: string | null;
  sms_eligible: boolean;
}

/** Delivery channels a per-event preference can carry. */
export type NotificationChannel = 'in_app' | 'email' | 'sms' | 'digest';

/**
 * Per-event channel flags, keyed by channel. Read from the RESOLVED map the
 * preferences endpoint returns (catalog defaults already applied and any mandatory
 * channel already forced on), so a channel key may legitimately be absent for a
 * channel the catalog does not describe.
 */
export type NotificationEventPreference = Partial<Record<NotificationChannel, boolean>>;

/**
 * The current user's effective notification preferences — matches
 * `NotificationPreferencesResponse` (`GET`/`PUT /users/me/notification-preferences`).
 *
 * `preferences` is the resolved per-event channel map the dispatcher would apply
 * right now, NOT the raw stored row, so the UI can never disagree with what
 * actually gets sent. The three SMS flags exist so the UI can explain exactly why
 * an SMS toggle would currently be inert (no number / company egress off / provider
 * not configured). `sms_configured` is a plain boolean — no provider credential is
 * ever returned to the client.
 */
export interface NotificationPreferences {
  preferences: Record<string, NotificationEventPreference>;
  has_saved_preferences: boolean;
  phone?: string | null;
  sms_egress_enabled: boolean;
  sms_configured: boolean;
}

/**
 * Body of `PUT /users/me/notification-preferences`. The backend declares
 * `extra="forbid"` and PR 4 owns the SMS channel only, so each entry carries
 * exactly `{ sms }` — send only the events you are changing.
 */
export interface NotificationPreferencesUpdate {
  preferences: Record<string, { sms: boolean }>;
}

/**
 * Result of the self-service "send test SMS" action (`TestSMSResponse`). Only
 * `detail` is meant for display; `sid` / `provider_status` are provider
 * bookkeeping and are deliberately NOT surfaced in the UI.
 */
export interface TestSmsResponse {
  status: string;
  sid?: string | null;
  provider_status?: string | null;
  detail: string;
}

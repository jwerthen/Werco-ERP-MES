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

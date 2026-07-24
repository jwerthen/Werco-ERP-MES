/**
 * Notification severity → status-badge coloring + relative time.
 *
 * Notification severities (`info | warning | critical`) are a different
 * vocabulary than the domain status strings in `statusColors.ts`, so they map
 * onto the same 5 canonical instrument-panel variants here (shared by the bell
 * popover and the /notifications page so severity looks identical in both).
 */

import { variantClass, StatusVariant } from './statusColors';
import { formatCentralDate, toDate } from './centralTime';
import { NotificationSeverity } from '../types/notification';

/** Canonical status-badge variant for each notification severity. */
export const severityVariant: Record<NotificationSeverity, StatusVariant> = {
  info: 'blue',
  warning: 'amber',
  critical: 'red',
};

/**
 * `StatusBadge` `colorMap` keyed by severity string — pass alongside
 * `status={n.severity}` so the badge colors by severity rather than falling
 * back to the neutral slate default (severities aren't in the status map).
 */
export const SEVERITY_COLOR_MAP: Record<string, string> = {
  info: variantClass[severityVariant.info],
  warning: variantClass[severityVariant.warning],
  critical: variantClass[severityVariant.critical],
};

/**
 * Shop-local relative time for recent notifications, falling back to an
 * absolute Central-time date for older rows. Never uses `toLocaleString()`
 * (which renders in the viewer's zone and mis-parses zone-less strings).
 *
 * The wire value is an absolute instant (UTC `Z`), so the relative delta is
 * timezone-agnostic and correct regardless of where it renders.
 */
export function formatNotificationTime(value?: string | null): string {
  if (!value) return '';
  const parsed = toDate(value);
  if (!parsed) return '';
  const diffMs = Date.now() - parsed.getTime();
  const sec = Math.round(diffMs / 1000);
  if (sec < 45) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.round(hr / 24);
  if (days < 7) return `${days}d ago`;
  // Older than a week: fall back to an absolute Central-time date.
  return formatCentralDate(value);
}

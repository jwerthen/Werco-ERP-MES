/**
 * statusColors — the single source of truth for status-badge coloring.
 *
 * Across ~19 pages each screen previously declared its own status->class map,
 * and they DISAGREED (e.g. `in_progress` was blue on WorkOrderDetail, amber on
 * ShopFloorSimple/Maintenance, yellow on Customers). This module resolves every
 * status string used in the app to ONE canonical semantic variant so a given
 * status looks identical everywhere.
 *
 * Canonical semantic rule (applied to resolve the disagreements):
 *   green  -> done / good terminal: completed, released, active, approved, passed,
 *             accepted, closed-good, available, delivered, compliant, use_as_is...
 *   blue   -> live / in-flight good: in_progress, ready, sent, in_use, in_transit...
 *   amber  -> waiting / not-yet-started: pending, draft, on_hold, scheduled, due,
 *             partial, under_review, awaiting...
 *   red    -> bad / needs attention: failed, rejected, overdue, scrapped, cancelled,
 *             blocked, open (defect), non_compliant, lost, needs_repair...
 *   slate  -> dormant / neutral terminal: obsolete, inactive, void, retired, closed,
 *             expired, skipped, not_assessed...
 *
 * Notable resolution: `in_progress` is canonically BLUE (live work in flight).
 *
 * Classes are bg-/text- pairs in the instrument-panel style (semi-transparent
 * fill + bright text), consistent with the existing badges.
 */

export type StatusVariant = 'green' | 'blue' | 'amber' | 'red' | 'slate';

/** The bg / text class pair for each canonical variant (instrument-panel style). */
export const variantClass: Record<StatusVariant, string> = {
  green: 'bg-green-500/20 text-emerald-300',
  blue: 'bg-blue-500/20 text-blue-300',
  amber: 'bg-amber-500/20 text-amber-300',
  red: 'bg-red-500/20 text-red-300',
  slate: 'bg-slate-800/50 text-slate-400',
};

/**
 * Canonical variant for every status string found across the app's pages.
 * Keys are the raw status values (snake_case) as they arrive from the API.
 */
const statusVariantMap: Record<string, StatusVariant> = {
  confirmed: 'green',
  majority: 'amber',
  review: 'red',

  // --- green: good / done / active-good ---
  active: 'green',
  released: 'green',
  approved: 'green',
  completed: 'green',
  complete: 'green',
  passed: 'green',
  accepted: 'green',
  converted: 'green',
  available: 'green',
  received: 'green',
  delivered: 'green',
  shipped: 'green',
  compliant: 'green',
  use_as_is: 'green',
  ok: 'green',
  won: 'green',

  // --- blue: in-flight good / live work ---
  in_progress: 'blue', // canonical color for in_progress
  in_use: 'blue',
  ready: 'blue',
  sent: 'blue',
  submitted: 'blue',
  checked_out: 'blue',
  packed: 'blue',
  in_transit: 'blue',
  out_for_delivery: 'blue',
  rework: 'blue',
  not_applicable: 'blue',

  // --- amber: waiting / not-yet-started / caution ---
  pending: 'amber',
  pending_approval: 'amber',
  pending_disposition: 'amber',
  pending_resolution: 'amber',
  pending_inspection: 'amber',
  draft: 'amber',
  on_hold: 'amber',
  hold: 'amber',
  scheduled: 'amber',
  awaiting: 'amber',
  due: 'amber',
  partial: 'amber',
  under_review: 'amber',
  in_review: 'amber',
  in_implementation: 'amber',
  root_cause_analysis: 'amber',
  corrective_action: 'amber',
  verification: 'amber',
  conditional: 'amber',
  pre_transit: 'amber',
  maintenance: 'amber',
  warn: 'amber',
  repair: 'amber',

  // --- red: bad / failed / needs attention ---
  open: 'red', // defect/NCR sense: an open finding needs attention
  failed: 'red',
  rejected: 'red',
  overdue: 'red',
  scrapped: 'red',
  scrap: 'red',
  cancelled: 'red',
  canceled: 'red',
  blocked: 'red',
  non_compliant: 'red',
  needs_repair: 'red',
  lost: 'red',
  damaged: 'red',
  urgent: 'red',
  return_to_vendor: 'red',
  // A work center that is `offline` is DOWN (an active problem an operator must
  // see), distinct from `out_of_service` (decommissioned -> slate, below).
  offline: 'red',

  // --- slate: dormant / neutral terminal ---
  closed: 'slate',
  inactive: 'slate',
  obsolete: 'slate',
  void: 'slate',
  retired: 'slate',
  expired: 'slate',
  skipped: 'slate',
  out_of_service: 'slate',
  not_assessed: 'slate',
  // Receiving dock-to-stock: the lot was accepted into inventory without an
  // incoming inspection because none was required. Neutral slate (NOT green
  // `passed`) so the receiving record reads honestly — no inspection occurred.
  not_required: 'slate',
};

/**
 * Resolve a status string to its canonical semantic variant.
 * Case/format-insensitive (handles "In Progress", "IN_PROGRESS", etc.).
 * Falls back to 'slate' for unknown statuses (neutral, non-alarming).
 */
export function statusVariant(status: string | null | undefined): StatusVariant {
  if (!status) return 'slate';
  const key = String(status).trim().toLowerCase().replace(/[\s-]+/g, '_');
  return statusVariantMap[key] ?? 'slate';
}

/**
 * Resolve a status string straight to its bg/text class string.
 * This is what `StatusBadge` uses as its default coloring.
 */
export function statusColor(status: string | null | undefined): string {
  return variantClass[statusVariant(status)];
}

/**
 * Flat status -> class map (e.g. for `StatusBadge`'s `colorMap` prop or any
 * lookup-by-key call site). Derived from the canonical variant map so it can
 * never drift from `statusVariant`.
 */
export const statusColorMap: Record<string, string> = Object.fromEntries(
  Object.keys(statusVariantMap).map((status) => [status, statusColor(status)]),
);

/** Fallback class for an unknown status (also the StatusBadge default fallback). */
export const UNKNOWN_STATUS_CLASS = variantClass.slate;

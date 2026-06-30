/**
 * Visitor sign-in shared constants.
 *
 * Purpose tiles mirror the backend VisitorPurpose enum
 * (meeting|delivery|contractor|interview|audit|other). When "Other" is chosen
 * a free-text purpose note is REQUIRED (server-validated; the tablet also gates
 * submit on it).
 */

import type { VisitorPurpose } from '../../types/visitor';

export interface PurposeTile {
  value: VisitorPurpose;
  label: string;
}

/** Structured purpose tiles, in display order (touch-first, like kiosk reasons). */
export const PURPOSE_TILES: PurposeTile[] = [
  { value: 'meeting', label: 'Meeting' },
  { value: 'delivery', label: 'Delivery' },
  { value: 'contractor', label: 'Contractor' },
  { value: 'interview', label: 'Interview' },
  { value: 'audit', label: 'Audit' },
  { value: 'other', label: 'Other' },
];

/** Human label for a purpose value (used by the admin log table). */
export function purposeLabel(value: string): string {
  return PURPOSE_TILES.find(t => t.value === value)?.label ?? value;
}

/**
 * StatusBadge colorMap override for visitor statuses:
 *  - signed_in → amber (on-site, open visit)
 *  - signed_out → slate (closed, dormant terminal)
 * These two strings aren't in the central statusColors map, so the override
 * pins them deterministically rather than relying on the unknown→slate fallback.
 */
export const VISITOR_STATUS_COLORS: Record<string, string> = {
  signed_in: 'bg-amber-500/20 text-amber-300',
  signed_out: 'bg-slate-800/50 text-slate-400',
};

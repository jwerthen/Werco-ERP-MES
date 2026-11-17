/**
 * Preventive-maintenance display + payload helpers shared by the Maintenance page and its
 * components. The enums mirror backend/app/models/maintenance.py; the server types every one
 * of them, so an unknown value is a 422 rather than a silently stored string.
 */

import type {
  MaintenanceFrequency,
  MaintenancePriority,
  MaintenanceTargetFields,
  MaintenanceType,
} from '../types/maintenance';

export const MAINTENANCE_TYPE_LABELS: Record<MaintenanceType, string> = {
  preventive: 'Preventive',
  predictive: 'Predictive',
  corrective: 'Corrective',
  emergency: 'Emergency',
};

export const MAINTENANCE_PRIORITY_LABELS: Record<MaintenancePriority, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  critical: 'Critical',
};

/** The server's MaintenanceFrequency, in cadence order. There is no "usage based". */
export const MAINTENANCE_FREQUENCY_LABELS: Record<MaintenanceFrequency, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  biweekly: 'Bi-weekly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
  semi_annual: 'Semi-annual',
  annual: 'Annual',
  custom: 'Custom (every N days)',
};

export const frequencyText = (frequency: string, frequencyDays?: number | null): string => {
  if (frequency === 'custom') return frequencyDays ? `Every ${frequencyDays} days` : 'Custom';
  return (MAINTENANCE_FREQUENCY_LABELS as Record<string, string>)[frequency] ?? frequency.replace(/_/g, ' ');
};

/** "CNC-01 · Mori Seiki MV-653" for a work center, "FL-01 · Forklift 1" for an asset. */
export const targetText = (row: Partial<MaintenanceTargetFields>): string => {
  const code = row.target_code ?? (row.target_type === 'asset' ? row.asset_tag : null);
  const name = row.target_name ?? (row.target_type === 'asset' ? row.asset_name : row.work_center_name);
  return [code, name].filter(Boolean).join(' · ') || '—';
};

/**
 * A checklist typed one item per line, sent as the JSON array of strings the model stores
 * (blank lines dropped, whitespace trimmed). An empty checklist is sent as null.
 */
export const checklistPayload = (text: string): string | null => {
  const items = text
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
  return items.length ? JSON.stringify(items) : null;
};

type RoleUser = { role?: string | null; is_superuser?: boolean | null } | null | undefined;

/** Schedules, work-order create/edit/hold/cancel, assets and the PM import: ADMIN / MANAGER /
 *  SUPERVISOR -- plus PLATFORM_ADMIN, whom the server's `require_role` always admits (a hidden
 *  control the server would accept is a missing feature, not a safeguard). */
export const canPlanMaintenance = (user: RoleUser): boolean =>
  !!user && (!!user.is_superuser || ['admin', 'manager', 'supervisor', 'platform_admin'].includes(user.role ?? ''));

/** Starting / completing a maintenance work order also admits OPERATOR. */
export const canPerformMaintenance = (user: RoleUser): boolean => canPlanMaintenance(user) || user?.role === 'operator';

/** Statuses a work order can be started from (mirrors the start verb). */
export const STARTABLE_STATUSES = new Set(['scheduled', 'overdue', 'on_hold']);

/** Statuses a work order can still be cancelled from (anything not already over). */
export const CANCELLABLE_STATUSES = new Set(['scheduled', 'overdue', 'in_progress', 'on_hold']);

/** Statuses in which a maintenance work order is still open (mirrors the server's `_OPEN_STATUSES`). */
export const OPEN_WO_STATUSES = new Set(['scheduled', 'overdue', 'in_progress', 'on_hold']);

/** One line of a work order's checklist, ticked off at completion. */
export interface ChecklistResultItem {
  item: string;
  done: boolean;
}

/**
 * A work order's `checklist_results`, read tolerantly: the `[{item, done}]` array this page
 * writes, a plain JSON array of strings (a schedule-style checklist), or one item per line.
 */
export const parseChecklistResults = (raw?: string | null): ChecklistResultItem[] => {
  const text = (raw ?? '').trim();
  if (!text) return [];
  try {
    const parsed: unknown = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed
        .map(entry => {
          if (typeof entry === 'string') return { item: entry.trim(), done: false };
          if (entry && typeof entry === 'object') {
            const record = entry as Record<string, unknown>;
            const item = typeof record.item === 'string' ? record.item : String(record.item ?? '');
            return { item: item.trim(), done: record.done === true };
          }
          return { item: String(entry ?? '').trim(), done: false };
        })
        .filter(entry => entry.item);
    }
  } catch {
    // Not JSON: one item per line, below.
  }
  return text
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => ({ item, done: false }));
};

/** The `checklist_results` a work order is opened with: one unticked item per non-blank line. */
export const checklistResultsPayload = (text: string): string | null => {
  const items = text
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
  return items.length ? JSON.stringify(items.map(item => ({ item, done: false }))) : null;
};

/** The open work order (if any) each PM schedule is being worked under, keyed by schedule id. */
export const openWorkOrdersBySchedule = <T extends { schedule_id?: number | null; status: string }>(
  workOrders: T[]
): Map<number, T> => {
  const bySchedule = new Map<number, T>();
  for (const wo of workOrders) {
    if (wo.schedule_id != null && OPEN_WO_STATUSES.has(wo.status) && !bySchedule.has(wo.schedule_id)) {
      bySchedule.set(wo.schedule_id, wo);
    }
  }
  return bySchedule;
};

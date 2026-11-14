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

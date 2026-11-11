/**
 * Helpers shared by the core dashboard widgets (moved verbatim from pages/Dashboard.tsx).
 */
import type { StatusVariant } from '../../../ui';
import type { ActiveAssignment } from '../../../../types';

// Foundry tactical palette — no purple/indigo/orange; blue · cyan · green · amber · red
export const workCenterTypeColors: Record<string, string> = {
  fabrication: 'bg-fd-blue',
  cnc_machining: 'bg-fd-cyan',
  laser: 'bg-blue-600',
  press_brake: 'bg-sky-500',
  paint: 'bg-fd-amber',
  powder_coating: 'bg-amber-600',
  assembly: 'bg-fd-green',
  welding: 'bg-fd-red',
  inspection: 'bg-fd-blue',
  shipping: 'bg-slate-600',
};

// Work-center status dot colors derived from the central status variant so a
// given status reads identically across the app. `statusVariant()` resolves
// available->green, in_use->blue, maintenance->amber, offline->slate.
export const variantDotColor: Record<StatusVariant, string> = {
  green: 'bg-fd-green',
  blue: 'bg-fd-blue',
  amber: 'bg-fd-amber',
  red: 'bg-fd-red',
  slate: 'bg-slate-500',
};

const roleBadgeClasses: Record<string, string> = {
  admin: 'bg-slate-700/40 text-slate-300',
  manager: 'bg-fd-cyan/15 text-fd-cyan',
  supervisor: 'bg-fd-blue/15 text-fd-blue',
  operator: 'bg-fd-green/15 text-fd-green',
  quality: 'bg-fd-amber/15 text-fd-amber',
  shipping: 'bg-fd-blue/15 text-fd-blue',
  viewer: 'bg-slate-700/40 text-slate-400',
};

export const formatElapsed = (clockIn: string, nowMs: number) => {
  const startMs = new Date(clockIn).getTime();
  if (Number.isNaN(startMs)) return '--';

  const diffMs = Math.max(nowMs - startMs, 0);
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours === 0) {
    return `${minutes}m`;
  }

  return `${hours}h ${minutes}m`;
};

export const getRoleBadgeClass = (role?: string) => {
  return roleBadgeClasses[role || 'viewer'] || 'bg-slate-800/50 text-slate-300';
};

export const getEntryTypeLabel = (entryType?: string) => {
  if (!entryType) return 'Run';
  return entryType.charAt(0).toUpperCase() + entryType.slice(1);
};

// Compact operator label for dense rows: backend-provided display_name first,
// then "First L." derived from the full name, then the full name (single-token
// names), then the employee id as a last resort.
export const getOperatorDisplayName = (user: ActiveAssignment['user']): string => {
  if (user.display_name) return user.display_name;
  const name = (user.name || '').trim();
  if (name) {
    const tokens = name.split(/\s+/);
    if (tokens.length > 1) {
      return `${tokens[0]} ${tokens[tokens.length - 1].charAt(0).toUpperCase()}.`;
    }
    return name;
  }
  // employee_id is typed string, but the server serializes null for orphaned
  // time entries whose user row is gone — don't render "null" as a name.
  return user.employee_id || 'Unknown';
};

/** Live assignments sorted by work-center name, then clock-in. */
export function sortActiveAssignments(assignments: ActiveAssignment[] | undefined): ActiveAssignment[] {
  return [...(assignments || [])].sort((left, right) => {
    const centerCompare = (left.work_center.name || '').localeCompare(right.work_center.name || '');
    if (centerCompare !== 0) return centerCompare;
    return new Date(left.clock_in).getTime() - new Date(right.clock_in).getTime();
  });
}

/** Scroll to the idle-operators list in the presence panel and focus it (null-safe). */
export function focusIdleOperators() {
  const panel = document.getElementById('idle-operators');
  panel?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  panel?.focus();
}

/** "N not available" copy shared by source-backed widgets. */
export const NOT_AVAILABLE_FOR_ROLE = 'Not available for your role';

/**
 * Pure layout math for the /wallboard ANDON WALL floor grid.
 *
 * Kept free of React so the deterministic grid shape, alarm-first sort, and
 * density-tier hysteresis are directly unit-testable (see
 * wallboardLayout.test.ts — N = 1, 3, 8, 9, 12, 14, 20).
 */

import type { WallboardJob, WallboardWorkCenter } from '../types/wallboard';

/** Per-tile job-row budget tier, driven by the count of ACTIVE tiles. */
export type WallboardDensityTier = 'roomy' | 'standard' | 'dense';

export interface WallboardGridShape {
  rows: number;
  cols: number;
  tier: WallboardDensityTier;
}

/** Job rows a tile may show before collapsing the rest into "+N more". */
export const TIER_JOB_ROWS: Record<WallboardDensityTier, number> = {
  roomy: 3,
  standard: 2,
  dense: 1,
};

export function tierForCount(nActive: number): WallboardDensityTier {
  if (nActive <= 6) return 'roomy';
  if (nActive <= 12) return 'standard';
  return 'dense';
}

/**
 * Deterministic grid that always exactly fills the wall:
 *   rows = max(1, round(sqrt(N / 1.6)))   cols = ceil(N / rows)
 * Verified: 1→1×1, 3→1×3, 8→2×4, 9→2×5, 12→3×4, 14→3×5, 20→4×5.
 * Trailing empty cells (rows*cols − N) render as plain background and always
 * land bottom-right (row-major grid flow).
 */
export function computeGridShape(nActive: number): WallboardGridShape {
  const n = Math.max(1, nActive);
  const rows = Math.max(1, Math.round(Math.sqrt(n / 1.6)));
  const cols = Math.ceil(n / rows);
  return { rows, cols, tier: tierForCount(n) };
}

/**
 * Density-tier hysteresis: a tier change only applies once the candidate tier
 * has held for TWO consecutive polls, so a work center flapping in/out of idle
 * across the 6↔7 or 12↔13 boundary can't thrash every tile's row budget.
 * Grid rows/cols still track N immediately (a tile appearing/disappearing has
 * to reflow); only the job-row budget is damped.
 */
export interface TierHysteresisState {
  /** The tier currently applied to the wall. */
  tier: WallboardDensityTier;
  /** Candidate tier seen on the previous poll (null = no pending change). */
  pendingTier: WallboardDensityTier | null;
}

export function nextTierState(prev: TierHysteresisState | null, nActive: number): TierHysteresisState {
  const candidate = tierForCount(Math.max(1, nActive));
  // First paint: apply immediately — nothing to damp yet.
  if (prev === null) return { tier: candidate, pendingTier: null };
  if (candidate === prev.tier) return { tier: prev.tier, pendingTier: null };
  // Second consecutive poll on the same new tier → commit the switch.
  if (prev.pendingTier === candidate) return { tier: candidate, pendingTier: null };
  // First poll across the boundary → hold the old tier, remember the candidate.
  return { tier: prev.tier, pendingTier: candidate };
}

// ---- Work-center classification & sort -------------------------------------

/** Alarm-first tile order: DOWN → BLOCKED → RUNNING-LATE → RUNNING. */
export type WorkCenterStateClass = 'down' | 'blocked' | 'late' | 'running';

const CLASS_ORDER: Record<WorkCenterStateClass, number> = {
  down: 0,
  blocked: 1,
  late: 2,
  running: 3,
};

export function classifyWorkCenter(wc: WallboardWorkCenter): WorkCenterStateClass {
  if (wc.down !== null) return 'down';
  if (wc.blocked_count > 0) return 'blocked';
  if (wc.active_jobs.some(job => job.is_late ?? false)) return 'late';
  return 'running';
}

/** Idle = nothing running, nothing down, nothing blocked → idle strip, not a tile. */
export function isIdleWorkCenter(wc: WallboardWorkCenter): boolean {
  return wc.active_jobs.length === 0 && wc.down === null && wc.blocked_count === 0;
}

/**
 * Partition into grid tiles (alarm-first, alphabetical within class — spatial
 * stability: a tile only moves when its state CLASS changes) and idle-strip
 * centers (alphabetical).
 */
export function partitionWorkCenters(workCenters: WallboardWorkCenter[]): {
  active: WallboardWorkCenter[];
  idle: WallboardWorkCenter[];
} {
  const active: WallboardWorkCenter[] = [];
  const idle: WallboardWorkCenter[] = [];
  for (const wc of workCenters) {
    (isIdleWorkCenter(wc) ? idle : active).push(wc);
  }
  active.sort((a, b) => {
    const byClass = CLASS_ORDER[classifyWorkCenter(a)] - CLASS_ORDER[classifyWorkCenter(b)];
    if (byClass !== 0) return byClass;
    return a.name.localeCompare(b.name);
  });
  idle.sort((a, b) => a.name.localeCompare(b.name));
  return { active, idle };
}

// ---- Job (work-order) classification ----------------------------------------

/**
 * Job-tile state class, strict precedence DOWN > BLOCKED > LATE > RUNNING >
 * WAITING. Drives the filled header band + right-hand state word. The server
 * sorts the wall (alarm-first, then lateness, then promise date) — the client
 * NEVER re-sorts; this classifier only styles.
 */
export type JobStateClass = 'down' | 'blocked' | 'late' | 'running' | 'waiting';

export function classifyJob(job: WallboardJob): JobStateClass {
  if (job.down) return 'down';
  if (job.blocked) return 'blocked';
  if (job.is_late) return 'late';
  if (job.running) return 'running';
  return 'waiting';
}

// ---- Leading-magnitude / duration formatting --------------------------------

/** Downtime / elapsed minutes → "47m", "2h14m", "38h" (fits the 5ch column). */
export function formatDownDuration(minutes: number): string {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 10) return `${h}h${String(m % 60).padStart(2, '0')}m`;
  return `${h}h`;
}

/** Blocked age in hours → "45m", "38h", "6d" (fits the 5ch column). */
export function formatAgeHours(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  if (hours < 100) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

/** "material_missing" → "material missing" (render uppercase via CSS). */
export function blockerLabel(category: string): string {
  return category.replace(/_/g, ' ');
}

/** Dept chip renders title-cased, never the raw query param: "machining" → "Machining". */
export function titleCaseDept(dept: string): string {
  return dept
    .replace(/[_-]+/g, ' ')
    .trim()
    .split(/\s+/)
    .map(word => (word ? word[0].toUpperCase() + word.slice(1).toLowerCase() : word))
    .join(' ');
}

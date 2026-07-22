/**
 * Pure classification + formatting helpers for the /wallboard Foundry TV
 * board. Kept free of React so the strict job-state precedence and the
 * duration / label formatting are directly unit-testable (see
 * wallboardLayout.test.ts). The server sorts the board; nothing here sorts.
 */

import type { WallboardJob } from '../types/wallboard';

/**
 * Work-order card state class, strict precedence DOWN > BLOCKED > LATE >
 * RUNNING > WAITING. Drives the card's status edge, chip, time value, and
 * stop reason. The server sorts the grid (alarm-first) — the client NEVER
 * re-sorts; this classifier only styles.
 */
export type JobStateClass = 'down' | 'blocked' | 'late' | 'running' | 'waiting';

export function classifyJob(job: WallboardJob): JobStateClass {
  if (job.down) return 'down';
  if (job.blocked) return 'blocked';
  if (job.is_late) return 'late';
  if (job.running) return 'running';
  return 'waiting';
}

// ---- Duration / label formatting --------------------------------------------

/** Downtime / elapsed minutes → "47m", "2h14m", "38h" (render uppercase). */
export function formatDownDuration(minutes: number): string {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 10) return `${h}h${String(m % 60).padStart(2, '0')}m`;
  return `${h}h`;
}

/** Blocked age in hours → "45m", "38h", "6d" (render uppercase). */
export function formatAgeHours(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  if (hours < 100) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

/** "material_missing" → "material missing" (render uppercase via caller). */
export function blockerLabel(category: string): string {
  return category.replace(/_/g, ' ');
}

/** Sanitize the raw ?dept= param for display: "cnc_machining" → "Cnc Machining"
 *  (the HUD scope line uppercases the result — this mainly strips separators). */
export function titleCaseDept(dept: string): string {
  return dept
    .replace(/[_-]+/g, ' ')
    .trim()
    .split(/\s+/)
    .map(word => (word ? word[0].toUpperCase() + word.slice(1).toLowerCase() : word))
    .join(' ');
}

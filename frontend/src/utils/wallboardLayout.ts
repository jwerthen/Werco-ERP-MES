/**
 * Pure classification, windowing + formatting helpers for the /wallboard
 * Foundry TV board. Kept free of React so the strict job-state precedence,
 * the anchor/field paging math and the duration / label formatting are
 * directly unit-testable (see wallboardLayout.test.ts).
 * The server sorts the board; nothing here sorts.
 */

import type { WallboardJob } from '../types/wallboard';

/**
 * Work-order card state class, strict precedence HELD > DOWN > BLOCKED >
 * LATE > RUNNING > WAITING. Drives the card's status edge, chip, time value,
 * and stop reason. The server sorts the grid (alarm-first) — the client NEVER
 * re-sorts; this classifier only styles.
 *
 * `held` is FIRST on purpose. An ON_HOLD work order is deliberately stopped
 * and somebody already knows; showing DOWN or BLOCKED on it would name the
 * wrong condition, and — because `held` renders grey/de-emphasized like
 * WAITING — leading the precedence is exactly what keeps a held card out of
 * the pulsing red DOWN wash. The alarm channel is not spent on a known stop.
 */
export type JobStateClass = 'held' | 'down' | 'blocked' | 'late' | 'running' | 'waiting';

/**
 * The `WorkOrderStatus` value the backend serializes for an on-hold WO
 * (`WorkOrderStatus.ON_HOLD = "on_hold"`, lower-cased enum value — see
 * `wallboard_service._build_job_wall`). Held jobs joined the wall population
 * with `_JOB_WALL_WO_STATUSES = [RELEASED, IN_PROGRESS, ON_HOLD]`; the server
 * also sorts them to the BACK, so a held job can never take an anchor slot.
 */
export const HELD_WO_STATUS = 'on_hold';

export function classifyJob(job: WallboardJob): JobStateClass {
  if (job.status?.trim().toLowerCase() === HELD_WO_STATUS) return 'held';
  if (job.down) return 'down';
  if (job.blocked) return 'blocked';
  if (job.is_late) return 'late';
  if (job.running) return 'running';
  return 'waiting';
}

// ---- Anchor row + rotating field ---------------------------------------------

/** Grid row 1 — `jobs.slice(0, 4)`. Pinned, live, never part of a page plan. */
export const ANCHOR_SLOTS = 4;

/** Grid rows 2-3 — the window size over `jobs.slice(ANCHOR_SLOTS)`. */
export const FIELD_SLOTS = 8;

/**
 * Refuse to page unless a flip reveals at least this many NEW cards. A flip
 * that displaces 8 cards to show 1-3 is motion the board did not earn, so the
 * static band (`pages === 1` for every delivered count <= 15) falls out of the
 * formula rather than being a hard-coded threshold.
 */
export const MIN_NEW = 4;

/**
 * Modulo, not remainder. JS `%` is a SIGN-PRESERVING REMAINDER: a backward
 * system-clock step (near-certain over weeks on consumer TV hardware that
 * boots with a bad RTC and then NTP-syncs) makes `slot - anchorSlot` negative,
 * `-1 % 3` is `-1`, `starts[-1]` is `undefined`, and the grid goes silently
 * blank WITHOUT THROWING — so the global ErrorBoundary never fires and the
 * board just sits there empty. This is the fix.
 *
 * Returns 0 (page 0 — today's board) for a non-positive or non-finite modulus,
 * and for a non-finite `a` (an Invalid Date reaches here as NaN, which would
 * index `undefined` exactly like the negative case).
 */
export function safeMod(a: number, b: number): number {
  if (!Number.isFinite(a) || !Number.isFinite(b) || b < 1) return 0;
  return ((a % b) + b) % b;
}

/**
 * Page plan for the rotating field, given the FIELD count
 * (`F = max(0, jobs.length - ANCHOR_SLOTS)` — the count of jobs BELOW the
 * anchor row, NOT `jobs.length`).
 *
 * Returns the `starts` array; its length IS the page count.
 *
 *   pages     = F < FIELD_SLOTS + MIN_NEW (=12) ? 1 : ceil(F / FIELD_SLOTS)
 *   starts[i] = min(i * FIELD_SLOTS, F - FIELD_SLOTS)
 *
 * Pages are disjoint except the FINAL window, which back-fills flush against
 * the end so the last page is a full page instead of a row of holes. Three
 * properties fall out and are property-tested, not example-tested:
 * coverage (the union of the windows is every field index), never-partially-
 * blank (with `pages > 1` every window holds exactly FIELD_SLOTS entries),
 * and the static band above.
 *
 * The `max(0, ...)` floor is a no-op wherever `pages > 1` (there `F >= 12`, so
 * `F - FIELD_SLOTS >= 4`); it only keeps the single-page degenerate case
 * (`F < FIELD_SLOTS`) from reporting a nonsense negative start.
 */
export function planFieldPages(fieldCount: number): number[] {
  const total = Number.isFinite(fieldCount) ? Math.max(0, Math.trunc(fieldCount)) : 0;
  const pages = total < FIELD_SLOTS + MIN_NEW ? 1 : Math.ceil(total / FIELD_SLOTS);
  const maxStart = Math.max(0, total - FIELD_SLOTS);
  return Array.from({ length: pages }, (_, i) => Math.max(0, Math.min(i * FIELD_SLOTS, maxStart)));
}

/**
 * The FIELD_SLOTS-wide window of the field list at `start`.
 *
 * CLAMPS `start` into range internally, so an out-of-range, negative,
 * fractional or non-finite page position can never produce an empty or
 * partial grid no matter what the caller passes. This is the second of the
 * two independent guards on the page index (`safeMod` is the first) — the
 * failure it closes is silent, so it is closed twice.
 *
 * Generic because the cycle plan freezes `wo_number` STRINGS, while a test or
 * a caller resolving through the live payload holds jobs.
 */
export function fieldWindow<T>(fieldWos: readonly T[], start: number): T[] {
  const maxStart = Math.max(0, fieldWos.length - FIELD_SLOTS);
  const clamped = Number.isFinite(start) ? Math.min(Math.max(0, Math.trunc(start)), maxStart) : 0;
  return fieldWos.slice(clamped, clamped + FIELD_SLOTS);
}

/** What the 2.375rem strip under the grid renders this tick. */
export interface StripCopy {
  /** `null` = render NO strip at all (n === 0; WoGrid's EmptyZone owns the zone). */
  text: string | null;
  /** The segmented page bar is rendered ONLY while the board is cycling. */
  showPageBar: boolean;
}

/**
 * The five-state strip copy matrix. `delivered` = `jobs.length` (n),
 * `total` = `jobs_total`, `R` = the count of open work orders the board is NOT
 * showing (see the comment on `onBoard` below: `max(0, total - min(n, 12))` in
 * the static band — today's rule verbatim — and `max(0, total - n)` once the
 * board cycles and every delivered job reaches the screen).
 *
 * 1. n === 0                 -> no strip at all (unchanged empty zone).
 * 2. pages === 1 && R === 0  -> ALL OPEN WORK ORDERS ON BOARD      (byte-identical to today)
 * 3. pages === 1 && R > 0    -> +{R} MORE WORK ORDERS IN QUEUE     (byte-identical to today)
 * 4. pages > 1  && R === 0   -> TOP 4 PINNED · PAGE i/pages · n OPEN WORK ORDERS
 * 5. pages > 1  && R > 0     -> TOP 4 PINNED · PAGE i/pages · n OF total OPEN WORK ORDERS · +{R} NOT ON BOARD
 *
 * Two lines of that matrix are CORRECTNESS, not taste:
 * - `+N MORE ... IN QUEUE` is NEVER emitted while cycling, so the phrase keeps
 *   exactly one meaning across the whole screen — "permanently hidden and
 *   strictly less severe", owned solely by the Z3 rail. A viewer who learned
 *   that Zone 2's `+N` resolves itself on a cadence would stand waiting in
 *   front of the LATE panel forever. `+N NOT ON BOARD` is deliberately
 *   different wording for the genuinely truncated residue.
 * - `PINNED`, never `HELD`: ON_HOLD work orders are on the wall now, so "HELD"
 *   would carry two meanings inside one strip.
 */
export function stripCopy({
  pageIndex,
  pages,
  delivered,
  total,
}: {
  pageIndex: number;
  pages: number;
  delivered: number;
  total: number;
}): StripCopy {
  const n = Number.isFinite(delivered) ? Math.max(0, Math.trunc(delivered)) : 0;
  if (n === 0) return { text: null, showPageBar: false };

  const openTotal = Number.isFinite(total) ? Math.max(0, Math.trunc(total)) : n;
  const pageCount = Number.isFinite(pages) ? Math.max(1, Math.trunc(pages)) : 1;

  // The residue is counted against what the board ACTUALLY PUTS ON SCREEN, which
  // is only `delivered` once the board cycles. In the static band the grid is 12
  // cells and the 13th..15th delivered job is off-board exactly as it is today —
  // so `+N MORE` there must count from `min(n, 12)`, today's rule verbatim
  // (`WoGrid`: `jobsTotal - visible.length`). Counting from `n` instead would
  // print `+3` where today prints `+5` (Wallboard.test.tsx's 14-jobs/17-total
  // case) and, at n = 13 with nothing truncated, would claim ALL OPEN WORK
  // ORDERS ON BOARD with a job hidden. Once `pages > 1` every delivered job does
  // reach the screen, so there the residue is the genuinely truncated tail.
  const onBoard = pageCount <= 1 ? Math.min(n, ANCHOR_SLOTS + FIELD_SLOTS) : n;
  const residue = Math.max(0, openTotal - onBoard);

  if (pageCount <= 1) {
    return {
      text: residue > 0 ? `+${residue} MORE WORK ORDERS IN QUEUE` : 'ALL OPEN WORK ORDERS ON BOARD',
      showPageBar: false,
    };
  }

  // safeMod already lands in [0, pageCount); trunc keeps a fractional index from
  // printing "PAGE 1.5/3", and the clamp is the same second guard fieldWindow applies.
  const safeIndex = Math.min(Math.max(0, Math.trunc(safeMod(pageIndex, pageCount))), pageCount - 1);
  const head = `TOP ${ANCHOR_SLOTS} PINNED · PAGE ${safeIndex + 1}/${pageCount}`;
  const text =
    residue > 0
      ? `${head} · ${n} OF ${openTotal} OPEN WORK ORDERS · +${residue} NOT ON BOARD`
      : `${head} · ${n} OPEN WORK ORDERS`;
  return { text, showPageBar: true };
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

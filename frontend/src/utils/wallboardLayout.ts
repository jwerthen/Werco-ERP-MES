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
 * The board cycles whenever a delivered job would otherwise be off-screen —
 * i.e. from 13 delivered work orders up (owner decision 2026-08-19). An earlier
 * cut refused to page until a flip revealed at least 4 new cards, on the
 * argument that displacing 8 cards to show 1-3 is motion the board did not
 * earn. The owner overruled it: a job the floor cannot see is the failure this
 * feature exists to fix, and "+2 MORE WORK ORDERS IN QUEUE" is that failure
 * whatever the motion economics.
 *
 * The accepted cost lives in the 13-15 band, and it is a real one. `starts` is
 * flush-clamped so every page stays FULL (never a row of holes), which at
 * F = 9..11 leaves the two windows overlapping by 5-7 of their 8 cards: the
 * flip reads as the field SHIFTING by one to three slots rather than as a clean
 * page turn. That is the least legible flip the board can produce, and it is
 * strictly better than the alternatives at those counts — a short page would
 * blank 4-7 cells for a whole dwell, and disjoint full pages are arithmetically
 * impossible when F is barely over FIELD_SLOTS. From 16 delivered jobs (F = 12,
 * the owner's actual shop) the stride is a clean 4 — exactly one grid row.
 */

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
 *   pages     = F <= FIELD_SLOTS ? 1 : ceil(F / FIELD_SLOTS)
 *   starts[i] = min(i * FIELD_SLOTS, F - FIELD_SLOTS)
 *
 * Pages are disjoint except the FINAL window, which back-fills flush against
 * the end so the last page is a full page instead of a row of holes. Three
 * properties fall out and are property-tested, not example-tested:
 * coverage (the union of the windows is every field index, so no delivered job
 * is ever unreachable), never-partially-blank (with `pages > 1` every window
 * holds exactly FIELD_SLOTS entries), and the static band — `pages === 1`
 * exactly when nothing is off-screen, i.e. for every delivered count <= 12.
 *
 * The `max(0, ...)` floor is a no-op wherever `pages > 1` (there `F >= 9`, so
 * `F - FIELD_SLOTS >= 1`); it only keeps the single-page degenerate case
 * (`F < FIELD_SLOTS`) from reporting a nonsense negative start.
 */
export function planFieldPages(fieldCount: number): number[] {
  const total = Number.isFinite(fieldCount) ? Math.max(0, Math.trunc(fieldCount)) : 0;
  const pages = total <= FIELD_SLOTS ? 1 : Math.ceil(total / FIELD_SLOTS);
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
 * `total` = `jobs_total`, `R = max(0, total - n)` — the open work orders the
 * board is NOT showing, which is only ever the tail the SERVER truncated at its
 * 24-job cap, because every delivered job now reaches the screen (below 13 it
 * fits; at 13 and up the field cycles).
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

  // The residue is counted against what the board ACTUALLY PUTS ON SCREEN — and
  // since the board now cycles whenever a delivered job would be off-screen,
  // that is ALWAYS `delivered`. `pages === 1` holds exactly when `n <= 12`, so
  // the single-page case needs no `min(n, 12)` of its own: the two branches
  // collapse to one rule, and `+N` counts only genuinely TRUNCATED work (the
  // tail beyond the server's 24-job cap) in both.
  const residue = Math.max(0, openTotal - n);

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

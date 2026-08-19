/**
 * Zone 2 cycle state for the /wallboard TV board — the anchor row (grid row 1,
 * pinned) plus the rotating field (grid rows 2-3, flipping on a 22s dwell).
 *
 * Deliberately a `.ts` file with NO JSX: the paging math lives in
 * `utils/wallboardLayout.ts` and the *policy* lives here, so neither needs a
 * renderer to be reasoned about. The hook adds NO timer of its own — `slot` is
 * derived by the caller from the board's existing 1s clock tick, which is what
 * keeps every TV in the building in phase and self-correcting after a throttled
 * or occluded tab resumes.
 *
 * THE LOAD-BEARING PROPERTY: anchor + field page 0 is exactly `jobs[0..11]` —
 * today's board, card for card. Rotation is a departure from the current board
 * and a return to it, so a job shown twice or silently skipped is (with the one
 * documented exception below) unrepresentable rather than merely defended
 * against.
 *
 * FREEZE IDENTITY, RESOLVE CONTENT LIVE. The plan holds `wo_number` STRINGS
 * only; every render resolves them through a Map built from the freshest
 * payload. Freezing the job OBJECT would stall `WoCard`'s elapsed clock for a
 * whole dwell and then jump it (the card computes
 * `(current_op.elapsed_minutes ?? 0) + extraMinutes`, and `extraMinutes` is
 * measured from the live `lastUpdated`), and it would let a card render RUNNING
 * while the HUD chip beside it already says DOWN. `anchorJobs` is NEVER in the
 * plan — it is `jobs.slice(0, ANCHOR_SLOTS)` re-derived live on every render.
 */

import { useEffect, useMemo, useState } from 'react';
import type { WallboardJob } from '../../types/wallboard';
import { ANCHOR_SLOTS, FIELD_SLOTS, fieldWindow, planFieldPages, safeMod } from '../../utils/wallboardLayout';

/** A field slot with no resolvable job renders a plain cell, never a ghost card. */
const EMPTY_FIELD: readonly (WallboardJob | null)[] = Array.from({ length: FIELD_SLOTS }, () => null);

export interface WoCycle {
  /** Grid row 1 — live, pinned, never paged. Length 0..ANCHOR_SLOTS. */
  anchorJobs: WallboardJob[];
  /** Grid rows 2-3 — ALWAYS exactly FIELD_SLOTS entries; `null` = plain cell. */
  fieldJobs: (WallboardJob | null)[];
  pageIndex: number;
  pages: number;
}

interface CyclePlan {
  /**
   * The delivered SET, ORDER-INSENSITIVE (`wo_numbers.sort().join(',')`). This
   * is what survives the real stress case: `running` is position 3 of the
   * server's sort key and flips on every clock-in/out, so at lunch and every
   * shift change nearly the whole shop's flag flips within minutes — which is
   * also when the most people walk past. Under an order-insensitive key that
   * reorders the array and rebuilds nothing: no card moves, cards only recolor
   * in place.
   */
  key: string;
  /** The `?dept=` this plan's wo_numbers were drawn from. */
  dept: string | null;
  /** The alarm-snap counter this plan was built at (see rebuild case b). */
  alarmSnap: number;
  /** Frozen identities of `jobs.slice(ANCHOR_SLOTS)` — strings, never objects. */
  fieldWos: string[];
  /** `planFieldPages(fieldWos.length)`; its length IS the page count. */
  starts: number[];
  /** Phase origin: `pageIndex = safeMod(slot - anchorSlot, pages)`. */
  anchorSlot: number;
  /**
   * Set to the slot in which a set-change was FIRST observed; the deferred
   * rebuild (case d) fires once `slot` moves past it, i.e. at the next cycle
   * boundary. `null` = nothing pending.
   */
  pendingSince: number | null;
}

function planKey(jobs: readonly WallboardJob[]): string {
  return jobs
    .map(job => job.wo_number)
    .slice()
    .sort()
    .join(',');
}

function buildPlan(
  key: string,
  dept: string | null,
  alarmSnap: number,
  fieldWos: string[],
  anchorSlot: number
): CyclePlan {
  return { key, dept, alarmSnap, fieldWos, starts: planFieldPages(fieldWos.length), anchorSlot, pendingSince: null };
}

/**
 * @param jobs      The delivered, server-sorted job wall (`data.jobs`). The client NEVER re-sorts.
 * @param dept      The board's `?dept=` scope — a change invalidates every frozen wo_number.
 * @param slot      `Math.floor(now.getTime() / CYCLE_DWELL_MS)`, from the caller's existing 1s tick.
 * @param alarmSnap Monotonic counter bumped by the caller's poll-time diff whenever a wo_number is
 *                  NEW to the `(blocked || down)` set. EDGE-triggered: a machine down for three
 *                  hours fires ONCE, so the board can never get pinned on page 0 all afternoon.
 * @param frozen    nightDim — the board is declaring nobody is looking, so the cycle pins to page 0.
 * @param revoked   Dead display token; the caller renders its own full-screen state.
 */
export function useWoCycle({
  jobs,
  dept,
  slot,
  alarmSnap,
  frozen,
  revoked,
}: {
  jobs: WallboardJob[] | null | undefined;
  dept: string | null;
  slot: number;
  alarmSnap: number;
  frozen: boolean;
  revoked: boolean;
}): WoCycle {
  const [plan, setPlan] = useState<CyclePlan | null>(null);

  const list = useMemo(() => (Array.isArray(jobs) ? jobs : []), [jobs]);
  const key = useMemo(() => planKey(list), [list]);
  const fieldWos = useMemo(() => list.slice(ANCHOR_SLOTS).map(job => job.wo_number), [list]);
  const byWo = useMemo(() => new Map(list.map(job => [job.wo_number, job])), [list]);

  /**
   * REBUILD POLICY, exhaustive:
   *  (a) plan is null                     -> build now.
   *  (b) alarmSnap advanced               -> rebuild now, snap the field to page 0.
   *  (c) dept changed                     -> rebuild now (else the frozen wo_numbers hold the OLD
   *                                          department's jobs and resolve to a page of blanks).
   *  (d) set changed, no new alarm        -> rebuild at the NEXT cycle boundary, PRESERVING the
   *                                          phase, so a WO completing or being released never
   *                                          disturbs a cycle in progress and never yanks the board
   *                                          back to page 0 (which, on a shop where something closes
   *                                          every few minutes, would mean the later pages are never
   *                                          reached at all).
   *  (e) ORDER changed, set unchanged     -> NO REBUILD AT ALL.
   *
   * Case (d) defers because it must not disturb A CYCLE IN PROGRESS — so it is
   * gated on there BEING one. Two conditions mean there is not, and both rebuild
   * immediately rather than showing a page of blanks for a whole 22s dwell:
   * the plan holds no field at all (the board's cold start — the very first
   * render happens before any payload has arrived, and treating that empty plan
   * as an in-progress cycle would blank grid rows 2-3 for the first dwell of
   * every boot), and the plan's entire frozen field has vanished from the live
   * payload (a wholesale population replacement; case (c) is the special case of
   * this that we can detect without looking at the data).
   */
  useEffect(() => {
    if (revoked) return;
    if (list.length === 0) return; // nothing delivered yet — there is nothing to plan
    setPlan(prev => {
      if (prev === null) return buildPlan(key, dept, alarmSnap, fieldWos, slot); // (a)
      if (prev.alarmSnap !== alarmSnap) return buildPlan(key, dept, alarmSnap, fieldWos, slot); // (b)
      if (prev.dept !== dept) return buildPlan(key, dept, alarmSnap, fieldWos, slot); // (c)
      // (e) — and the no-change case. DISARM any pending rebuild: the delivered
      // set is back to the one this plan was built from, so nothing is waiting
      // for a boundary. (It really does come back — `jobs` is capped at 24, so
      // on a shop with more open work than that, a WO flapping across the rank-24
      // boundary leaves and re-enters the set. Left armed, the NEXT genuine set
      // change would find `pendingSince !== slot` and rebuild IMMEDIATELY, which
      // is exactly the mid-dwell disturbance case (d) exists to prevent.)
      if (prev.key === key) return prev.pendingSince === null ? prev : { ...prev, pendingSince: null };
      if (fieldWos.length > 0) {
        const stranded = prev.fieldWos.length > 0 && !prev.fieldWos.some(wo => byWo.has(wo));
        // No cycle is in progress to protect — heal now, at page 0.
        if (prev.fieldWos.length === 0 || stranded) return buildPlan(key, dept, alarmSnap, fieldWos, slot);
      }
      if (prev.pendingSince === null) return { ...prev, pendingSince: slot }; // (d) arm
      if (prev.pendingSince === slot) return prev; // (d) still inside the same dwell
      return buildPlan(key, dept, alarmSnap, fieldWos, prev.anchorSlot); // (d) fire, phase preserved
    });
  }, [revoked, list, dept, alarmSnap, key, slot, fieldWos, byWo]);

  // A plan built for another dept, or from before the alarm snap, must not be
  // rendered for even one frame — page 0 of the LIVE field is exactly what both
  // of those rebuilds are about to produce, so falling back to it here makes the
  // snap immediate instead of one commit late.
  const usable = plan !== null && plan.dept === dept && plan.alarmSnap === alarmSnap;
  const activeWos = usable ? plan.fieldWos : fieldWos;
  const starts = usable ? plan.starts : planFieldPages(fieldWos.length);
  const pages = Math.max(1, starts.length);
  // `frozen` (nightDim) pins the cycle to page 0 — the board is explicitly
  // declaring nobody is looking, and page 0 is the board people already know.
  const pageIndex = frozen || !usable || plan === null ? 0 : safeMod(slot - plan.anchorSlot, pages);

  const anchorJobs = useMemo(() => list.slice(0, ANCHOR_SLOTS), [list]);

  const fieldJobs = useMemo(() => {
    const anchorSet = new Set(anchorJobs.map(job => job.wo_number));
    const slotWos = fieldWindow(activeWos, starts[pageIndex] ?? 0);
    return Array.from({ length: FIELD_SLOTS }, (_, i) => {
      const wo: string | undefined = slotWos[i];
      if (wo === undefined) return null;
      // MID-CYCLE RESOLUTION RULES (none of them touch the plan; all heal at the
      // next boundary):
      //  1. A frozen wo_number absent from the live payload renders a PLAIN CELL,
      //     not a stale ghost card.
      //  2. Survivors deliberately do NOT reflow up — reflow is the coordinate
      //     scrambling this whole design exists to prevent — so the hole stays a
      //     hole for the rest of the dwell.
      //  3. A wo_number that the live server sort has since lifted INTO the
      //     anchor row also renders a plain cell. The anchor is live while the
      //     field is frozen, so a reorder (case e, which deliberately rebuilds
      //     nothing) can put one job in both halves; without this it would render
      //     twice under one React key. It is not "skipped" — it is on screen, in
      //     row 1.
      if (anchorSet.has(wo)) return null;
      return byWo.get(wo) ?? null;
    });
  }, [anchorJobs, activeWos, starts, pageIndex, byWo]);

  if (revoked) {
    return { anchorJobs: [], fieldJobs: EMPTY_FIELD.slice(), pageIndex: 0, pages: 1 };
  }
  return { anchorJobs, fieldJobs, pageIndex, pages };
}

export default useWoCycle;

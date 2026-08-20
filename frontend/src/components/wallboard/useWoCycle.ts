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
   * The delivered set, ORDER-INSENSITIVE **WITHIN each half of the board** —
   * `anchor_wos.sort()` + `'|'` + `field_wos.sort()`. Order-insensitivity is
   * what survives the real stress case: `running` is position 3 of the server's
   * sort key and flips on every clock-in/out, so at lunch and every shift change
   * nearly the whole shop's flag flips within minutes — which is also when the
   * most people walk past. A reorder INSIDE the frozen field (or inside the
   * anchor row) therefore still rebuilds nothing: no card moves, cards only
   * recolor in place.
   *
   * The `|` split is CORRECTNESS, not tidiness. The anchor row is resolved LIVE
   * while the field is frozen, so a re-sort that carries a job ACROSS that
   * boundary puts the two halves into disagreement in both directions: the job
   * lifted INTO the anchor is nulled out of its frozen field slot (resolution
   * rule 3 below), and the job it DISPLACED is in neither the live top four nor
   * the frozen field list — i.e. rendered on no cell at all. Under a whole-list
   * key that state is invisible (the set never changed) and so it never heals;
   * with the split it is an ordinary set change that heals through case (d),
   * bounded to one dwell and phase-preserving. That is what keeps this hook's
   * load-bearing property — no delivered job silently skipped — true.
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
  const wos = jobs.map(job => job.wo_number);
  const anchor = wos.slice(0, ANCHOR_SLOTS).sort().join(',');
  const field = wos.slice(ANCHOR_SLOTS).sort().join(',');
  return `${anchor}|${field}`;
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
  const anchorJobs = useMemo(() => list.slice(0, ANCHOR_SLOTS), [list]);
  const anchorWos = useMemo(() => new Set(anchorJobs.map(job => job.wo_number)), [anchorJobs]);

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
   *  (e) ORDER changed WITHIN a half      -> NO REBUILD AT ALL. (A reorder that carries a job
   *                                          ACROSS the anchor/field boundary is a KEY change and
   *                                          heals through (d) — see `CyclePlan.key`.)
   *
   * Case (d) defers because it must not disturb A CYCLE IN PROGRESS — so it is
   * gated on there BEING one. Two shapes mean there is not, and both rebuild
   * immediately rather than carrying a stale plan for a whole 22s dwell:
   *
   *  * a SINGLE-PAGE plan (`starts.length <= 1`). There is no page position to
   *    preserve, so deferring buys nothing and costs a FALSE STRIP: WoGrid takes
   *    the delivered count live but the page count from the plan, so a board
   *    crossing 12 -> 13 would keep claiming ALL OPEN WORK ORDERS ON BOARD while
   *    the 13th job was on no screen at all. It also restores what the spec
   *    promises a light board — below 13 delivered jobs a release lands on the
   *    very next poll, exactly as it did before this feature existed.
   *  * the page the board is CURRENTLY SHOWING renders NOTHING — every
   *    wo_number in the live window has either left the payload or been lifted
   *    into the live anchor row. A page of blanks reads as broken and there is
   *    nothing on it to protect. Three earlier carve-outs are special cases of
   *    this one: the board's cold start (the plan holds no field at all, so the
   *    first dwell of every boot would blank rows 2-3), a wholesale population
   *    replacement (no frozen wo_number resolves), and a shrink to at most
   *    ANCHOR_SLOTS jobs (which would otherwise leave a multi-segment page bar
   *    on a board with no field at all).
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
      // No cycle is in progress to protect — heal now, at page 0.
      const shownStart =
        prev.starts[frozen ? 0 : safeMod(slot - prev.anchorSlot, Math.max(1, prev.starts.length))] ?? 0;
      const showsNothing = !fieldWindow(prev.fieldWos, shownStart).some(wo => byWo.has(wo) && !anchorWos.has(wo));
      if (prev.starts.length <= 1 || showsNothing) return buildPlan(key, dept, alarmSnap, fieldWos, slot);
      if (prev.pendingSince === null) return { ...prev, pendingSince: slot }; // (d) arm
      if (prev.pendingSince === slot) return prev; // (d) still inside the same dwell
      return buildPlan(key, dept, alarmSnap, fieldWos, prev.anchorSlot); // (d) fire, phase preserved
    });
  }, [revoked, list, dept, alarmSnap, key, slot, frozen, fieldWos, byWo, anchorWos]);

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

  const fieldJobs = useMemo(() => {
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
      //     field is frozen, so an anchor-crossing reorder can put one job in
      //     both halves for the dwell before the deferred rebuild lands; without
      //     this it would render twice under one React key. It is not "skipped"
      //     — it is on screen, in row 1. (The job it DISPLACED out of the anchor
      //     is what the split plan key exists for: that one really would be on
      //     no cell at all, so it changes the key and heals at the next
      //     boundary rather than staying invisible indefinitely.)
      if (anchorWos.has(wo)) return null;
      return byWo.get(wo) ?? null;
    });
  }, [anchorWos, activeWos, starts, pageIndex, byWo]);

  if (revoked) {
    return { anchorJobs: [], fieldJobs: EMPTY_FIELD.slice(), pageIndex: 0, pages: 1 };
  }
  return { anchorJobs, fieldJobs, pageIndex, pages };
}

export default useWoCycle;

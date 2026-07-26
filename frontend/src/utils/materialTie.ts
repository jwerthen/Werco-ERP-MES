/**
 * Material-tie display helpers (Dispatch Board chip + kiosk deduction notice).
 *
 * Pure and DOM-free on purpose — the arithmetic below mirrors the server's
 * consumption engine, so it is unit-tested directly rather than through a
 * rendered board or a kiosk modal.
 *
 * ---------------------------------------------------------------------------
 * THE ONE RULE THE COPY IN HERE EXISTS TO PROTECT
 * ---------------------------------------------------------------------------
 * Tied material leaves stock when the OPERATION completes — that operation's
 * ties, and only those. `apply_operation_completion_inventory_effects` runs
 * immediately after `finalize_operation_completion` on every operation-completion
 * handler, so a laser child work order (ONE OPERATION PER NEST) deducts nest 1's
 * sheets the moment nest 1 closes, rather than at the end of the job.
 * Work-order completion still runs the whole-work-order reconcile, but that is
 * now the SELF-HEAL — sum-delta means it recomputes `target` and sees
 * `delta == 0` for everything the per-operation post already moved — not the
 * moment stock leaves.
 *
 * IT IS STILL NOT PER RUN, and that is the half a future reader is most likely
 * to "correct" back out. Reporting 3 of 6 runs on a nest that is still open
 * deducts NOTHING: production reporting is deliberately not a trigger. An
 * operation that is still IN_PROGRESS is still REDUCIBLE
 * (`production_reduction_service` refuses a walk-back only once the operation is
 * COMPLETE), and consumption NEVER auto-reverses — a negative delta is a no-op,
 * and the only way back is a supervisor's explicit, reasoned, audited RETURN
 * (PR 3) — so consuming against a still-open operation would strand material
 * behind an office verb the floor cannot reach. Material moves when the
 * operation flips COMPLETE, and at no other moment.
 *
 * So every string this module produces is anchored on THIS OPERATION completing.
 * Two failure directions, both real:
 *  - "when WO-#### finishes" UNDERSTATES. It was true before the engine grew its
 *    per-operation entry point; restoring it tells an operator their nest costs
 *    nothing until the whole job closes, which is now false.
 *  - "per run" / "deducting now" on anything that is not a completion screen
 *    OVER-states, and is the same class of error in the other direction.
 * The two kiosk COMPLETE screens are the one place the copy may speak in the
 * present tense: the operator is about to fire the completion that posts it.
 *
 * Two further facts the copy has to carry, because both read as bugs otherwise:
 *  - the GOOD keypad does not move the number. `/complete` asserts
 *    `quantity_complete = quantity_ordered` regardless of what was keyed, so the
 *    prediction is computed from the ORDERED quantity;
 *  - SCRAP RAISES it. A scrapped run physically used its sheet (posted as ISSUE,
 *    not SCRAP, so lot genealogy keeps it), so keying 2 scrap predicts 2 EXTRA
 *    sheets.
 *
 * Everything here is an ESTIMATE and is labelled as one. Consumption is
 * reconcile-to-target, the operation's quantities can still move before it
 * closes, and a shortage NEVER blocks production — it drives the lot negative
 * and writes an `ALLOCATION_SHORTAGE` audit row.
 *
 * ---------------------------------------------------------------------------
 * WHAT THE RETURN VERB (PR 3) DID TO THE ARITHMETIC IN HERE
 * ---------------------------------------------------------------------------
 * `qty_consumed` used to be MONOTONICALLY NON-DECREASING. A reasoned RETURN
 * lowers it, and that breaks one of the two predictors below — but only one, and
 * only in one state. Working it through, because the wrong fix here is easy and
 * expensive:
 *
 * 1. THE KIOSK PREDICTOR IS ALREADY CORRECT AND WAS NOT TOUCHED.
 *    `predictMaterialConsumption` is TARGET-based — it recomputes the engine's
 *    own `per_run × (complete + scrapped) − qty_consumed`, which is exactly what
 *    the engine will post no matter which direction `qty_consumed` last moved.
 *    A lower `qty_consumed` there yields a LARGER delta, and that larger delta is
 *    genuinely what the next completion will draw. (`materialTie.parity.test.ts`
 *    is what keeps this true; it needed no new case.) A `return_and_untie` is
 *    invisible to it for a different reason: that cancels the tie, and
 *    `material_tie_view` serves OPEN ties only, so the tie leaves the kiosk and
 *    board payloads entirely rather than lingering with a stale forecast.
 *
 * 2. THE BOARD CHIP IS PLAN-BASED, AND THAT IS WHERE THE LIE LIVES.
 *    `qty_remaining` is `qty_planned − qty_consumed` (server-derived, floored at
 *    0) — a PLAN-versus-REPORTED gap, not a forecast. Lower `qty_consumed` and it
 *    rises. On a still-open operation that is harmless and in fact right: ties are
 *    created with `qty_planned == per_run × ordered`, `/complete` asserts
 *    `quantity_complete = quantity_ordered`, so the plan gap and the engine's
 *    delta are the same number and the material really will be drawn. On a
 *    COMPLETE operation it is a straight falsehood: `correct_over_consumption` is
 *    bounded by `qty_consumed − live_target`, so the server leaves the tie at
 *    `qty_consumed >= target` and the engine's delta is `<= 0` FOREVER, on every
 *    path including reconcile-on-read GETs. Nothing further can be drawn, and the
 *    chip would nonetheless announce "deducts N when this operation completes".
 *
 * THE FIX: a terminal-operation guard (`operationCanStillDraw`). When the row's
 * operation is COMPLETE, the chip states what was consumed and explicitly says
 * nothing further can be drawn, instead of forecasting a deduction. It also
 * suppresses the sibling-shortage tier there, because a shortage against a
 * deduction that cannot happen is a purchasing signal made of nothing.
 *
 * WHAT WAS DELIBERATELY *NOT* DONE: capping the live-row estimate at
 * `per_run × quantity_ordered − qty_consumed`. That looks like the tidier fix —
 * make the chip target-based like the kiosk — and it is wrong here, because
 * `DispatchBoardRow` carries `quantity_ordered` and `quantity_complete` but NOT
 * `quantity_scrapped`, and scrap RAISES the target. Capping without it would
 * under-state a scrapped operation's draw, and under-stating turns a real
 * shortage chip green. Over-stating a covered tie is a cosmetic error;
 * under-stating a shortage is the one that teaches a planner to stop trusting the
 * board. So the live-row estimate keeps its plan basis and its "Estimate" label,
 * and only the case that is provably impossible is suppressed.
 *
 * HONEST NOTE ON REACHABILITY: `queued_operations_query` serves READY/IN_PROGRESS
 * operations on non-terminal work orders, so a COMPLETE row does not reach the
 * board through today's payload — the guard is defence in depth against the next
 * caller (a history view, a completed-column board, a cached payload replayed
 * after the operation closed), not a repair of a live screen. It is cheap,
 * cannot under-state, and is the only place in this module that could assert a
 * deduction the engine has already proven it will refuse.
 */

import type { DispatchMaterialTie, MaterialAllocation } from '../types';
import type { KioskMaterialTie } from '../components/kiosk/kioskConstants';

/**
 * Float-residue guard. Quantities are floats end to end (a partial sheet is
 * real), so 0.0000000001 short must never paint a shortage chip.
 *
 * Deliberately LOOSER than the engine's `CONSUMPTION_EPSILON` (1e-9), not equal
 * to it — and the asymmetry only works in this direction. A display threshold
 * above the engine's means the UI can stay silent about a sub-microunit
 * deduction the engine still posts (harmless: nobody acts on 1e-7 of a sheet),
 * but it can never promise material the engine will refuse to move. Tightening
 * this below the engine's value would invert that and is the actual hazard.
 * `materialTie.parity.test.ts` asserts `TIE_EPSILON >= CONSUMPTION_EPSILON`.
 */
export const TIE_EPSILON = 1e-6;

/** Two decimals, trailing zeros stripped: 3 -> "3", 2.5 -> "2.5". */
export function formatTieQty(value: number): string {
  if (!Number.isFinite(value)) return '0';
  return String(Number(value.toFixed(2)));
}

const finite = (value: number | null | undefined): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

/**
 * `COALESCE(qty_per_run, 1.0)` — a NULL on an operation-scoped tie means "not
 * run-scaled", which the engine treats as 1.0 per run.
 */
export const effectivePerRun = (qtyPerRun: number | null | undefined): number =>
  typeof qtyPerRun === 'number' && Number.isFinite(qtyPerRun) ? qtyPerRun : 1.0;

/** Part number, falling back to the id so a chip is never blank. */
const partLabelOf = (tie: { part_number: string | null; part_id: number }): string =>
  (tie.part_number || '').trim() || `Part #${tie.part_id}`;

// ---------------------------------------------------------------------------
// Dispatch Board chip
// ---------------------------------------------------------------------------

/** Three-tier ramp, mirroring the due-date chip's neutral / amber / red. */
export type MaterialTieTone = 'ok' | 'warn' | 'short';

export interface MaterialTieChip {
  tone: MaterialTieTone;
  /** Short label for the dense card line. */
  text: string;
  /** Full, truthful sentence for the chip's `title` — carries the estimate caveat. */
  title: string;
}

/**
 * The slice of a board row the chip reads.
 *
 * Every field is OPTIONAL on purpose. `status` was added by the RETURN verb's
 * terminal-operation guard, and a payload (or fixture) without it must behave
 * exactly as it did before that guard existed — an absent status is treated as
 * "still live", which is what every row the board actually serves is.
 */
export interface MaterialTieChipRow {
  material_tie?: DispatchMaterialTie | null;
  work_order_number?: string;
  /**
   * The OPERATION's status (`DispatchBoardRow.status`). Only `complete` is
   * terminal — `OperationStatus` has no cancelled/skipped member.
   */
  status?: string | null;
}

/**
 * Can a completion still fire on this operation, and therefore can the engine
 * still draw against its ties?
 *
 * `false` ONLY for a status that is definitely terminal. An unknown, absent or
 * unrecognised status reads as "yes" so a new status value can never silently
 * blank a live chip — the failure direction that matters is suppressing a real
 * forecast, not leaving one up a moment too long.
 */
function operationCanStillDraw(status: string | null | undefined): boolean {
  const normalized = (status || '').trim().toLowerCase();
  return normalized !== 'complete' && normalized !== 'completed';
}

/**
 * The board chip for one queued operation, or `null` when the row carries no
 * tie.
 *
 * `null` is load-bearing: an untied operation must render NOTHING — no
 * placeholder, no "not tied" nag — because an untied work order has to stay
 * byte-identical to its pre-feature self.
 *
 * Only OPERATION-scoped ties ever reach the board (the server sends nothing
 * else here); a work-order-scoped tie would fan out across every card of that
 * work order and read as N separate ties. That is also why every sentence below
 * is anchored on "this operation": a dispatch card IS an operation row, and an
 * operation-scoped tie deducts when that operation completes.
 *
 * The tiers:
 *  - `short`  — `short_by > 0`: stock will not cover the remainder and the lot
 *               will be driven negative. Advisory; production is never blocked.
 *  - `warn`   — covered, but with less than one run's worth of margin left over
 *               (the "this is the last of the stock" case).
 *  - `ok`     — covered, already fully consumed, or settled (see below).
 *
 * A COMPLETE operation gets no forecast at all. Its ties can never be drawn
 * against again — after a bounded RETURN the engine's delta is pinned `<= 0`
 * forever — while the plan-based `qty_remaining` this chip reads goes back UP.
 * See the RETURN section of the module docstring.
 */
export function materialTieChip(row: MaterialTieChipRow): MaterialTieChip | null {
  const tie = row.material_tie;
  if (!tie) return null;

  const part = partLabelOf(tie);
  const uom = (tie.unit_of_measure || '').trim();
  const remaining = finite(tie.qty_remaining);
  const shortBy = finite(tie.short_by);
  const onHand = finite(tie.on_hand);
  const canStillDraw = operationCanStillDraw(row.status);
  // A card is one OPERATION, and an operation-scoped tie deducts when that
  // operation completes — not when the work order finishes (that understates a
  // per-nest laser WO) and not per reported run (nothing posts until COMPLETE).
  const whenClause = 'when this operation completes';
  const qty = (value: number) => `${formatTieQty(value)}${uom ? ` ${uom}` : ''}`;
  const lotClause = tie.pinned_lot_number ? ` Pinned to lot ${tie.pinned_lot_number}.` : '';

  // A SIBLING tie is short. The card draws one chip (the lowest allocation id),
  // so an operation carrying two tied parts can have its shortage land on the
  // tie there was no room to render. `any_short` is computed server-side across
  // ALL of the operation's ties, so surface it here — otherwise the chip reads
  // "covered" and `countShortTies` under-counts the column, which is the
  // failure direction that teaches a planner to stop trusting the indicator.
  // `|| 1` covers a pre-feature payload with no `tie_count`: one chip, no siblings.
  const otherCount = Math.max(0, (finite(tie.tie_count) || 1) - 1);

  // ---------------------------------------------------------------------------
  // Terminal operation: state the record, forecast NOTHING.
  //
  // Placed above every other tier — including the two shortage tiers — because
  // all of them are claims about a FUTURE deduction, and on a closed operation
  // there is no future deduction to be short of. `short_by` is derived from the
  // same plan-based `qty_remaining` that a RETURN pushes back up, so leaving the
  // shortage tiers reachable here would let a returned quantity manufacture a
  // purchasing signal out of material that is already back on the shelf.
  // ---------------------------------------------------------------------------
  if (!canStillDraw) {
    return {
      tone: 'ok',
      text: `${part} · settled`,
      title:
        `This operation is complete: ${qty(finite(tie.qty_consumed))} of ${part} reported consumed, and ` +
        `nothing further can be drawn against this tie. A returned quantity does not re-arm it — the ` +
        `engine's delta stays at or below zero on every path. Reported total; the inventory ledger is the ` +
        `authoritative record.${lotClause}`,
    };
  }

  if (tie.any_short && shortBy <= TIE_EPSILON && otherCount > 0) {
    return {
      tone: 'short',
      text: `${part} +${otherCount} · short`,
      title:
        `${part} is covered, but this operation carries ${otherCount} more tied ` +
        `${otherCount === 1 ? 'part that is' : 'parts, at least one of which is'} short of stock. ` +
        `Open the work order's Materials section for the full list. A shortage never blocks ` +
        `production; the lot is driven negative and flagged for purchasing.${lotClause}`,
    };
  }

  // Nothing left to deplete against PLAN. `qty_consumed` is a CACHE — the ledger
  // (inventory_transactions.allocation_id) is the authoritative total, so the
  // tooltip says "reported" rather than asserting the ledger's answer.
  //
  // The figure quoted is `qty_consumed`, NOT `qty_planned`. This branch is
  // reached whenever `qty_consumed >= qty_planned`, and over-consumption
  // (`consumed > target`, so `consumed > planned` too) became an ordinary steady
  // state once material started posting per operation — quoting the plan would
  // under-report the real draw by exactly the amount a supervisor is looking at
  // when they open the RETURN dialog. The two agree in the ordinary case.
  if (remaining <= TIE_EPSILON) {
    return {
      tone: 'ok',
      text: `${part} · issued`,
      title:
        `${qty(finite(tie.qty_consumed))} of ${part} reported consumed — nothing further deducts ` +
        `${whenClause}. Reported total; the inventory ledger is the authoritative record.${lotClause}`,
    };
  }

  if (shortBy > TIE_EPSILON) {
    return {
      tone: 'short',
      text: `Short ${qty(shortBy)} · ${part}`,
      title:
        `Estimate: deducts ${qty(remaining)} of ${part} ${whenClause}, but only ${qty(onHand)} is on hand ` +
        `— short ${qty(shortBy)}. A shortage never blocks production; the lot is driven negative and ` +
        `flagged for purchasing.${lotClause}`,
    };
  }

  // "Last of the stock": covered, but after this tie draws there is less than
  // one more run's worth left on the shelf.
  if (onHand - remaining < effectivePerRun(tie.qty_per_run) - TIE_EPSILON) {
    return {
      tone: 'warn',
      text: `${qty(remaining)} · ${part} · last of stock`,
      title:
        `Estimate: deducts ${qty(remaining)} of ${part} ${whenClause}. That is nearly all of the ` +
        `${qty(onHand)} on hand — less than one run's worth would remain.${lotClause}`,
    };
  }

  return {
    tone: 'ok',
    text: `${qty(remaining)} · ${part}`,
    title: `Estimate: deducts ${qty(remaining)} of ${part} ${whenClause}. ${qty(onHand)} on hand.${lotClause}`,
  };
}

/**
 * How many rows in a column carry a SHORT tie — the per-column rollup next to
 * the changeover summary. Recomputed from the queue on every render so it stays
 * correct through an optimistic reorder.
 *
 * Delegates to `materialTieChip` rather than reading `short_by` itself, so the
 * rollup and the chips can never disagree — including on the terminal-operation
 * guard, where a settled tie must not be counted toward a purchasing signal.
 */
export function countShortTies(rows: readonly MaterialTieChipRow[]): number {
  return rows.reduce((total, row) => (materialTieChip(row)?.tone === 'short' ? total + 1 : total), 0);
}

// ---------------------------------------------------------------------------
// Over-consumption (the open loop a reduce can leave behind)
// ---------------------------------------------------------------------------

/** The two operation quantities the consumption target is computed from. */
export interface TieTargetOperation {
  quantity_complete?: number | null;
  quantity_scrapped?: number | null;
}

/**
 * How much material this tie holds that its production record no longer justifies —
 * `max(0, qty_consumed - target)`. `0` means squared up.
 *
 * **Why this exists.** The office reduce verb can lower a COMPLETE operation's
 * quantities, which drops `target` below `qty_consumed`. The material is still out on
 * the floor and the ledger still says so — correctly — but nothing forces the
 * supervisor to return it, no event fires, and an over-consumed tie is otherwise
 * indistinguishable from an ordinary one. It is the single point where the reduce
 * relaxation's safety rests on a human remembering, so the human has to be shown it.
 *
 * The target is the ENGINE's own formula, not an approximation of it:
 * operation-scoped ties reconcile to `qty_per_run x (complete + scrapped)`, while a
 * work-order-scoped tie drains against `qty_planned` in the one-shot backflush. That
 * parity is asserted against the backend in `materialTie.parity.test.ts` — if the
 * engine's arithmetic ever moves, fix it HERE, not by adding a second copy elsewhere.
 *
 * Returns `null` when it cannot be known rather than guessing `0`: an operation-scoped
 * tie whose operation was not supplied (or was detached by a nest re-import) has no
 * target to measure against, and a confident "squared up" would be a worse answer than
 * an absent one.
 */
export function overConsumedQty(
  tie: Pick<MaterialAllocation, 'qty_per_run' | 'qty_planned' | 'qty_consumed' | 'work_order_operation_id'>,
  operation: TieTargetOperation | null | undefined
): number | null {
  const consumed = finite(tie.qty_consumed);
  let target: number;
  if (tie.work_order_operation_id == null) {
    // Work-order-scoped: drained once against the plan by the backflush leg.
    target = finite(tie.qty_planned);
  } else {
    if (!operation) return null;
    target =
      effectivePerRun(tie.qty_per_run) * (finite(operation.quantity_complete) + finite(operation.quantity_scrapped));
  }
  const over = consumed - target;
  return over > TIE_EPSILON ? over : 0;
}

// ---------------------------------------------------------------------------
// Kiosk deduction prediction
// ---------------------------------------------------------------------------

/** One part's worth of predicted consumption, summed across the ties on it. */
export interface ConsumptionPredictionLine {
  /** Stable React key: part + pinned lot (different pins are different pools). */
  key: string;
  partLabel: string;
  partName: string | null;
  unitOfMeasure: string;
  /** Predicted positive delta for this part (already summed, floored at 0). */
  qty: number;
  /** Stock the tie draws from — the pinned lot when pinned, else the part total. */
  onHand: number;
  /** `max(0, qty - onHand)`. Advisory only; a shortage never blocks the job. */
  shortBy: number;
  pinnedLotNumber: string | null;
}

export interface ConsumptionPrediction {
  lines: ConsumptionPredictionLine[];
  /** Total predicted units across every line (mixed UoM — display only). */
  total: number;
  /** How much of `total` is attributable to the scrap keyed on this screen. */
  scrapAdds: number;
  anyShort: boolean;
}

export interface ConsumptionPredictionInput {
  ties: readonly KioskMaterialTie[] | null | undefined;
  /**
   * The operation's ORDERED/target quantity. `/complete` asserts
   * `quantity_complete = quantity_ordered`, so the GOOD keypad never moves the
   * prediction — this is what the completion will actually record.
   */
  quantityOrdered: number | null | undefined;
  /** The OPERATION's scrap total already recorded. NEVER this session's count. */
  operationScrapped: number | null | undefined;
  /** Scrap keyed on this screen, not yet posted. Raises the prediction. */
  scrapEntered: number | null | undefined;
}

/** Sum-delta target for one tie: `per_run × (final complete + final scrapped)`. */
function predictedDelta(tie: KioskMaterialTie, finalComplete: number, finalScrapped: number): number {
  const perRun = effectivePerRun(tie.qty_per_run);
  const target = perRun * (finalComplete + finalScrapped);
  // Mirrors the engine: a NEGATIVE delta is a NO-OP, never an auto-reversal.
  return Math.max(0, target - finite(tie.qty_consumed));
}

function sumDeltas(ties: readonly KioskMaterialTie[], finalComplete: number, finalScrapped: number): number {
  return ties.reduce((total, tie) => total + predictedDelta(tie, finalComplete, finalScrapped), 0);
}

/**
 * What completing this operation is expected to take out of stock — posted by
 * **that completion**, not by the runs reported along the way and not deferred
 * to the work order's own completion.
 *
 * Mirrors `material_consumption_service._consume_one_allocation`:
 *
 * ```
 * finalComplete  = quantity_ordered                    // what /complete asserts
 * finalScrapped  = operationScrapped + scrapEntered
 * target         = (qty_per_run ?? 1.0) * (finalComplete + finalScrapped)
 * predictedDelta = max(0, target - qty_consumed)       // per tie; summed per part
 * ```
 *
 * `finalComplete` is the ORDERED quantity and stays so under per-operation
 * timing: `/complete` asserts `quantity_complete = quantity_ordered` and the
 * clock-out path clamps at the same target, so at the instant the operation
 * flips COMPLETE — which is now the instant consumption posts — the engine reads
 * exactly that number.
 *
 * `qty_consumed` is an input for a reason: leaving it out over-states a
 * partially-consumed tie (an earlier completion, a replay, or a reconcile-on-read
 * GET may already have posted against it). It is a CACHE (the ledger is
 * authoritative), which is one more reason this is presented as an estimate.
 *
 * Returns `null` when there are no ties — an untied operation renders NOTHING.
 */
export function predictMaterialConsumption(input: ConsumptionPredictionInput): ConsumptionPrediction | null {
  const ties = (input.ties || []).filter(Boolean);
  if (ties.length === 0) return null;

  const finalComplete = Math.max(0, finite(input.quantityOrdered));
  const alreadyScrapped = Math.max(0, finite(input.operationScrapped));
  const keyedScrap = Math.max(0, finite(input.scrapEntered));
  const finalScrapped = alreadyScrapped + keyedScrap;

  // Pinned ties draw from their own lot, so a part with two different pins is
  // two distinct pools; grouping on part+lot keeps `onHand` from double-counting.
  const groups = new Map<string, ConsumptionPredictionLine>();
  ties.forEach((tie) => {
    const qty = predictedDelta(tie, finalComplete, finalScrapped);
    const key = `${tie.part_id}::${tie.pinned_lot_number ?? ''}`;
    const existing = groups.get(key);
    if (existing) {
      existing.qty += qty;
      return;
    }
    groups.set(key, {
      key,
      partLabel: partLabelOf(tie),
      partName: tie.part_name,
      unitOfMeasure: (tie.unit_of_measure || '').trim(),
      qty,
      onHand: finite(tie.on_hand),
      shortBy: 0,
      pinnedLotNumber: tie.pinned_lot_number,
    });
  });

  const lines = Array.from(groups.values()).filter((line) => line.qty > TIE_EPSILON);
  lines.forEach((line) => {
    line.shortBy = Math.max(0, line.qty - line.onHand);
  });

  const total = lines.reduce((sum, line) => sum + line.qty, 0);
  // Difference of the two full sums rather than `perRun × scrapEntered`: the
  // per-tie `max(0, …)` clamp makes those disagree on an over-consumed tie.
  const withoutKeyedScrap = sumDeltas(ties, finalComplete, alreadyScrapped);
  const scrapAdds = Math.max(0, total - Math.max(0, withoutKeyedScrap));

  return {
    lines,
    total,
    scrapAdds,
    anyShort: lines.some((line) => line.shortBy > TIE_EPSILON),
  };
}

// ---------------------------------------------------------------------------
// Kiosk copy builders — shared so both completion modals say the SAME thing
// ---------------------------------------------------------------------------

/**
 * The notice heading, shown on the two kiosk COMPLETE screens.
 *
 * Those screens are the one place this module may speak in the present tense:
 * the operator is one tap from firing the completion that posts the deduction,
 * so it is THIS operation's completion that moves the stock. The work order is
 * still named — a crew station confirms a badge scan against a job label, and
 * "on WO-####" is the context that makes the sentence checkable — but it is
 * context, never the trigger.
 */
export function deductionHeadline(workOrderNumber: string | null | undefined): string {
  const wo = (workOrderNumber || '').trim();
  return wo
    ? `Material — deducts when you complete this operation on ${wo}`
    : 'Material — deducts when you complete this operation';
}

/**
 * "2 sheets" → `2 EA · SHT-.125-304` (+ the lot when the tie is pinned).
 *
 * Deliberately carries NO timing word. The heading above it and
 * `DEDUCTION_TIMING_NOTE` below it own that one fact between them, so there is
 * exactly one place to change when the trigger moves (it just did) rather than
 * a third string to leave behind saying something older.
 */
export function deductionLineText(line: ConsumptionPredictionLine): string {
  const qty = `${formatTieQty(line.qty)}${line.unitOfMeasure ? ` ${line.unitOfMeasure}` : ''}`;
  const lot = line.pinnedLotNumber ? ` · lot ${line.pinnedLotNumber}` : '';
  return `${qty} · ${line.partLabel}${lot}`;
}

/**
 * The line that stops "why did my scrap raise the material?" becoming a ticket.
 * `null` when no scrap was keyed on this screen.
 */
export function scrapNoteText(prediction: ConsumptionPrediction, scrapEntered: number | null | undefined): string | null {
  const keyed = Math.max(0, finite(scrapEntered));
  if (keyed <= 0 || prediction.scrapAdds <= TIE_EPSILON) return null;
  return (
    `Includes +${formatTieQty(prediction.scrapAdds)} for the ${formatTieQty(keyed)} scrap you entered — ` +
    `a scrapped run still used its material.`
  );
}

/** The amber shortage line, or `null` when stock covers every line. */
export function shortageNoteText(prediction: ConsumptionPrediction): string | null {
  const shortLines = prediction.lines.filter((line) => line.shortBy > TIE_EPSILON);
  if (shortLines.length === 0) return null;
  const detail = shortLines
    .map((line) => `${line.partLabel} short ${formatTieQty(line.shortBy)}${line.unitOfMeasure ? ` ${line.unitOfMeasure}` : ''}`)
    .join(' · ');
  return `Stock may not cover it — ${detail}. This never blocks the job; tell your supervisor.`;
}

/**
 * The timing disclaimer. Shown verbatim on both completion screens because it
 * carries the two facts an operator gets wrong in OPPOSITE directions:
 *  - finishing the whole job is NOT a prerequisite — this operation completing
 *    is what posts it (nest 1 of 3 deducts nest 1's sheets);
 *  - reporting runs along the way posts NOTHING, so the number does not tick
 *    down as pieces are called in.
 */
export const DEDUCTION_TIMING_NOTE =
  'Estimate — this leaves stock when the operation completes, not as each run is reported.';

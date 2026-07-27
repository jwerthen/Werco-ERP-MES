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
 * Consumption fires at WORK-ORDER completion, NEVER per run. Every
 * `apply_completion_inventory_effects` call site sits inside
 * `if work_order_completed:`. A laser child work order carries ONE OPERATION
 * PER NEST, so finishing nest 1 of 3 deducts NOTHING — all three flush when the
 * last operation closes the work order.
 *
 * So every string this module produces says "deducts N when WO-#### finishes".
 * Never "this will deduct N", never "deducting now". When in doubt, under-claim:
 * wrong copy on a shop floor is a real defect, not a wording nit.
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
 * reconcile-to-target, quantities can still move before the work order closes,
 * and a shortage NEVER blocks production — it drives the lot negative and writes
 * an `ALLOCATION_SHORTAGE` audit row.
 */

import type { DispatchBoardRow } from '../types';
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
 * The board chip for one queued operation, or `null` when the row carries no
 * tie.
 *
 * `null` is load-bearing: an untied operation must render NOTHING — no
 * placeholder, no "not tied" nag — because an untied work order has to stay
 * byte-identical to its pre-feature self.
 *
 * Only OPERATION-scoped ties ever reach the board (the server sends nothing
 * else here); a work-order-scoped tie would fan out across every card of that
 * work order and read as N separate ties.
 *
 * The tiers:
 *  - `short`  — `short_by > 0`: stock will not cover the remainder and the lot
 *               will be driven negative. Advisory; production is never blocked.
 *  - `warn`   — covered, but with less than one run's worth of margin left over
 *               (the "this is the last of the stock" case).
 *  - `ok`     — covered, or already fully consumed.
 */
export function materialTieChip(
  row: Pick<DispatchBoardRow, 'material_tie' | 'work_order_number'>
): MaterialTieChip | null {
  const tie = row.material_tie;
  if (!tie) return null;

  const part = partLabelOf(tie);
  const uom = (tie.unit_of_measure || '').trim();
  const remaining = finite(tie.qty_remaining);
  const shortBy = finite(tie.short_by);
  const onHand = finite(tie.on_hand);
  const wo = (row.work_order_number || '').trim();
  const whenClause = wo ? `when ${wo} finishes` : 'when this work order finishes';
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

  // Nothing left to deplete. `qty_consumed` is a CACHE — the ledger
  // (inventory_transactions.allocation_id) is the authoritative total, so the
  // tooltip says "reported" rather than asserting the ledger's answer.
  if (remaining <= TIE_EPSILON) {
    return {
      tone: 'ok',
      text: `${part} · issued`,
      title:
        `${qty(finite(tie.qty_planned))} of ${part} reported consumed — nothing further deducts ` +
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
 */
export function countShortTies(rows: readonly Pick<DispatchBoardRow, 'material_tie' | 'work_order_number'>[]): number {
  return rows.reduce((total, row) => (materialTieChip(row)?.tone === 'short' ? total + 1 : total), 0);
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
 * What completing this operation is expected to take out of stock — **when the
 * WORK ORDER finishes**, not now.
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
 * `qty_consumed` is an input for a reason: leaving it out over-states a
 * partially-consumed tie. It is a CACHE (the ledger is authoritative), which is
 * one more reason this is presented as an estimate.
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
 * The notice heading. Names the work order, because the whole point is that the
 * deduction is deferred to that work order's completion, not this operation's.
 */
export function deductionHeadline(workOrderNumber: string | null | undefined): string {
  const wo = (workOrderNumber || '').trim();
  return wo ? `Material — deducts when ${wo} finishes` : 'Material — deducts when this work order finishes';
}

/** "2 sheets" → `2 EA · SHT-.125-304` (+ the lot when the tie is pinned). */
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
 * is the single fact an operator is most likely to get wrong: finishing THIS
 * operation does not move stock unless it is the one that closes the work order.
 */
export const DEDUCTION_TIMING_NOTE =
  'Estimate — nothing leaves stock until the last operation on this work order completes.';

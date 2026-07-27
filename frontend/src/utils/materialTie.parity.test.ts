/**
 * PARITY: the sum-delta formula now exists in two languages.
 *
 * `predictMaterialConsumption` (TypeScript, `utils/materialTie.ts`) predicts what
 * `_consume_one_allocation` (Python,
 * `backend/app/services/material_consumption_service.py`) will actually post. Two
 * independent implementations of one arithmetic rule drift silently: the kiosk
 * would quietly start promising an operator a different number of sheets than the
 * ledger takes, and nothing would fail.
 *
 * So the backend rule is TRANSCRIBED here as a reference implementation, kept
 * beside the source it mirrors, and both are driven through the same case table.
 * The backend body, verbatim:
 *
 * ```python
 * good = float(operation.quantity_complete or 0)
 * scrapped = float(operation.quantity_scrapped or 0)
 * # COALESCE(qty_per_run, 1.0)
 * per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
 * target = per_run * (good + scrapped)
 * delta = target - float(allocation.qty_consumed or 0)
 * if delta <= _EPSILON:
 *     # the reduce-over-count case: a NO-OP, never an auto-reversal
 *     return
 * ```
 *
 * When this file fails, the fix is to bring the TWO implementations back into
 * agreement — not to adjust the reference below to match whatever the client
 * happens to do.
 *
 * ONE DELIBERATE DIFFERENCE, asserted at the bottom: the client's float-residue
 * threshold is LOOSER than the engine's (1e-6 vs 1e-9). That direction is the safe
 * one — the UI can suppress a sub-microunit deduction the engine still posts, but
 * it can never promise a deduction the engine would skip.
 */

import { TIE_EPSILON, effectivePerRun, predictMaterialConsumption } from './materialTie';
import type { KioskMaterialTie } from '../components/kiosk/kioskConstants';

/** `material_consumption_service._EPSILON` (defined in `completion_inventory_service`). */
const BACKEND_EPSILON = 1e-9;

/**
 * Reference transcription of `_consume_one_allocation`'s target/delta arithmetic.
 * Returns the quantity the ENGINE would post (0 when it no-ops).
 */
function backendConsumptionDelta(args: {
  qtyPerRun: number | null;
  quantityComplete: number;
  quantityScrapped: number;
  qtyConsumed: number;
}): number {
  const good = args.quantityComplete || 0;
  const scrapped = args.quantityScrapped || 0;
  // COALESCE(qty_per_run, 1.0) — a NULL on an operation-scoped row means "not
  // run-scaled" per the model docstring.
  const perRun = args.qtyPerRun !== null && args.qtyPerRun !== undefined ? args.qtyPerRun : 1.0;
  const target = perRun * (good + scrapped);
  const delta = target - (args.qtyConsumed || 0);
  // Includes the reduce-over-count case (target fell below what was consumed):
  // a NO-OP, never an auto-reversal.
  if (delta <= BACKEND_EPSILON) return 0;
  return delta;
}

const tie = (overrides: Partial<KioskMaterialTie> = {}): KioskMaterialTie => ({
  allocation_id: 1,
  part_id: 55,
  part_number: 'SHT-.125-304',
  part_name: '.125 304 sheet',
  unit_of_measure: 'EA',
  qty_per_run: 1,
  qty_planned: 5,
  qty_consumed: 0,
  qty_remaining: 5,
  on_hand: 1_000_000,
  short_by: 0,
  pinned_lot_number: null,
  ...overrides,
});

/**
 * The client's `finalComplete` is the ORDERED quantity, because `/complete`
 * asserts `quantity_complete = quantity_ordered` regardless of what was keyed —
 * so the engine's `operation.quantity_complete` at consume time IS that number.
 * `finalScrapped` is the operation's recorded scrap plus what is on the keypad.
 */
interface Case {
  name: string;
  qtyPerRun: number | null;
  quantityOrdered: number;
  operationScrapped: number;
  scrapEntered: number;
  qtyConsumed: number;
}

const CASES: Case[] = [
  { name: 'the headline nest: one sheet per run, nothing consumed yet', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'a NULL per-run reads as 1.0', qtyPerRun: null, quantityOrdered: 4, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'partly consumed across sessions', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 2 },
  { name: 'scrap already recorded on the operation raises the target', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 3, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'scrap keyed on this screen raises it too', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 2, qtyConsumed: 0 },
  { name: 'both scrap sources accumulate', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 3, scrapEntered: 2, qtyConsumed: 1 },
  { name: 'multiple sheets per run', qtyPerRun: 3, quantityOrdered: 4, operationScrapped: 1, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'a fractional per-run (half a sheet is real)', qtyPerRun: 0.5, quantityOrdered: 7, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'fully consumed against target: no-op', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 5 },
  { name: 'OVER-consumed (the walked-back over-count): no-op, never a reversal', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 12 },
  { name: 'over-consumed but scrap pushes the target back above it', qtyPerRun: 1, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 2, qtyConsumed: 6 },
  { name: 'zero ordered and zero scrap', qtyPerRun: 1, quantityOrdered: 0, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'scrap only (every run scrapped — the sheets were still cut)', qtyPerRun: 1, quantityOrdered: 0, operationScrapped: 4, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'per-run 0 is a real value, not "unset"', qtyPerRun: 0, quantityOrdered: 5, operationScrapped: 2, scrapEntered: 0, qtyConsumed: 0 },
  { name: 'float residue: consumed is a hair under target', qtyPerRun: 1, quantityOrdered: 3, operationScrapped: 0, scrapEntered: 0, qtyConsumed: 2.9999999999 },
];

describe('sum-delta parity with material_consumption_service._consume_one_allocation', () => {
  it.each(CASES)('$name', (testCase) => {
    const expected = backendConsumptionDelta({
      qtyPerRun: testCase.qtyPerRun,
      // What /complete will assert, and therefore what the engine will read.
      quantityComplete: testCase.quantityOrdered,
      quantityScrapped: testCase.operationScrapped + testCase.scrapEntered,
      qtyConsumed: testCase.qtyConsumed,
    });

    const prediction = predictMaterialConsumption({
      ties: [tie({ qty_per_run: testCase.qtyPerRun, qty_consumed: testCase.qtyConsumed })],
      quantityOrdered: testCase.quantityOrdered,
      operationScrapped: testCase.operationScrapped,
      scrapEntered: testCase.scrapEntered,
    })!;

    expect(prediction).not.toBeNull();
    expect(prediction.total).toBeCloseTo(expected, 9);
  });

  it('agrees across a wide randomized sweep, not just the curated table', () => {
    // Deterministic LCG — a seeded sweep catches a sign/clamp divergence the
    // hand-picked cases above could miss, without making the suite flaky.
    let seed = 20260725;
    const next = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };

    for (let i = 0; i < 500; i += 1) {
      const qtyPerRun = next() < 0.15 ? null : Math.round(next() * 400) / 100;
      const quantityOrdered = Math.round(next() * 50);
      const operationScrapped = Math.round(next() * 10);
      const scrapEntered = Math.round(next() * 5);
      const qtyConsumed = Math.round(next() * 6000) / 100;

      const expected = backendConsumptionDelta({
        qtyPerRun,
        quantityComplete: quantityOrdered,
        quantityScrapped: operationScrapped + scrapEntered,
        qtyConsumed,
      });
      const prediction = predictMaterialConsumption({
        ties: [tie({ qty_per_run: qtyPerRun, qty_consumed: qtyConsumed })],
        quantityOrdered,
        operationScrapped,
        scrapEntered,
      })!;

      // The client drops a sub-epsilon line entirely; the engine's residue band is
      // narrower. Both are "nothing worth showing", so compare within the CLIENT's
      // threshold — the asymmetry itself is asserted separately below.
      expect(Math.abs(prediction.total - expected)).toBeLessThanOrEqual(TIE_EPSILON);
    }
  });

  it('COALESCEs a null per-run exactly the way the engine does', () => {
    expect(effectivePerRun(null)).toBe(1.0);
    expect(
      backendConsumptionDelta({ qtyPerRun: null, quantityComplete: 3, quantityScrapped: 0, qtyConsumed: 0 })
    ).toBe(3);
  });

  it('never predicts a reversal, because the engine never posts one', () => {
    // A supervisor walking back an over-count drives the delta negative. The
    // engine no-ops (the sheet is already cut; un-consuming is an explicit,
    // reasoned, audited compensating transaction). The client must not show a
    // credit either.
    for (const qtyConsumed of [6, 10, 100, 1e6]) {
      const prediction = predictMaterialConsumption({
        ties: [tie({ qty_consumed: qtyConsumed })],
        quantityOrdered: 5,
        operationScrapped: 0,
        scrapEntered: 0,
      })!;
      expect(prediction.total).toBe(0);
      expect(prediction.lines).toHaveLength(0);
      expect(
        backendConsumptionDelta({
          qtyPerRun: 1,
          quantityComplete: 5,
          quantityScrapped: 0,
          qtyConsumed,
        })
      ).toBe(0);
    }
  });

  it('keeps the client threshold LOOSER than the engine, never stricter', () => {
    // The one deliberate divergence. Looser means the UI can stay silent about a
    // deduction the engine still posts (sub-microunit, invisible on a shop
    // screen); STRICTER would mean promising an operator material the engine
    // refuses to move, which is the direction that must never be introduced.
    expect(TIE_EPSILON).toBeGreaterThanOrEqual(BACKEND_EPSILON);
  });
});

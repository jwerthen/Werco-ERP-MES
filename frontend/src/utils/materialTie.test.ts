/**
 * Material-tie display helpers.
 *
 * These are the pure rules behind the Dispatch Board chip and the kiosk
 * deduction notice, so they are asserted here rather than through a rendered
 * board or modal. The load-bearing cases:
 *  - an UNTIED row/operation produces NOTHING (null), never a placeholder;
 *  - the shortage tier is advisory and mirrors the server's `short_by`;
 *  - the kiosk prediction mirrors `material_consumption_service`: the GOOD
 *    keypad never moves it (the ordered quantity is what /complete asserts),
 *    SCRAP raises it, `qty_consumed` is subtracted, and a negative delta is a
 *    no-op rather than an auto-reversal;
 *  - every generated sentence is worded for WORK-ORDER completion.
 */

import {
  DEDUCTION_TIMING_NOTE,
  countShortTies,
  deductionHeadline,
  deductionLineText,
  effectivePerRun,
  formatTieQty,
  materialTieChip,
  predictMaterialConsumption,
  scrapNoteText,
  shortageNoteText,
} from './materialTie';
import type { DispatchBoardRow, DispatchMaterialTie } from '../types';
import type { KioskMaterialTie } from '../components/kiosk/kioskConstants';

const makeTie = (overrides: Partial<DispatchMaterialTie> = {}): DispatchMaterialTie => ({
  allocation_id: 1,
  part_id: 55,
  part_number: 'SHT-.125-304',
  unit_of_measure: 'EA',
  qty_per_run: 1,
  qty_planned: 3,
  qty_consumed: 0,
  qty_remaining: 3,
  on_hand: 10,
  short_by: 0,
  pinned_inventory_item_id: null,
  pinned_lot_number: null,
  ...overrides,
});

const makeRow = (
  tie?: DispatchMaterialTie | null
): Pick<DispatchBoardRow, 'material_tie' | 'work_order_number'> => ({
  work_order_number: 'WO-2026-0142',
  material_tie: tie ?? null,
});

const makeKioskTie = (overrides: Partial<KioskMaterialTie> = {}): KioskMaterialTie => ({
  allocation_id: 1,
  part_id: 55,
  part_number: 'SHT-.125-304',
  part_name: '.125 304 sheet',
  unit_of_measure: 'EA',
  qty_per_run: 1,
  qty_planned: 5,
  qty_consumed: 0,
  qty_remaining: 5,
  on_hand: 50,
  short_by: 0,
  pinned_lot_number: null,
  ...overrides,
});

describe('formatTieQty / effectivePerRun', () => {
  it('trims float noise to two decimals without rounding a partial away', () => {
    expect(formatTieQty(3)).toBe('3');
    expect(formatTieQty(2.5)).toBe('2.5');
    expect(formatTieQty(2.9000000000000004)).toBe('2.9');
  });

  it('COALESCEs a null qty_per_run to 1.0, matching the engine', () => {
    expect(effectivePerRun(null)).toBe(1);
    expect(effectivePerRun(undefined)).toBe(1);
    expect(effectivePerRun(0.5)).toBe(0.5);
    // 0 is a real value, not "unset" — it must not become 1.
    expect(effectivePerRun(0)).toBe(0);
  });
});

describe('materialTieChip', () => {
  it('returns null for an untied row so nothing at all renders', () => {
    expect(materialTieChip(makeRow(null))).toBeNull();
    expect(materialTieChip({ work_order_number: 'WO-1' })).toBeNull();
  });

  it('renders a neutral chip with the remaining quantity and part when covered', () => {
    const chip = materialTieChip(makeRow(makeTie()));
    expect(chip).not.toBeNull();
    expect(chip!.tone).toBe('ok');
    expect(chip!.text).toBe('3 EA · SHT-.125-304');
  });

  it('words the tooltip for WORK-ORDER completion and flags it as an estimate', () => {
    const chip = materialTieChip(makeRow(makeTie()))!;
    expect(chip.title).toContain('when WO-2026-0142 finishes');
    expect(chip.title.toLowerCase()).toContain('estimate');
    // Never claims the deduction is happening now.
    expect(chip.title.toLowerCase()).not.toContain('deducting');
  });

  it('goes SHORT on short_by > 0 and says a shortage never blocks production', () => {
    const chip = materialTieChip(makeRow(makeTie({ on_hand: 1, short_by: 2 })))!;
    expect(chip.tone).toBe('short');
    expect(chip.text).toBe('Short 2 EA · SHT-.125-304');
    expect(chip.title).toContain('never blocks production');
  });

  it('warns when covered with less than one run of margin left', () => {
    // remaining 3, on hand 3.5, per-run 1 => 0.5 margin.
    const chip = materialTieChip(makeRow(makeTie({ on_hand: 3.5 })))!;
    expect(chip.tone).toBe('warn');
    expect(chip.text).toContain('last of stock');
  });

  it('stays neutral once a full run of margin remains', () => {
    expect(materialTieChip(makeRow(makeTie({ on_hand: 4 })))!.tone).toBe('ok');
  });

  it('reports a fully consumed tie as issued, and calls the total REPORTED', () => {
    const chip = materialTieChip(makeRow(makeTie({ qty_consumed: 3, qty_remaining: 0 })))!;
    expect(chip.tone).toBe('ok');
    expect(chip.text).toBe('SHT-.125-304 · issued');
    expect(chip.title).toContain('reported consumed');
    expect(chip.title).toContain('ledger is the authoritative record');
  });

  it('does not paint a shortage from float residue', () => {
    const chip = materialTieChip(makeRow(makeTie({ short_by: 1e-9, on_hand: 10 })))!;
    expect(chip.tone).toBe('ok');
  });

  it('names the pinned lot so a lot-directed tie is visible on the board', () => {
    const chip = materialTieChip(makeRow(makeTie({ pinned_lot_number: 'HEAT-7741' })))!;
    expect(chip.title).toContain('HEAT-7741');
  });

  it('falls back to the part id rather than rendering a blank chip', () => {
    const chip = materialTieChip(makeRow(makeTie({ part_number: null })))!;
    expect(chip.text).toContain('Part #55');
  });
});

describe('countShortTies', () => {
  it('counts only the short rows and ignores untied ones', () => {
    const rows = [
      makeRow(makeTie({ short_by: 2, on_hand: 1 })),
      makeRow(makeTie({ allocation_id: 2 })),
      makeRow(null),
      makeRow(makeTie({ allocation_id: 3, short_by: 5, on_hand: 0 })),
    ];
    expect(countShortTies(rows)).toBe(2);
    expect(countShortTies([])).toBe(0);
  });
});

describe('predictMaterialConsumption', () => {
  it('returns null for an untied operation', () => {
    expect(
      predictMaterialConsumption({ ties: null, quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0 })
    ).toBeNull();
    expect(
      predictMaterialConsumption({ ties: [], quantityOrdered: 5, operationScrapped: 0, scrapEntered: 0 })
    ).toBeNull();
  });

  it('predicts per_run x (ordered + scrapped) minus what already consumed', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie({ qty_per_run: 1, qty_consumed: 2 })],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 0,
    })!;
    // target 5, consumed 2 => 3
    expect(result.total).toBe(3);
    expect(result.lines).toHaveLength(1);
    expect(result.lines[0].qty).toBe(3);
  });

  it('is NOT moved by the good keypad — the ordered quantity is what /complete asserts', () => {
    const base = { ties: [makeKioskTie()], operationScrapped: 0, scrapEntered: 0 };
    // The caller always passes quantity_ordered; there is no "good" input at all.
    expect(predictMaterialConsumption({ ...base, quantityOrdered: 5 })!.total).toBe(5);
    expect(predictMaterialConsumption({ ...base, quantityOrdered: 5 })!.total).toBe(5);
  });

  it('RAISES the prediction for keyed scrap and attributes the increase', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie()],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 2,
    })!;
    expect(result.total).toBe(7);
    expect(result.scrapAdds).toBe(2);
    expect(scrapNoteText(result, 2)).toContain('+2');
    expect(scrapNoteText(result, 2)).toContain('still used its material');
  });

  it('adds the operation scrap already recorded, not just what was keyed', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie()],
      quantityOrdered: 5,
      operationScrapped: 3,
      scrapEntered: 0,
    })!;
    expect(result.total).toBe(8);
    expect(result.scrapAdds).toBe(0);
    expect(scrapNoteText(result, 0)).toBeNull();
  });

  it('scales by qty_per_run and treats a null per-run as 1.0', () => {
    expect(
      predictMaterialConsumption({
        ties: [makeKioskTie({ qty_per_run: 0.5 })],
        quantityOrdered: 4,
        operationScrapped: 0,
        scrapEntered: 0,
      })!.total
    ).toBe(2);
    expect(
      predictMaterialConsumption({
        ties: [makeKioskTie({ qty_per_run: null })],
        quantityOrdered: 4,
        operationScrapped: 0,
        scrapEntered: 0,
      })!.total
    ).toBe(4);
  });

  it('never predicts a negative delta — an over-consumed tie is a no-op, not a reversal', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie({ qty_consumed: 12 })],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 0,
    });
    // Nothing left to predict => no lines at all.
    expect(result!.lines).toHaveLength(0);
    expect(result!.total).toBe(0);
    expect(result!.anyShort).toBe(false);
  });

  it('attributes scrap correctly even when the tie is already over-consumed', () => {
    // consumed 6; without keyed scrap target is 5 (delta clamped to 0), with 2
    // scrap the target is 7 => delta 1. A naive perRun*scrap would say 2.
    const result = predictMaterialConsumption({
      ties: [makeKioskTie({ qty_consumed: 6 })],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 2,
    })!;
    expect(result.total).toBe(1);
    expect(result.scrapAdds).toBe(1);
  });

  it('sums ties on the same part+lot into one line and keeps different pins apart', () => {
    const merged = predictMaterialConsumption({
      ties: [makeKioskTie(), makeKioskTie({ allocation_id: 2 })],
      quantityOrdered: 2,
      operationScrapped: 0,
      scrapEntered: 0,
    })!;
    expect(merged.lines).toHaveLength(1);
    expect(merged.lines[0].qty).toBe(4);

    const split = predictMaterialConsumption({
      ties: [
        makeKioskTie({ pinned_lot_number: 'A' }),
        makeKioskTie({ allocation_id: 2, pinned_lot_number: 'B' }),
      ],
      quantityOrdered: 2,
      operationScrapped: 0,
      scrapEntered: 0,
    })!;
    expect(split.lines).toHaveLength(2);
  });

  it('flags a predicted shortage without ever implying the job is blocked', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie({ on_hand: 3 })],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 0,
    })!;
    expect(result.anyShort).toBe(true);
    expect(result.lines[0].shortBy).toBe(2);
    const note = shortageNoteText(result)!;
    expect(note).toContain('short 2');
    expect(note).toContain('never blocks the job');
  });

  it('emits no shortage note when stock covers every line', () => {
    const result = predictMaterialConsumption({
      ties: [makeKioskTie()],
      quantityOrdered: 5,
      operationScrapped: 0,
      scrapEntered: 0,
    })!;
    expect(shortageNoteText(result)).toBeNull();
  });
});

describe('kiosk copy', () => {
  it('names the work order and defers the deduction to ITS completion', () => {
    expect(deductionHeadline('WO-2026-0142')).toBe('Material — deducts when WO-2026-0142 finishes');
    expect(deductionHeadline(null)).toBe('Material — deducts when this work order finishes');
    expect(deductionHeadline('  ')).toContain('this work order finishes');
  });

  it('never claims the deduction has already happened', () => {
    const line = deductionLineText({
      key: 'k',
      partLabel: 'SHT-.125-304',
      partName: null,
      unitOfMeasure: 'EA',
      qty: 2,
      onHand: 5,
      shortBy: 0,
      pinnedLotNumber: null,
    });
    expect(line).toBe('2 EA · SHT-.125-304');
    expect(DEDUCTION_TIMING_NOTE).toContain('last operation on this work order completes');
    expect(DEDUCTION_TIMING_NOTE.toLowerCase()).toContain('estimate');
  });

  it('shows the pinned lot on the line when the tie is lot-directed', () => {
    const line = deductionLineText({
      key: 'k',
      partLabel: 'SHT-.125-304',
      partName: null,
      unitOfMeasure: 'EA',
      qty: 2,
      onHand: 5,
      shortBy: 0,
      pinnedLotNumber: 'HEAT-7741',
    });
    expect(line).toContain('lot HEAT-7741');
  });
});

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
 *  - every generated sentence is anchored on THIS OPERATION completing, and the
 *    copy assertions below pin BOTH failure directions: it may not slip back to
 *    "when the work order finishes" (understates — a per-nest laser WO deducts
 *    each nest as it closes) and it may not drift to "per run" (over-states —
 *    reporting runs on an open operation posts nothing).
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
  tie?: DispatchMaterialTie | null,
  status?: string
): Pick<DispatchBoardRow, 'material_tie' | 'work_order_number'> & { status?: string } => ({
  work_order_number: 'WO-2026-0142',
  material_tie: tie ?? null,
  ...(status === undefined ? {} : { status }),
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

  it('words the tooltip for THIS OPERATION completing and flags it as an estimate', () => {
    // A dispatch card is an operation row, and an operation-scoped tie deducts
    // when that operation completes.
    const chip = materialTieChip(makeRow(makeTie()))!;
    expect(chip.title).toContain('when this operation completes');
    expect(chip.title.toLowerCase()).toContain('estimate');
    // Never claims the deduction is happening now (this is a queue, not a
    // completion screen) — and never defers it to the work order finishing,
    // which would tell a planner a per-nest laser WO costs nothing until the
    // last nest closes.
    expect(chip.title.toLowerCase()).not.toContain('deducting');
    expect(chip.title.toLowerCase()).not.toContain('finishes');
  });

  it('never words any chip tier as per-run or as deferred to work-order completion', () => {
    // Every tier, not just the happy one: the tooltip is the only place the
    // timing is stated, so a tier left behind is a tier that lies.
    const tiers = [
      makeTie(), // covered
      makeTie({ on_hand: 3.5 }), // last of stock
      makeTie({ on_hand: 1, short_by: 2 }), // short
      makeTie({ qty_consumed: 3, qty_remaining: 0 }), // fully issued
    ];
    tiers.forEach((tie) => {
      const title = materialTieChip(makeRow(tie))!.title.toLowerCase();
      expect(title).toContain('when this operation completes');
      expect(title).not.toContain('finishes');
      expect(title).not.toContain('per run');
    });
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

  it('reports the CONSUMED total on a fully-issued tie, not the plan figure', () => {
    // Over-consumption (`consumed > planned`) became an ordinary steady state
    // once material started posting per operation, and it is exactly the state a
    // supervisor opens the RETURN dialog on. Quoting `qty_planned` here would
    // under-report the draw by the amount they are about to give back.
    const chip = materialTieChip(makeRow(makeTie({ qty_planned: 3, qty_consumed: 5, qty_remaining: 0 })))!;
    expect(chip.title).toContain('5 EA of SHT-.125-304 reported consumed');
    expect(chip.title).not.toContain('3 EA of SHT-.125-304 reported consumed');
  });
});

describe('materialTieChip — the RETURN re-arm guard', () => {
  // A bounded `correct_over_consumption` lowers `qty_consumed`, which pushes the
  // PLAN-based `qty_remaining` this chip reads back UP. On a live operation that
  // is fine (the material really will be drawn at completion). On a COMPLETE one
  // it is a straight falsehood: the server leaves the tie at
  // `qty_consumed >= target`, so the engine's delta is pinned <= 0 forever.
  const returnedTie = makeTie({ qty_planned: 5, qty_consumed: 3, qty_remaining: 2, on_hand: 4 });

  it('still forecasts a deduction while the operation can complete', () => {
    ['ready', 'in_progress', 'on_hold', 'pending'].forEach((status) => {
      const chip = materialTieChip(makeRow(returnedTie, status))!;
      expect(chip.title).toContain('when this operation completes');
      expect(chip.text).toBe('2 EA · SHT-.125-304');
    });
  });

  it('forecasts NOTHING once the operation is complete, and says why', () => {
    const chip = materialTieChip(makeRow(returnedTie, 'complete'))!;
    expect(chip.tone).toBe('ok');
    expect(chip.text).toBe('SHT-.125-304 · settled');
    // No future-deduction claim of any kind.
    expect(chip.title).not.toContain('deducts');
    expect(chip.title).not.toContain('Estimate');
    expect(chip.title).toContain('nothing further can be drawn');
    // The consumed total is stated from qty_consumed, not the plan gap.
    expect(chip.title).toContain('3 EA');
  });

  it('does not raise a SHORTAGE against a deduction that cannot happen', () => {
    // Plan-based short_by re-arms with qty_remaining; a purchasing signal built
    // on a draw the engine has already refused is a signal made of nothing.
    const short = makeTie({ qty_planned: 5, qty_consumed: 3, qty_remaining: 2, on_hand: 0, short_by: 2 });
    expect(materialTieChip(makeRow(short, 'in_progress'))!.tone).toBe('short');
    expect(materialTieChip(makeRow(short, 'complete'))!.tone).toBe('ok');

    // …including the sibling-shortage tier, which reads the same basis.
    const sibling = makeTie({ tie_count: 2, any_short: true, short_by: 0 });
    expect(materialTieChip(makeRow(sibling, 'in_progress'))!.tone).toBe('short');
    expect(materialTieChip(makeRow(sibling, 'complete'))!.tone).toBe('ok');
  });

  it('treats an ABSENT or unknown status as still live — a new status never blanks a chip', () => {
    // Byte-identical to the pre-guard behaviour for any payload/fixture that
    // carries no status at all.
    expect(materialTieChip(makeRow(returnedTie))!.text).toBe('2 EA · SHT-.125-304');
    expect(materialTieChip(makeRow(returnedTie, 'some_future_status'))!.text).toBe('2 EA · SHT-.125-304');
    expect(materialTieChip(makeRow(returnedTie, ''))!.text).toBe('2 EA · SHT-.125-304');
  });

  it('still renders nothing at all for an untied complete operation', () => {
    expect(materialTieChip(makeRow(null, 'complete'))).toBeNull();
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

  it('agrees with the chips, so a settled tie never inflates the column rollup', () => {
    const short = makeTie({ short_by: 2, on_hand: 1 });
    expect(countShortTies([makeRow(short, 'in_progress'), makeRow(short, 'complete')])).toBe(1);
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
  it('anchors the deduction on THIS operation completing, with the work order as context', () => {
    expect(deductionHeadline('WO-2026-0142')).toBe(
      'Material — deducts when you complete this operation on WO-2026-0142'
    );
    expect(deductionHeadline(null)).toBe('Material — deducts when you complete this operation');
    expect(deductionHeadline('  ')).toBe('Material — deducts when you complete this operation');
  });

  it('never defers the kiosk deduction to the work order finishing', () => {
    // The regression this pins: the pre-per-operation copy read "deducts when
    // WO-#### finishes", which now understates — a laser child WO deducts each
    // nest as that nest's operation closes.
    ['WO-2026-0142', null].forEach((wo) => {
      expect(deductionHeadline(wo).toLowerCase()).not.toContain('finishes');
    });
    expect(DEDUCTION_TIMING_NOTE.toLowerCase()).not.toContain('work order');
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
    // The line itself carries no timing word — the headline and the timing note
    // own that fact between them, so there is no third string to leave stale.
    expect(line).toBe('2 EA · SHT-.125-304');
    expect(DEDUCTION_TIMING_NOTE).toContain('leaves stock when the operation completes');
    expect(DEDUCTION_TIMING_NOTE.toLowerCase()).toContain('estimate');
  });

  it('rules out the per-run reading explicitly — reporting runs posts nothing', () => {
    // The opposite failure direction from the one above. Consumption fires on
    // COMPLETE only: an in-progress operation is still reducible and
    // consumption never auto-reverses, so runs called in along the way move no
    // stock. The timing note has to say so, not merely omit it.
    expect(DEDUCTION_TIMING_NOTE).toContain('not as each run is reported');
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

/**
 * sheetPart — the CROSS-LANGUAGE half of the sheet-recognition contract.
 *
 * `sheetPart.test.ts` next door tests this module on its own terms. This file
 * tests something else: that the TypeScript grammar and the Python port of it
 * (`backend/app/services/sheet_stock_spec.py`) still answer the same way.
 *
 * WHY TWO PORTS EXIST AT ALL
 * --------------------------
 * The TS version answers two questions on the client, about a part the planner
 * has already chosen. The Python version answers a third one the client cannot:
 * given a nest's AI-read material / thickness / sheet size, WHICH stock part in
 * the tenant's catalog is that? Answering it needs the whole catalog, the tie
 * history and on-hand — all behind the DB — so it runs server-side.
 *
 * WHY THE DRIFT MATTERS MORE THAN EITHER PORT
 * -------------------------------------------
 * A drift is not a cosmetic inconsistency. The picker's default filter runs on
 * THIS grammar and the matcher's catalog parse runs on the other one, so a
 * divergence means the wizard offers a sheet the matcher refuses to see, or —
 * the dangerous direction — the matcher pre-fills a part the picker would have
 * hidden, and a planner confirms it without the row ever looking wrong. The tie
 * that follows depletes a real heat lot into an as-built record that never
 * auto-reverses.
 *
 * So both suites read ONE file, `backend/tests/fixtures/sheet_part_cases.json`.
 * There is no second copy to drift, and a change to either port that moves an
 * answer fails CI on both sides at once.
 *
 * ADDING A CASE: add it to the JSON, run both suites. Do not add a case here
 * and a matching one over there.
 */

import cases from '../../../backend/tests/fixtures/sheet_part_cases.json';
import { deriveSheetSpec, isSheetLikePart, SheetPartLike } from './sheetPart';

interface ParityCase {
  id: string;
  why: string;
  part_number: string | null;
  name: string | null;
  description: string | null;
  expect_sheet_like: boolean;
  expect_thickness: string | null;
  expect_sheet_size: string | null;
}

const parityCases: ParityCase[] = cases as ParityCase[];

const toPart = (row: ParityCase): SheetPartLike => ({
  part_number: row.part_number,
  name: row.name,
  description: row.description,
});

describe('sheet_part_cases.json — the shared fixture itself', () => {
  it('is populated', () => {
    // A fixture that silently empties passes both suites vacuously, which is the
    // one failure mode a parity harness cannot afford.
    expect(parityCases.length).toBeGreaterThanOrEqual(50);
  });

  it('has a unique id per case', () => {
    const ids = parityCases.map((row) => row.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('explains every case', () => {
    // The `why` is what a future reader needs to decide whether a failing case is
    // a regression or an intended change. A blank one makes the fixture a wall of
    // strings.
    for (const row of parityCases) {
      expect(typeof row.why).toBe('string');
      expect(row.why.length).toBeGreaterThan(0);
    }
  });
});

describe('deriveSheetSpec agrees with the Python port', () => {
  it.each(parityCases.map((row) => [row.id, row] as const))('%s', (_id, row) => {
    expect(deriveSheetSpec(toPart(row))).toEqual({
      thickness: row.expect_thickness,
      sheetSize: row.expect_sheet_size,
    });
  });
});

describe('isSheetLikePart agrees with the Python port', () => {
  it.each(parityCases.map((row) => [row.id, row] as const))('%s', (_id, row) => {
    expect(isSheetLikePart(toPart(row))).toBe(row.expect_sheet_like);
  });
});

describe('the pair, as the wizard composes them', () => {
  /** What the wizard writes onto a nest row: a spec, but only for sheet stock. */
  const pullThrough = (part: SheetPartLike) =>
    isSheetLikePart(part) ? deriveSheetSpec(part) : { thickness: null, sheetSize: null };

  const misreads = parityCases.filter((row) => !row.expect_sheet_like && row.expect_thickness !== null);

  it('has at least one row the grammar alone mis-reads', () => {
    // If this list ever empties, the guard below stops proving anything: it
    // would be asserting that nothing happens to rows that do not exist.
    expect(misreads.length).toBeGreaterThan(0);
  });

  it.each(misreads.map((row) => [row.id, row] as const))(
    'stamps nothing for %s',
    (_id, row) => {
      // The grammar alone DOES read a spec off these, and the sheet-likeness
      // gate is the only thing keeping angle-iron (or a hex-screw callout)
      // dimensions off a nest row.
      expect(deriveSheetSpec(toPart(row)).thickness).toBe(row.expect_thickness);
      expect(pullThrough(toPart(row))).toEqual({ thickness: null, sheetSize: null });
    }
  );
});

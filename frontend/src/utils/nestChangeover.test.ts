/**
 * Laser-nest sequencing helpers.
 *
 * The rules under test are the ones that decide whether the board claims a
 * setup exists: a changeover is only ever asserted between two REAL nests whose
 * material or thickness are both known and genuinely different — an absent
 * value is not evidence of a change, and casing/whitespace is not a change.
 */

import {
  changeoverLabel,
  formatNestDetail,
  nestChangeover,
  nestDetailSegments,
  nestQueueSummary,
} from './nestChangeover';
import type { DispatchNestInfo } from '../types';

const nest = (overrides: Partial<DispatchNestInfo> = {}): DispatchNestInfo => ({
  cnc_number: 'nest-p001',
  material: 'A36',
  thickness: '0.25in',
  sheet_size: '48x96',
  planned_runs: 5,
  completed_runs: 2,
  remaining_runs: 3,
  ...overrides,
});

describe('nestChangeover', () => {
  it('reports no changeover between two identical nests', () => {
    expect(nestChangeover(nest(), nest())).toBeNull();
  });

  it('reports a material-only change', () => {
    expect(nestChangeover(nest(), nest({ material: '304SS' }))).toBe('material');
  });

  it('reports a thickness-only change', () => {
    expect(nestChangeover(nest(), nest({ thickness: '0.5in' }))).toBe('thickness');
  });

  it('reports both when material AND thickness change', () => {
    expect(nestChangeover(nest(), nest({ material: '304SS', thickness: '0.5in' }))).toBe('both');
  });

  it('compares case-insensitively and ignores surrounding whitespace', () => {
    expect(nestChangeover(nest({ material: 'A36' }), nest({ material: ' a36 ' }))).toBeNull();
    expect(nestChangeover(nest({ thickness: '0.25IN' }), nest({ thickness: '  0.25in' }))).toBeNull();
  });

  it('does NOT invent a changeover when one side is unknown', () => {
    // Unknown -> known and known -> unknown, for both fields, incl. blank strings.
    expect(nestChangeover(nest({ material: null }), nest({ material: 'A36' }))).toBeNull();
    expect(nestChangeover(nest({ material: 'A36' }), nest({ material: undefined }))).toBeNull();
    expect(nestChangeover(nest({ material: '   ' }), nest({ material: 'A36' }))).toBeNull();
    expect(nestChangeover(nest({ thickness: null }), nest({ thickness: '0.5in' }))).toBeNull();
    // ...but a known-vs-known difference alongside an unknown still counts.
    expect(nestChangeover(nest({ thickness: null }), nest({ thickness: null, material: '304SS' }))).toBe('material');
  });

  it('reports nothing when either side is not a nest at all', () => {
    expect(nestChangeover(null, nest())).toBeNull();
    expect(nestChangeover(nest(), null)).toBeNull();
    expect(nestChangeover(undefined, undefined)).toBeNull();
  });
});

describe('changeoverLabel', () => {
  it('names what changes', () => {
    expect(changeoverLabel('material')).toBe('material change');
    expect(changeoverLabel('thickness')).toBe('thickness change');
    expect(changeoverLabel('both')).toBe('material + thickness change');
  });
});

describe('nestQueueSummary', () => {
  const row = (laser_nest: DispatchNestInfo | null) => ({ laser_nest });

  it('counts nests and the changeovers the current order costs', () => {
    const summary = nestQueueSummary([
      row(nest({ material: 'A36' })),
      row(nest({ material: 'A36' })), // same -> no changeover
      row(nest({ material: '304SS' })), // material change
      row(nest({ material: '304SS', thickness: '0.5in' })), // thickness change
    ]);
    expect(summary).toEqual({ nests: 4, changeovers: 2 });
  });

  it('does not count a boundary against a non-nest job', () => {
    const summary = nestQueueSummary([
      row(nest({ material: 'A36' })),
      row(null), // a deburr job sitting in the middle
      row(nest({ material: '304SS' })),
    ]);
    expect(summary).toEqual({ nests: 2, changeovers: 0 });
  });

  it('reports zero nests for a column with no nest work, and handles an empty column', () => {
    expect(nestQueueSummary([row(null), row(null)])).toEqual({ nests: 0, changeovers: 0 });
    expect(nestQueueSummary([])).toEqual({ nests: 0, changeovers: 0 });
  });

  it('re-counts on reorder — batching identical nests together removes changeovers', () => {
    const a = row(nest({ material: 'A36' }));
    const b = row(nest({ material: '304SS' }));
    // Alternating costs three changeovers; batched costs one.
    expect(nestQueueSummary([a, b, a, b]).changeovers).toBe(3);
    expect(nestQueueSummary([a, a, b, b]).changeovers).toBe(1);
  });
});

describe('nestDetailSegments', () => {
  it('lays out material, thickness, sheet size and sheets left', () => {
    expect(formatNestDetail(nest())).toBe('A36 · 0.25in · 48x96 · 3 of 5 sheets left');
  });

  it('omits missing pieces rather than printing empty separators', () => {
    expect(formatNestDetail(nest({ thickness: null, sheet_size: '  ' }))).toBe('A36 · 3 of 5 sheets left');
    expect(formatNestDetail(nest({ material: null, thickness: null, planned_runs: null, remaining_runs: null }))).toBe(
      '48x96'
    );
    expect(formatNestDetail(null)).toBe('');
    expect(nestDetailSegments(undefined)).toEqual([]);
  });

  it('derives sheets left from planned - completed only when the server omitted it', () => {
    expect(formatNestDetail(nest({ remaining_runs: null, completed_runs: 4 }))).toContain('1 of 5 sheets left');
    // An over-run nest never reports a negative remainder.
    expect(formatNestDetail(nest({ remaining_runs: null, completed_runs: 9 }))).toContain('0 of 5 sheets left');
  });

  it('keeps a real partial sheet but drops float noise', () => {
    expect(formatNestDetail(nest({ completed_runs: 2.5, remaining_runs: 2.5 }))).toContain('2.5 of 5 sheets left');
    expect(formatNestDetail(nest({ remaining_runs: 2.9000000000000004 }))).toContain('2.9 of 5 sheets left');
  });

  it('drops the runs segment when there is no planned run count to speak of', () => {
    expect(formatNestDetail(nest({ planned_runs: 0, remaining_runs: 0 }))).toBe('A36 · 0.25in · 48x96');
  });

  it('tags each segment so the card can style material and thickness differently', () => {
    expect(nestDetailSegments(nest()).map((segment) => segment.key)).toEqual([
      'material',
      'thickness',
      'sheet_size',
      'runs',
    ]);
  });
});

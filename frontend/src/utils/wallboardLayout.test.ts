/**
 * Pure helpers for the /wallboard Foundry TV board: the anchor-row + rotating-
 * field paging math, the strict job-state precedence, the duration formatting
 * behind the card time values and the BLOCKED/DOWN rail, and the label helpers.
 *
 * The paging math is asserted as PROPERTIES over the whole delivered range
 * (0..24, the payload cap), not as examples. It is the riskiest arithmetic in
 * the feature and every one of its failure modes is SILENT on the wall — a
 * skipped job, a half-blank grid or a blank grid never throws, so nothing
 * escalates it. Properties are the cheapest coverage that actually holds:
 * no jsdom, no fake timers, no React.
 */

import type { WallboardJob } from '../types/wallboard';
import {
  ANCHOR_SLOTS,
  FIELD_SLOTS,
  blockerLabel,
  classifyJob,
  fieldWindow,
  formatAgeHours,
  formatDownDuration,
  planFieldPages,
  safeMod,
  stripCopy,
  titleCaseDept,
} from './wallboardLayout';

/** The payload cap (`_JOB_WALL_LIMIT = 24`) — the full range a board can deliver. */
const DELIVERED_RANGE = Array.from({ length: 25 }, (_, n) => n);

/** F — the field population below the pinned anchor row, for a delivered count. */
const fieldCountFor = (n: number) => Math.max(0, n - ANCHOR_SLOTS);

/** The field list itself: index i stands for `jobs[ANCHOR_SLOTS + i]`. */
const fieldIndices = (n: number) => Array.from({ length: fieldCountFor(n) }, (_, i) => i);

describe('slot geometry', () => {
  it('anchor is exactly one grid row and anchor + field page 0 is the whole 4x3 grid', () => {
    expect(ANCHOR_SLOTS).toBe(4);
    expect(FIELD_SLOTS).toBe(8);
    // The load-bearing property of the whole design: page 0 IS today's board.
    expect(ANCHOR_SLOTS + FIELD_SLOTS).toBe(12);
  });
});

describe('planFieldPages (page plan over the rotating field)', () => {
  it('COVERAGE: once it cycles, the windows visit EVERY field index — no job is ever skipped', () => {
    DELIVERED_RANGE.forEach(n => {
      const field = fieldIndices(n);
      const starts = planFieldPages(field.length);
      const seen = new Set<number>();
      starts.forEach(start => fieldWindow(field, start).forEach(i => seen.add(i)));
      // Array.from, NOT [...seen]: tsconfig targets es5 with no downlevelIteration,
      // so spreading a Set compiles to a slice() over a length-less object and
      // silently yields [] — a vacuously-passing test in the other direction.
      const covered = Array.from(seen).sort((a, b) => a - b);
      // EVERY delivered job is reachable at every count. The single-page band is
      // no longer an exception to that: it holds only while the field FITS
      // (F <= FIELD_SLOTS), so `field.slice(0, FIELD_SLOTS)` IS the whole field
      // there. Nothing delivered is off-board at any n.
      const expected = starts.length > 1 ? field : field.slice(0, FIELD_SLOTS);
      expect(covered).toEqual(field);
      expect({ n, covered }).toEqual({ n, covered: expected });
    });
  });

  it('cycles at 13-15 too, in two full pages, overlapping rather than blanking (owner decision)', () => {
    // The band that used to sit static. `starts` is flush-clamped, so the second
    // window ends on the last field index and BOTH pages stay full — the flip
    // shifts the field by 1-3 slots instead of turning a clean page, which is the
    // accepted cost of never hiding a delivered job. A short page would blank
    // 4-7 cells for a whole dwell; disjoint full pages are arithmetically
    // impossible when F is barely over FIELD_SLOTS.
    expect(planFieldPages(fieldCountFor(13))).toEqual([0, 1]);
    expect(planFieldPages(fieldCountFor(14))).toEqual([0, 2]);
    expect(planFieldPages(fieldCountFor(15))).toEqual([0, 3]);
    [13, 14, 15].forEach(n => {
      const field = fieldIndices(n);
      const starts = planFieldPages(field.length);
      // Page 0 is still today's board, card for card — the load-bearing property.
      expect(fieldWindow(field, starts[0])).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
      expect(ANCHOR_SLOTS + fieldWindow(field, starts[0]).length).toBe(12);
      // Both pages full, and the last one reaches the final delivered job.
      starts.forEach(start => expect(fieldWindow(field, start)).toHaveLength(FIELD_SLOTS));
      expect(fieldWindow(field, starts[starts.length - 1])).toContain(field.length - 1);
    });
  });

  it('NEVER PARTIALLY BLANK: whenever it pages, every window holds exactly FIELD_SLOTS real entries', () => {
    DELIVERED_RANGE.forEach(n => {
      const field = fieldIndices(n);
      const starts = planFieldPages(field.length);
      if (starts.length <= 1) return;
      starts.forEach(start => {
        expect({ n, start, len: fieldWindow(field, start).length }).toEqual({ n, start, len: FIELD_SLOTS });
      });
    });
  });

  it('STATIC BAND: pages === 1 exactly when nothing is off-screen (n <= 12), cycling from 13 up', () => {
    DELIVERED_RANGE.forEach(n => {
      const pages = planFieldPages(fieldCountFor(n)).length;
      // The board moves if and only if a delivered job would otherwise be hidden
      // — the grid is 12 cells, so that is exactly n >= 13.
      expect({ n, cycles: pages > 1 }).toEqual({ n, cycles: n >= ANCHOR_SLOTS + FIELD_SLOTS + 1 });
    });
  });

  it('starts are non-negative, non-decreasing, and never stride past FIELD_SLOTS (no gap can open)', () => {
    DELIVERED_RANGE.forEach(n => {
      const starts = planFieldPages(fieldCountFor(n));
      starts.forEach((start, i) => {
        expect(start).toBeGreaterThanOrEqual(0);
        if (i > 0) {
          expect(start).toBeGreaterThanOrEqual(starts[i - 1]);
          expect(start - starts[i - 1]).toBeLessThanOrEqual(FIELD_SLOTS);
        }
      });
    });
  });

  it('the FINAL window back-fills flush against the end of the field, never a row of holes', () => {
    DELIVERED_RANGE.forEach(n => {
      const F = fieldCountFor(n);
      const starts = planFieldPages(F);
      if (starts.length <= 1) return;
      expect({ n, end: starts[starts.length - 1] + FIELD_SLOTS }).toEqual({ n, end: F });
    });
  });

  it('matches the worked values in the spec', () => {
    expect(planFieldPages(fieldCountFor(12))).toEqual([0]); // F = 8 -> fits, static
    expect(planFieldPages(fieldCountFor(13))).toEqual([0, 1]); // F = 9 -> cycles, 7/8 overlap
    expect(planFieldPages(fieldCountFor(15))).toEqual([0, 3]); // F = 11
    expect(planFieldPages(fieldCountFor(16))).toEqual([0, 4]); // F = 12, stride = one grid row
    expect(planFieldPages(fieldCountFor(20))).toEqual([0, 8]); // F = 16
    expect(planFieldPages(fieldCountFor(24))).toEqual([0, 8, 12]); // F = 20, the cap
  });

  it('degenerate field counts still yield exactly one page starting at 0', () => {
    expect(planFieldPages(0)).toEqual([0]);
    expect(planFieldPages(-5)).toEqual([0]);
    expect(planFieldPages(Number.NaN)).toEqual([0]);
    expect(planFieldPages(3)).toEqual([0]);
  });
});

describe('safeMod (modulo, not JS remainder)', () => {
  it('a backward clock step yields a valid page index instead of a negative one', () => {
    // The failure this closes: -1 % 3 === -1, starts[-1] is undefined, the grid
    // goes blank WITHOUT throwing, so the ErrorBoundary never fires.
    expect(-1 % 3).toBe(-1);
    expect(safeMod(-1, 3)).toBe(2);
    expect(safeMod(-3, 3)).toBe(0);
    expect(safeMod(-7, 3)).toBe(2);
    for (let a = -50; a <= 50; a += 1) {
      const m = safeMod(a, 3);
      expect(m).toBeGreaterThanOrEqual(0);
      expect(m).toBeLessThan(3);
    }
  });

  it('returns 0 for a non-positive or non-finite modulus rather than NaN', () => {
    expect(safeMod(5, 0)).toBe(0);
    expect(safeMod(5, -2)).toBe(0);
    expect(safeMod(5, 0.5)).toBe(0);
    expect(safeMod(5, Number.NaN)).toBe(0);
    expect(safeMod(5, Number.POSITIVE_INFINITY)).toBe(0);
  });

  it('returns 0 for a non-finite dividend (an Invalid Date arrives here as NaN)', () => {
    expect(safeMod(Number.NaN, 3)).toBe(0);
    expect(safeMod(Number.POSITIVE_INFINITY, 3)).toBe(0);
    expect(safeMod(Number.NEGATIVE_INFINITY, 3)).toBe(0);
  });

  it('holds for huge epoch-derived slot numbers', () => {
    const slot = Math.floor(Date.now() / 22_000);
    expect(safeMod(slot, 3)).toBeGreaterThanOrEqual(0);
    expect(safeMod(slot, 3)).toBeLessThan(3);
    expect(safeMod(Number.MAX_SAFE_INTEGER, 3)).toBe(Number.MAX_SAFE_INTEGER % 3);
    expect(safeMod(-Number.MAX_SAFE_INTEGER, 3)).toBe(3 - (Number.MAX_SAFE_INTEGER % 3));
  });
});

describe('fieldWindow (the second, independent guard)', () => {
  const field = fieldIndices(24); // F = 20

  it('clamps an out-of-range start so a full field can never render a partial or empty grid', () => {
    expect(fieldWindow(field, 999)).toEqual([12, 13, 14, 15, 16, 17, 18, 19]);
    expect(fieldWindow(field, field.length)).toHaveLength(FIELD_SLOTS);
  });

  it('clamps a negative start to page 0 instead of slicing from the end', () => {
    // Array.prototype.slice treats a negative index as from-the-end — the exact
    // silent wrong-window bug the clamp exists to prevent.
    expect(fieldWindow(field, -1)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(fieldWindow(field, -999)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });

  it('clamps a fractional or non-finite start', () => {
    expect(fieldWindow(field, 8.7)).toEqual([8, 9, 10, 11, 12, 13, 14, 15]);
    expect(fieldWindow(field, Number.NaN)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });

  it('a short field returns everything it has, for any start', () => {
    const short = fieldIndices(9); // F = 5
    [-4, 0, 3, 99].forEach(start => expect(fieldWindow(short, start)).toEqual([0, 1, 2, 3, 4]));
    expect(fieldWindow([], 4)).toEqual([]);
  });

  it('windows wo_number STRINGS too — the cycle plan freezes identity, not job objects', () => {
    const wos = ['WO-5', 'WO-6', 'WO-7'];
    expect(fieldWindow(wos, 0)).toEqual(wos);
  });
});

describe('stripCopy (the five-state copy matrix)', () => {
  const at = (n: number, total: number, pageIndex = 0) =>
    stripCopy({ pageIndex, pages: planFieldPages(fieldCountFor(n)).length, delivered: n, total });

  it('1. n === 0 renders NO strip at all', () => {
    expect(at(0, 0)).toEqual({ text: null, showPageBar: false });
    expect(at(0, 7)).toEqual({ text: null, showPageBar: false });
  });

  it('2. static band with nothing off-board is BYTE-IDENTICAL to today', () => {
    [1, 4, 11, 12].forEach(n => {
      expect(at(n, n)).toEqual({ text: 'ALL OPEN WORK ORDERS ON BOARD', showPageBar: false });
    });
  });

  it('3. static band with residue is BYTE-IDENTICAL to today', () => {
    expect(at(1, 4)).toEqual({ text: '+3 MORE WORK ORDERS IN QUEUE', showPageBar: false });
    expect(at(5, 17)).toEqual({ text: '+12 MORE WORK ORDERS IN QUEUE', showPageBar: false });
    expect(at(12, 24)).toEqual({ text: '+12 MORE WORK ORDERS IN QUEUE', showPageBar: false });
    // The static band now holds ONLY where the field fits (n <= 12), so every
    // delivered job on a single-page board is on screen and `+N` counts nothing
    // but the tail the SERVER truncated at its 24-job cap.
    expect(at(12, 12)).toEqual({ text: 'ALL OPEN WORK ORDERS ON BOARD', showPageBar: false });
  });

  it('3b. a delivered job is NEVER off-board at any count — the strip can only report SERVER truncation', () => {
    // The board cycles from 13 up (owner decision 2026-08-19), so `+N MORE ...
    // IN QUEUE` can no longer mean "delivered but not shown". At 13-15 — the
    // band that used to sit static and hide 1-3 jobs — the strip now names the
    // page instead of apologising for a hidden job.
    expect(at(13, 13)).toEqual({ text: 'TOP 4 PINNED · PAGE 1/2 · 13 OPEN WORK ORDERS', showPageBar: true });
    expect(at(14, 14)).toEqual({ text: 'TOP 4 PINNED · PAGE 1/2 · 14 OPEN WORK ORDERS', showPageBar: true });
    expect(at(15, 15)).toEqual({ text: 'TOP 4 PINNED · PAGE 1/2 · 15 OPEN WORK ORDERS', showPageBar: true });
    expect(at(16, 16, 0)).toEqual({ text: 'TOP 4 PINNED · PAGE 1/2 · 16 OPEN WORK ORDERS', showPageBar: true });
    // A single-page board is single-page BECAUSE everything fits, so it can only
    // ever say ALL ... ON BOARD or count the server's truncated tail.
    DELIVERED_RANGE.forEach(n => {
      const pages = planFieldPages(fieldCountFor(n)).length;
      if (n === 0 || pages > 1) return;
      expect({ n, text: at(n, n).text }).toEqual({ n, text: 'ALL OPEN WORK ORDERS ON BOARD' });
    });
  });

  it('4. cycling with no residue names the pinned row, the page and the population', () => {
    expect(at(16, 16, 0)).toEqual({ text: 'TOP 4 PINNED · PAGE 1/2 · 16 OPEN WORK ORDERS', showPageBar: true });
    expect(at(16, 16, 1)).toEqual({ text: 'TOP 4 PINNED · PAGE 2/2 · 16 OPEN WORK ORDERS', showPageBar: true });
    expect(at(20, 20, 1)).toEqual({ text: 'TOP 4 PINNED · PAGE 2/2 · 20 OPEN WORK ORDERS', showPageBar: true });
    expect(at(24, 24, 2)).toEqual({ text: 'TOP 4 PINNED · PAGE 3/3 · 24 OPEN WORK ORDERS', showPageBar: true });
  });

  it('5. cycling with residue says NOT ON BOARD — never "MORE ... IN QUEUE"', () => {
    expect(at(24, 31, 0)).toEqual({
      text: 'TOP 4 PINNED · PAGE 1/3 · 24 OF 31 OPEN WORK ORDERS · +7 NOT ON BOARD',
      showPageBar: true,
    });
    expect(at(16, 19, 1)).toEqual({
      text: 'TOP 4 PINNED · PAGE 2/2 · 16 OF 19 OPEN WORK ORDERS · +3 NOT ON BOARD',
      showPageBar: true,
    });
  });

  it('never emits "MORE WORK ORDERS IN QUEUE" while cycling, at any n or page', () => {
    DELIVERED_RANGE.forEach(n => {
      const pages = planFieldPages(fieldCountFor(n)).length;
      for (let i = 0; i < pages; i += 1) {
        [n, n + 9].forEach(total => {
          const { text, showPageBar } = stripCopy({ pageIndex: i, pages, delivered: n, total });
          expect(showPageBar).toBe(n > 0 && pages > 1);
          if (pages > 1) {
            expect(text).not.toContain('IN QUEUE');
            expect(text).toContain('PINNED');
            expect(text).not.toContain('HELD');
          }
        });
      }
    });
  });

  it('a total below the delivered count reads as zero residue, never a negative', () => {
    expect(at(16, 0, 0).text).toBe('TOP 4 PINNED · PAGE 1/2 · 16 OPEN WORK ORDERS');
    expect(at(12, 3).text).toBe('ALL OPEN WORK ORDERS ON BOARD');
  });

  it('an out-of-range page index still prints a page inside the plan', () => {
    expect(stripCopy({ pageIndex: -1, pages: 3, delivered: 24, total: 24 }).text).toContain('PAGE 3/3');
    expect(stripCopy({ pageIndex: 9, pages: 3, delivered: 24, total: 24 }).text).toContain('PAGE 1/3');
    expect(stripCopy({ pageIndex: Number.NaN, pages: 3, delivered: 24, total: 24 }).text).toContain('PAGE 1/3');
  });
});

describe('classifyJob (work-order card state class)', () => {
  const base: WallboardJob = { wo_number: 'WO-1' };

  it('applies strict precedence HELD > DOWN > BLOCKED > LATE > RUNNING > WAITING', () => {
    expect(classifyJob({ ...base, status: 'on_hold', down: true, blocked: true, is_late: true, running: true })).toBe(
      'held'
    );
    expect(classifyJob({ ...base, down: true, blocked: true, is_late: true, running: true })).toBe('down');
    expect(classifyJob({ ...base, blocked: true, is_late: true, running: true })).toBe('blocked');
    expect(classifyJob({ ...base, is_late: true, running: true })).toBe('late');
    expect(classifyJob({ ...base, running: true })).toBe('running');
    expect(classifyJob(base)).toBe('waiting');
  });

  it('a held job wins over EVERY other flag, one flag at a time', () => {
    const held = { ...base, status: 'on_hold' };
    expect(classifyJob({ ...held, down: true })).toBe('held');
    expect(classifyJob({ ...held, blocked: true })).toBe('held');
    // A held WO stays in `late_total` (the LATE rail already counts ON_HOLD),
    // so is_late + on_hold is a REAL combination — and it reads HELD.
    expect(classifyJob({ ...held, is_late: true, days_late: 9 })).toBe('held');
    expect(classifyJob({ ...held, running: true })).toBe('held');
    expect(classifyJob(held)).toBe('held');
  });

  it('reads the backend WorkOrderStatus value, tolerating case and stray whitespace', () => {
    expect(classifyJob({ ...base, status: 'ON_HOLD' })).toBe('held');
    expect(classifyJob({ ...base, status: ' on_hold ' })).toBe('held');
  });

  it('the other wall statuses are NOT held', () => {
    expect(classifyJob({ ...base, status: 'released' })).toBe('waiting');
    expect(classifyJob({ ...base, status: 'in_progress', running: true })).toBe('running');
    expect(classifyJob({ ...base, status: 'on_holdish', down: true })).toBe('down');
  });

  it('a sparse job (all flags absent) classifies as waiting', () => {
    expect(classifyJob({ wo_number: 'WO-SPARSE' })).toBe('waiting');
  });
});

describe('duration formatting (card time values, rail magnitude columns)', () => {
  it('formats downtime minutes as 47m / 2h14m / 38h', () => {
    expect(formatDownDuration(47)).toBe('47m');
    expect(formatDownDuration(134)).toBe('2h14m');
    expect(formatDownDuration(38 * 60)).toBe('38h');
    expect(formatDownDuration(0)).toBe('0m');
  });

  it('formats blocked age hours as 45m / 38h / 6d', () => {
    expect(formatAgeHours(0.75)).toBe('45m');
    expect(formatAgeHours(38)).toBe('38h');
    expect(formatAgeHours(144)).toBe('6d');
  });
});

describe('labels', () => {
  it('blockerLabel replaces underscores', () => {
    expect(blockerLabel('material_missing')).toBe('material missing');
  });

  it('titleCaseDept renders the scope line title-cased, never the raw param', () => {
    expect(titleCaseDept('machining')).toBe('Machining');
    expect(titleCaseDept('cnc_milling')).toBe('Cnc Milling');
    expect(titleCaseDept('WELDING')).toBe('Welding');
  });
});

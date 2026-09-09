import { analyzeLeftovers, leftoversToFile, LEFTOVER_EXCLUSION_PROFILE } from './leftovers';
import { nestParts, rect, validateNest, validateStock, type Loop, type Nest, type Part, type Stock } from './nesting';
import { EXCLUSION_PROFILE, validateStockExclusions, type StockExclusion } from './stock-exclusions';

const at = (width: number, height: number, x: number, y: number): Loop => ({
  type: 'poly',
  points: [
    { x, y },
    { x: x + width, y },
    { x: x + width, y: y + height },
    { x, y: y + height },
  ],
});
const region = (outline: Loop, id = 'damage', clearance = 0): StockExclusion => ({
  id,
  label: id,
  reason: 'Measured unavailable area for this scenario',
  outline,
  clearance,
});
const plate = (outline: Loop = rect(10, 10), quantity = 1): Part => ({
  id: 'plate',
  name: 'Synthetic plate',
  loops: [outline],
  rotate: false,
  color: 0,
  quantity,
});
const sheet = (exclusions?: StockExclusion[], gap = 2): Stock => ({
  width: 100,
  height: 80,
  margin: 0,
  gap,
  bedWidth: 100,
  bedHeight: 80,
  maxSheets: 2,
  ...(exclusions === undefined ? {} : { exclusions }),
});
const fixedNest = (part: Part, x = 0, y = 0): Nest => ({
  placements: [{ partId: part.id, instance: 0, sheet: 0, x, y, width: 10, height: 10, rotation: 0 }],
  unplaced: [],
  sheets: 1,
  area: 100,
  utilization: 1.25,
  method: 'Independent fixed placement',
});

describe('stock exclusion geometry', () => {
  it('places rectangles beside a region without consuming or inventing part instances', () => {
    const p = plate(rect(20, 10), 3),
      s = sheet([region(at(50, 80, 0, 0))]);
    const n = nestParts([p], s);
    expect(n.unplaced).toEqual([]);
    expect(n.placements).toHaveLength(3);
    expect(n.placements.every(v => v.x >= 51.0008)).toBe(true);
    expect(new Set(n.placements.map(v => v.instance)).size).toBe(3);
    expect(n.area).toBe(600);
    expect(() => validateNest([p], s, n)).not.toThrow();
  });
  it('uses the missing corner of a concave part rather than its bounding rectangle', () => {
    const p = plate({
      type: 'poly',
      points: [
        { x: 0, y: 0 },
        { x: 60, y: 0 },
        { x: 60, y: 20 },
        { x: 20, y: 20 },
        { x: 20, y: 60 },
        { x: 0, y: 60 },
      ],
    });
    const s = { ...sheet([region(at(35, 35, 25, 25))]), width: 61, height: 61, maxSheets: 1 };
    const n = nestParts([p], s);
    expect(n.unplaced).toEqual([]);
    expect(n.placements[0]).toMatchObject({ x: 0, y: 0 });
  });
  it('refuses compensated square-corner overlap even when nominal Euclidean distance is sufficient', () => {
    const p = plate(),
      s = sheet([region(at(10, 10, 11.98, 10.82), 'corner', 1)], 2);
    // Nearest original corners are farther apart than the two radial reserves.
    expect(Math.hypot(1.98, 0.82)).toBeGreaterThan(2.0008);
    expect(() => validateNest([p], s, fixedNest(p))).toThrow(/guarded stock exclusion/);
  });
  it('requires half-gap and curve tolerance in addition to the entered region clearance', () => {
    const p = { ...plate(), geometryToleranceMm: 0.02 };
    const s = sheet([region(at(10, 10, 12, 0), 'slot', 1)], 2);
    expect(() => validateNest([p], s, fixedNest(p, 0.02, 0.02))).toThrow(/guarded stock exclusion/);
  });
  it('keeps an exclusion inside a part hole blocked while part-in-part is disabled', () => {
    const p = { ...plate(rect(60, 60)), loops: [rect(60, 60), at(20, 20, 20, 20)] };
    const s = { ...sheet([region({ type: 'circle', cx: 30, cy: 30, r: 2 })]), width: 60, height: 60, maxSheets: 1 };
    const n = nestParts([p], s);
    expect(n.placements).toEqual([]);
    expect(n.unplaced).toHaveLength(1);
  });
  it('conserves quantities without allocating a fully excluded sheet', () => {
    const p = plate(rect(10, 10), 2),
      s = sheet([region(rect(100, 80))]);
    const n = nestParts([p], s);
    expect(n.sheets).toBe(0);
    expect(n.placements).toEqual([]);
    expect(n.unplaced).toEqual([{ partId: p.id, count: 2, reason: expect.stringMatching(/search found no valid/i) }]);
  });
  it('unions overlapping exclusions and reconciles the v2 area ledger', () => {
    const p = plate(),
      s = { ...sheet([region(at(20, 20, 30, 30), 'a'), region(at(20, 20, 40, 30), 'b')], 0), margin: 5 };
    const n = fixedNest(p, 10, 10),
      report = analyzeLeftovers([p], s, n),
      row = report.sheets[0];
    expect(report.version).toBe('werco-leftovers-v2');
    expect(report.assumptions.profile).toEqual(LEFTOVER_EXCLUSION_PROFILE);
    expect(row.excludedArea).toBeGreaterThan(600);
    expect(row.excludedArea).toBeLessThan(600.1);
    expect(
      row.edgeMarginArea +
        row.excludedArea! +
        row.nominalPartArea +
        row.reservedCutoutArea +
        row.clearanceAndProtectionArea +
        row.remainingArea
    ).toBeCloseTo(row.grossArea, 7);
    expect(row.regions.every(r => r.creditUSD === 0 && r.classification === 'review')).toBe(true);
    const exported = leftoversToFile(report, { parts: [p], stock: s, nest: n });
    expect(Object.entries(exported.sheets[0]).find(([key]) => key === 'excludedAreaIn2')?.[1]).toBeCloseTo(
      row.excludedArea! / (25.4 * 25.4),
      10
    );
  });
  it('counts only exclusion area within the usable sheet, not the margin overlap twice', () => {
    const p = plate(),
      s = { ...sheet([region(at(10, 10, 0, 0))], 0), margin: 5 };
    const row = analyzeLeftovers([p], s, fixedNest(p, 30, 30)).sheets[0];
    expect(row.excludedArea).toBeGreaterThan(24.99);
    expect(row.excludedArea).toBeLessThan(25.01);
  });
  it('reserves at least the analytical circular exclusion plus entered clearance', () => {
    const p = plate(),
      s = sheet([region({ type: 'circle', cx: 50, cy: 40, r: 10 }, 'circle', 3)], 0);
    const report = analyzeLeftovers([p], s, fixedNest(p));
    expect(report.sheets[0].excludedArea).toBeGreaterThanOrEqual(Math.PI * 13 ** 2);
    expect(report.sheets[0].excludedArea).toBeLessThan(Math.PI * (13 + 0.005) ** 2);
  });
  it('preserves legacy outputs when there are no exclusions and binds every v2 region rule', () => {
    const p = plate(),
      old = sheet(undefined, 0),
      empty = sheet([], 0),
      n = fixedNest(p);
    expect(nestParts([p], old)).toEqual(nestParts([p], empty));
    expect(analyzeLeftovers([p], old, n)).toEqual(analyzeLeftovers([p], empty, n));
    const s = sheet([region(at(10, 10, 40, 40))], 0),
      report = analyzeLeftovers([p], s, n);
    const changed = { ...s, exclusions: [{ ...s.exclusions![0], clearance: 1 }] };
    expect(() => leftoversToFile(report, { parts: [p], stock: changed, nest: n })).toThrow(/input binding/);
  });
  it('rejects malformed, outside, duplicated and numerically collapsed regions without normalizing source data', () => {
    const good = region(at(10, 10, 1, 1)),
      source = JSON.stringify(good);
    validateStockExclusions([good], 100, 80);
    expect(JSON.stringify(good)).toBe(source);
    for (const regions of [
      null,
      [good, good],
      [{ ...good, id: 'invalid id' }],
      [{ ...good, reason: ' ' }],
      [{ ...good, clearance: -1 }],
      [region(at(10, 10, 95, 0))],
      [region(at(0.00001, 10, 1, 1))],
    ])
      expect(() => validateStockExclusions(regions, 100, 80)).toThrow();
    expect(() => validateStock({ ...sheet(), exclusions: null as unknown as StockExclusion[] })).toThrow();
  });
  it('unions legacy part-corner reserves without making the ledger negative', () => {
    const p = plate(rect(10, 10), 2),
      s = sheet([region(at(2, 2, 70, 60))], 2);
    const n = fixedNest(p, 5, 5);
    n.placements.push({ ...n.placements[0], instance: 1, x: 16.98, y: 15.82 });
    expect(Math.hypot(1.98, 0.82)).toBeGreaterThan(2);
    expect(() => validateNest([p], s, n)).not.toThrow();
    const row = analyzeLeftovers([p], s, n).sheets[0];
    expect(row.clearanceAndProtectionArea).toBeGreaterThanOrEqual(0);
    expect(Math.abs(row.reconciliationResidualArea)).toBeLessThan(1e-7);
  });
  it('includes stock regions in the existing job source-vertex limit', () => {
    const loop: Loop = {
      type: 'poly',
      points: Array.from({ length: 80 }, (_, i) => ({
        x: 1 + Math.cos((i * Math.PI) / 40),
        y: 1 + Math.sin((i * Math.PI) / 40),
      })),
    };
    const parts = Array.from({ length: 250 }, (_, i) => ({ ...plate(loop), id: `part-${i}` }));
    expect(() => nestParts(parts, sheet([region(at(1, 1, 40, 40))]))).toThrow(/20,000 vertices/);
    expect(() =>
      validateStockExclusions(
        Array.from({ length: 17 }, (_, i) => region(rect(1, 1), `r${i}`)),
        100,
        80
      )
    ).toThrow(/16 regions/);
  });
  it('repeats identical scenarios deterministically and preserves source order and geometry', () => {
    const p = plate(rect(25, 25), 7),
      s = sheet([region(at(25, 80, 0, 0), 'left'), region({ type: 'circle', cx: 80, cy: 50, r: 8 }, 'spot')]);
    const snapshot = JSON.stringify(s),
      first = nestParts([p], s);
    expect(nestParts([p], s)).toEqual(first);
    expect(JSON.stringify(s)).toBe(snapshot);
    expect(first.placements.length + first.unplaced.reduce((n, p) => n + p.count, 0)).toBe(7);
    expect(EXCLUSION_PROFILE.partGapFraction).toBe(0.5);
  });
});

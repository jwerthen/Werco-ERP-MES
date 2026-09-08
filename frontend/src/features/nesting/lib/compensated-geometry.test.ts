import { CURRENT_GEOMETRY_PROFILE, GEOMETRY_PROFILE_SPEC } from './geometry-profile';
import { prepareCompensatedGeometry } from './compensated-geometry';
import { analyzeLeftovers, leftoversToFile } from './leftovers';
import {
  bounds,
  nestParts,
  partFitsUsableStock,
  rect,
  validateNest,
  type Loop,
  type Nest,
  type Part,
  type Placement,
  type Stock,
} from './nesting';

const stock = (current = true): Stock => ({
  width: 100,
  height: 80,
  bedWidth: 100,
  bedHeight: 80,
  margin: 0,
  gap: 2,
  maxSheets: 3,
  ...(current ? { geometryProfile: CURRENT_GEOMETRY_PROFILE } : {}),
});
const part = (outer: Loop = rect(10, 10), quantity = 1): Part => ({
  id: 'p',
  name: 'Synthetic profile',
  loops: [outer],
  quantity,
  rotate: false,
  color: 0,
});
const pose = (p: Part, x: number, y: number, instance = 0, rotation: Placement['rotation'] = 0): Placement => {
  const b = bounds(p.loops[0]);
  return {
    partId: p.id,
    x,
    y,
    width: rotation % 180 ? b.height : b.width,
    height: rotation % 180 ? b.width : b.height,
    rotation,
    instance,
    sheet: 0,
  };
};
const nest = (...placements: Placement[]): Nest => ({
  placements,
  unplaced: [],
  sheets: placements.length ? 1 : 0,
  area: 0,
  utilization: 0,
  method: 'Independent geometry fixture',
});

describe('current compensated geometry', () => {
  it('preserves old edge placements but rejects the same placement under explicit current rules', () => {
    const p = part(),
      n = nest(pose(p, 0, 0));
    expect(() => validateNest([p], stock(false), n)).not.toThrow();
    expect(() => validateNest([p], stock(), n)).toThrow(/envelope.*usable sheet/);
  });
  it('rejects square-tangent corner overlap independently of sufficient nominal distance', () => {
    const p = part(rect(10, 10), 2),
      n = nest(pose(p, 10, 10), pose(p, 21.98, 20.82, 1));
    expect(Math.hypot(1.98, 0.82)).toBeGreaterThan(2);
    expect(() => validateNest([p], stock(false), n)).not.toThrow();
    expect(() => validateNest([p], stock(), n)).toThrow(/envelopes overlap/);
  });
  it('requires canonical grid placements, without silently rounding original placement coordinates', () => {
    const p = part(),
      s = stock();
    expect(() => validateNest([p], s, nest(pose(p, 10.00001, 10)))).toThrow(/integer-grid/);
    expect(() => validateNest([p], stock(false), nest(pose(p, 10.00001, 10)))).not.toThrow();
    expect(() => validateNest([p], s, nest(pose(p, 10.0001, 10)))).not.toThrow();
  });
  it('applies a real envelope fit test and allowed rotations without considering obstacles', () => {
    const p = { ...part(rect(10, 4)), rotate: true },
      s = { ...stock(), width: 7, height: 13 };
    expect(partFitsUsableStock(p, s)).toBe(true);
    expect(partFitsUsableStock({ ...p, rotationMode: 'fixed' }, s)).toBe(false);
    expect(partFitsUsableStock(part(rect(10, 10)), { ...stock(), width: 12, height: 12 })).toBe(false);
    expect(partFitsUsableStock(part(rect(10, 10)), { ...stock(false), width: 12, height: 12 })).toBe(true);
  });
  it('nests current rectangles using guarded candidates and retains all quantities', () => {
    const p = part(rect(10, 10), 5),
      s = stock(),
      result = nestParts([p], s);
    expect(result.placements).toHaveLength(5);
    expect(result.sheets).toBe(1);
    expect(result.placements.every(v => v.x >= 1.0008 && v.y >= 1.0008)).toBe(true);
    expect(() => validateNest([p], s, result)).not.toThrow();
    expect(result).toEqual(nestParts([p], s));
  });
  it('nests analytic circles and retains cross-grain cardinal rotation', () => {
    const p = { ...part({ type: 'circle', cx: 5, cy: 5, r: 5 }, 4), rotate: true, grainAxis: 'x' as const };
    const s = { ...stock(), grainAxis: 'y' as const },
      result = nestParts([p], s);
    expect(result.placements).toHaveLength(4);
    expect(result.placements.every(v => v.rotation === 90 || v.rotation === 270)).toBe(true);
    expect(() => validateNest([p], s, result)).not.toThrow();
  });
  it('reuses exact origin envelopes for translated poses and refuses external source IDs', () => {
    const p = part({
      type: 'poly',
      points: [
        { x: 0, y: 0 },
        { x: 10.00005, y: 0 },
        { x: 5, y: 6.00005 },
      ],
    });
    const c = prepareCompensatedGeometry([p], stock())!,
      a = c.pose(p, pose(p, 5.0001, 7.0002)),
      b = c.pose(p, pose(p, 10.0001, 12.0002));
    expect(a.part.envelope).toBe(b.part.envelope);
    const first = c.translatedPaths(a),
      second = c.translatedPaths(b);
    expect(second).toEqual(first.map(path => path.map(v => ({ X: v.X + 50000, Y: v.Y + 50000 }))));
    expect(Object.isFrozen(a.part.envelope.paths[0][0])).toBe(true);
    expect(() => c.get({ ...p, id: 'unknown' }, 0)).toThrow(/Unknown part/);
  });
  it('reserves holes and uses exact exclusion guards', () => {
    const p = part(rect(20, 20), 2);
    p.loops.push({ type: 'circle', cx: 10, cy: 10, r: 3 });
    const s = {
      ...stock(),
      exclusions: [
        {
          id: 'damage',
          label: 'Damage',
          reason: 'Synthetic observation',
          clearance: 1,
          outline: { type: 'circle' as const, cx: 30, cy: 30, r: 12 },
        },
      ],
    };
    const result = nestParts([p], s),
      report = analyzeLeftovers([p], s, result);
    expect(result.placements).toHaveLength(2);
    expect(report.version).toBe('werco-leftovers-v3');
    expect(report.sheets[0].excludedArea).toBeGreaterThan(Math.PI * 13 * 13);
    expect(report.sheets[0].reservedCutoutArea).toBeCloseTo(2 * Math.PI * 9, 8);
    expect(() => leftoversToFile(report, { parts: [p], stock: s, nest: result })).not.toThrow();
  });
  it('emits v3 with zero excluded area, exact profile, and a reconciled zero-credit ledger', () => {
    const p = part(rect(10, 10), 2),
      s = stock(),
      n = nestParts([p], s),
      report = analyzeLeftovers([p], s, n);
    expect(report).toMatchObject({
      version: 'werco-leftovers-v3',
      creditUSD: 0,
      assumptions: { profile: GEOMETRY_PROFILE_SPEC },
    });
    const row = report.sheets[0];
    expect(row.excludedArea).toBe(0);
    expect(row.regions.every(v => v.id.startsWith('leftover-v3-') && v.creditUSD === 0)).toBe(true);
    expect(
      row.nominalPartArea +
        row.reservedCutoutArea +
        row.clearanceAndProtectionArea +
        row.remainingArea +
        row.edgeMarginArea +
        row.excludedArea!
    ).toBeCloseTo(row.grossArea, 8);
    expect(() => leftoversToFile(report, { parts: [p], stock: s, nest: n })).not.toThrow();
    expect(() => leftoversToFile(report, { parts: [p], stock: stock(false), nest: n })).toThrow(/version/);
    const missing = { ...report, sheets: report.sheets.map(row => ({ ...row })) };
    delete missing.sheets[0].excludedArea;
    expect(() => leftoversToFile(missing)).toThrow(/ledger/);
  });
  it('bounds aggregate prepared orientations and never turns a geometry refusal into an accepted layout', () => {
    const parts = Array.from({ length: 40 }, (_, index) => ({
      ...part({ type: 'circle', cx: 9000, cy: 9000, r: 9000 }),
      id: `large-${index}`,
    }));
    const s = { ...stock(), width: 20000, height: 20000, bedWidth: 20000, bedHeight: 20000, gap: 0 };
    expect(() => nestParts(parts, s)).toThrow(/prepared-envelope vertex budget/);
  });
  it('conserves 300 repeated current instances without mutating their source drawings', () => {
    const parts = Array.from({ length: 100 }, (_, index) => ({
      ...part(rect(10, 5), 3),
      id: `repeat-${index}`,
      rotate: true,
    }));
    const s = { ...stock(), width: 600, height: 600, bedWidth: 600, bedHeight: 600, gap: 1, margin: 2 };
    const before = JSON.stringify(parts),
      started = performance.now();
    const result = nestParts(parts, s);
    expect(performance.now() - started).toBeLessThan(15000);
    expect(result.placements).toHaveLength(300);
    expect(result.unplaced).toEqual([]);
    expect(result.sheets).toBe(1);
    expect(new Set(result.placements.map(p => `${p.partId}:${p.instance}`)).size).toBe(300);
    expect(JSON.stringify(parts)).toBe(before);
    expect(result).toEqual(nestParts(parts, s));
  }, 30000);
  it('keeps original v1 and v2 report contracts under omitted profile', () => {
    const p = part(),
      s = stock(false),
      n = nestParts([p], s);
    expect(analyzeLeftovers([p], s, n)).toMatchObject({ version: 'werco-leftovers-v1' });
    s.exclusions = [
      {
        id: 'damage',
        label: 'Damage',
        reason: 'Synthetic observation',
        clearance: 0,
        outline: { type: 'circle', cx: 50, cy: 50, r: 1 },
      },
    ];
    expect(analyzeLeftovers([p], s, nestParts([p], s))).toMatchObject({ version: 'werco-leftovers-v2' });
  });
});

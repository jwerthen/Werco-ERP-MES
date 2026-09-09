import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { stockForRecordedPiece, prepareStockDomain } from './remnant-domain';
import { nestParts, rect, validateNest, validateJob, type Part, type Loop } from './nesting';
import { jobFromFile, jobToFile } from './units';
import { nominalDomainContains } from './nominal-domain';
import type { StockPieceEvidence, ObservedShape } from '../../../types/stockPiece';

const points = (p: number[][]) => p.map(([x, y]) => ({ x: String(x), y: String(y) }));
const evidence = (geometry: ObservedShape): StockPieceEvidence => ({
  version: 1,
  unit: 'in',
  measurement_method: 'Synthetic benchmark',
  source_units: 'in',
  geometry,
  unavailable_zones: [],
  thickness: '0.125',
  grade: 'A36',
  grain_axis: null,
  location_note: null,
  ownership_note: null,
  certification_note: null,
});
const stock = (geometry: ObservedShape, margin = 0.125, gap = 0.125) =>
  stockForRecordedPiece(evidence(geometry), {
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    margin: margin * 25.4,
    gap: gap * 25.4,
    zoneClearanceIn: '0',
  });
const part = (id: string, outer: Loop, quantity = 1): Part => ({
  id,
  name: id,
  quantity,
  rotate: true,
  color: 0,
  loops: [outer],
});
const rectangle = (id: string, w: number, h: number, quantity = 1) => part(id, rect(w * 25.4, h * 25.4), quantity);

test('one rectangular recorded piece is used once; full quantities remain explicit and reproducible', () => {
  const s = stock({ kind: 'rectangle', width: '10', height: '8' });
  const parts = [rectangle('plate', 3, 3, 8)];
  const result = nestParts(parts, s);
  expect(result.sheets).toBe(1);
  expect(result.placements).toHaveLength(6);
  expect(result.unplaced).toEqual([expect.objectContaining({ partId: 'plate', count: 2 })]);
  expect(result.utilization).toBeCloseTo((100 * 54) / 80, 8);
  expect(nestParts(parts, s)).toEqual(result);
});

test('a concave source places parts in both arms without acquiring the missing corner', () => {
  const s = stock({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 4],
      [4, 4],
      [4, 10],
      [0, 10],
    ]),
    holes: [],
  });
  const parts = [rectangle('block', 3, 3, 6)];
  const result = nestParts(parts, s);
  expect(result.placements.length).toBeGreaterThanOrEqual(5);
  expect(result.placements.every(p => p.x + p.width < 4 * 25.4 || p.y + p.height < 4 * 25.4)).toBe(true);
  expect(result.utilization).toBeCloseTo((100 * result.area) / prepareStockDomain(s).grossArea, 8);
  expect(() => validateNest(parts, s, result)).not.toThrow();
});

test('a circular source uses actual circular stock; a square that fits only the box is unplaced', () => {
  const s = stock({ kind: 'circle', cx: '0', cy: '0', r: '5' });
  expect(nestParts([rectangle('oversize', 8, 8)], s).placements).toHaveLength(0);
  const round = part('round', { type: 'circle', cx: 3 * 25.4, cy: 3 * 25.4, r: 3 * 25.4 });
  const result = nestParts([round], s);
  expect(result.placements).toHaveLength(1);
  expect(result.utilization).toBeCloseTo(36, 8);
});

test('physical holes cannot be covered and inward erosion can yield no usable material', () => {
  const s = stock({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]),
    holes: [
      points([
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
      ]),
    ],
  });
  expect(nestParts([rectangle('cover', 8, 8)], s).placements).toHaveLength(0);
  const empty = stock({ kind: 'rectangle', width: '1', height: '1' }, 1);
  const result = nestParts([rectangle('tiny', 0.1, 0.1)], empty);
  expect(result.sheets).toBe(0);
  expect(result.unplaced[0].count).toBe(1);
});

test('unknown measured grain blocks grain-required parts even when the shape fits', () => {
  const s = stock({ kind: 'rectangle', width: '10', height: '10' });
  const p = { ...rectangle('grain', 1, 1), rotationMode: 'quarter-turn' as const, grainAxis: 'x' as const };
  const result = nestParts([p], s);
  expect(result.placements).toHaveLength(0);
  expect(result.unplaced[0].reason).toMatch(/grain/i);
});

test('independent original-curve distances reject enclosing circles, concave bridges and covered holes', () => {
  expect(nominalDomainContains({ type: 'circle', cx: 5, cy: 5, r: 20 }, rect(10, 10), [], 0.1)).toBe(false);
  expect(nominalDomainContains(rect(10, 10), { type: 'circle', cx: 5, cy: 5, r: 5 }, [], 0.1)).toBe(false);
  expect(nominalDomainContains({ type: 'circle', cx: 5, cy: 5, r: 2 }, rect(10, 10), [], 0.1)).toBe(true);
  const small = {
    type: 'poly' as const,
    points: [
      { x: 1, y: 1 },
      { x: 9, y: 1 },
      { x: 9, y: 9 },
      { x: 1, y: 9 },
    ],
  };
  expect(nominalDomainContains(small, rect(10, 10), [{ type: 'circle', cx: 5, cy: 5, r: 1 }], 0.1)).toBe(false);
  const notch: Loop = {
    type: 'poly',
    points: [
      [0, 0],
      [10, 0],
      [10, 10],
      [6, 10],
      [6, 4],
      [4, 4],
      [4, 10],
      [0, 10],
    ].map(([x, y]) => ({ x, y })),
  };
  expect(nominalDomainContains(small, notch, [], 0.1)).toBe(false);
});

test('legacy standalone job readers and writers cannot silently drop an actual domain', () => {
  const s = stock({ kind: 'rectangle', width: '10', height: '8' });
  const job = {
    version: 1 as const,
    name: 'Recorded',
    material: 'Carbon steel',
    thickness: 3.175,
    parts: [rectangle('plate', 1, 1)],
    stock: s,
    bedConfirmed: true,
  };
  expect(() => validateJob(job)).toThrow(/source-bound project/);
  expect(() => jobToFile(job)).toThrow(/source-bound project/);
  for (const version of [1, 2, 8, 13, 16])
    expect(() => jobFromFile({ ...job, version, units: 'in' })).toThrow(/source-bound project/);
});

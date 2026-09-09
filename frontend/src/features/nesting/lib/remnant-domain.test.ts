import type { StockPieceEvidence, ObservedShape } from '../../../types/stockPiece';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { guardedOuter, filledArea } from './guarded-geometry';
import { domainContains, prepareDomainBoundary } from './domain-containment';
import { stockForRecordedPiece, prepareStockDomain } from './remnant-domain';
import { rect } from './nesting';

const evidence = (geometry: ObservedShape): StockPieceEvidence => ({
  version: 1,
  unit: 'in',
  measurement_method: 'Synthetic dimensions',
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
const points = (values: number[][]) => values.map(([x, y]) => ({ x: String(x), y: String(y) }));
const stock = (value: StockPieceEvidence, marginIn = 0.375) =>
  stockForRecordedPiece(value, {
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    margin: marginIn * 25.4,
    gap: 0.125 * 25.4,
    zoneClearanceIn: '0',
  });
const rectangle = () => evidence({ kind: 'rectangle', width: '10', height: '8' });

test('rectangle material uses its actual area and exact inward numerical protection', () => {
  const input = rectangle(),
    snapshot = JSON.stringify(input),
    s = stock(input);
  const domain = prepareStockDomain(s);
  const inset = Math.ceil((0.375 * 25.4 + 0.0004) * 10_000) / 10_000;
  expect(domain.grossArea).toBe(80 * 25.4 * 25.4);
  expect(domain.usableArea).toBeCloseTo((254 - 2 * inset) * (203.2 - 2 * inset), 8);
  expect(domain.edgeAndProtectionArea + domain.unavailableArea + domain.usableArea).toBeCloseTo(domain.grossArea, 8);
  expect(s.maxSheets).toBe(1);
  expect(JSON.stringify(input)).toBe(snapshot);
});

test('a concave source does not gain its missing bounding-box corner', () => {
  const source = evidence({
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
  const domain = prepareStockDomain(stock(source, 0));
  expect(domain.grossArea).toBe(64 * 25.4 * 25.4);
  const part = prepareDomainBoundary(guardedOuter(rect(2 * 25.4, 2 * 25.4), 0));
  expect(domainContains(part, domain.prepared, Math.round(1 * 25.4 * 10_000), Math.round(1 * 25.4 * 10_000))).toBe(
    true
  );
  expect(domainContains(part, domain.prepared, Math.round(6 * 25.4 * 10_000), Math.round(6 * 25.4 * 10_000))).toBe(
    false
  );
});

test('physical holes are absent material and cannot be covered by a surrounding part', () => {
  const source = evidence({
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
  const domain = prepareStockDomain(stock(source, 0));
  expect(domain.grossArea).toBeCloseTo(96 * 25.4 * 25.4, 8);
  const spanning = prepareDomainBoundary(guardedOuter(rect(4 * 25.4, 4 * 25.4), 0));
  expect(domainContains(spanning, domain.prepared, 3 * 254000, 3 * 254000)).toBe(false);
});

test('available circular material is conservatively inward and has analytic gross area', () => {
  const domain = prepareStockDomain(stock(evidence({ kind: 'circle', cx: '-2', cy: '5', r: '4' }), 0.125));
  const r = (4 - 0.125) * 25.4;
  expect(domain.grossArea).toBeCloseTo(Math.PI * (4 * 25.4) ** 2, 8);
  expect(domain.usableArea).toBeLessThan(Math.PI * r * r);
  expect(domain.usableArea).toBeGreaterThan(Math.PI * (r - 0.004) ** 2);
  for (const ring of domain.usable)
    for (const p of ring) expect(Math.hypot(p.X / 10_000 - 4 * 25.4, p.Y / 10_000 - 4 * 25.4)).toBeLessThan(r);
});

test('negative and large translated source origins use exact inch subtraction before conversion', () => {
  const source = evidence({
    kind: 'polygon',
    outer: [
      { x: '-99999.000000001', y: '-15' },
      { x: '-99989.000000001', y: '-15' },
      { x: '-99989.000000001', y: '-7' },
      { x: '-99999.000000001', y: '-7' },
    ],
    holes: [],
  });
  const s = stock(source);
  expect(s.domain.sourceOriginIn).toEqual({ x: '-99999.000000001', y: '-15' });
  expect(s.domain.outer).toEqual(rect(254, 203.2));
  expect(prepareStockDomain(s).grossArea).toBe(80 * 25.4 * 25.4);
  const circle = stock(evidence({ kind: 'circle', cx: '-100000', cy: '0', r: '1' }));
  expect(circle.domain.sourceOriginIn.x).toBe('-100001');
});

test('overlapping unavailable zones are unioned and counted once', () => {
  const source = rectangle();
  source.unavailable_zones = [
    {
      id: 'a',
      label: 'First',
      reason: 'Synthetic',
      outline: {
        kind: 'polygon',
        pts: points([
          [1, 1],
          [3, 1],
          [3, 3],
          [1, 3],
        ]),
      },
    },
    {
      id: 'b',
      label: 'Second',
      reason: 'Synthetic',
      outline: {
        kind: 'polygon',
        pts: points([
          [2, 1],
          [4, 1],
          [4, 3],
          [2, 3],
        ]),
      },
    },
  ];
  const domain = prepareStockDomain(stock(source, 0));
  const individualArea = domain.guardedZones.reduce((total, path) => total + filledArea([path]), 0);
  expect(domain.unavailableArea).toBeLessThan(individualArea);
  expect(domain.unavailableArea).toBeGreaterThan(6 * 25.4 * 25.4);
  expect(domain.edgeAndProtectionArea + domain.unavailableArea + domain.usableArea).toBeCloseTo(domain.grossArea, 8);
});

test('margin can split a narrow-neck piece and can honestly leave no usable region', () => {
  const source = evidence({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [4, 0],
      [4, 1.8],
      [8, 1.8],
      [8, 0],
      [12, 0],
      [12, 4],
      [8, 4],
      [8, 2.2],
      [4, 2.2],
      [4, 4],
      [0, 4],
    ]),
    holes: [],
  });
  const split = prepareStockDomain(stock(source, 0.5));
  expect(split.prepared.original.rings.filter(ring => ring.positive)).toHaveLength(2);
  const empty = prepareStockDomain(stock(evidence({ kind: 'rectangle', width: '1', height: '1' }), 0.6));
  expect(empty.usable).toEqual([]);
  expect(empty.usableArea).toBe(0);
});

test.each([
  { kind: 'unknown' },
  {
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 10],
      [0, 10],
      [10, 0],
    ]),
    holes: [],
  },
  {
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]),
    holes: [
      points([
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ]),
    ],
  },
] as ObservedShape[])('invalid or unknown source topology is refused: %j', geometry => {
  expect(() => stock(evidence(geometry))).toThrow();
});

test('a zone in a missing corner cannot be silently cropped to a source bounding box', () => {
  const source = evidence({
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
  source.unavailable_zones = [
    {
      id: 'bad',
      label: 'Bad',
      reason: 'Synthetic',
      outline: {
        kind: 'polygon',
        pts: points([
          [6, 6],
          [8, 6],
          [8, 8],
          [6, 8],
        ]),
      },
    },
  ];
  expect(() => stock(source, 0)).toThrow(/wholly/);
});

test('unknown physical grain is never replaced by an inferred full-sheet grain', () => {
  expect(stock(rectangle()).grainAxis).toBeUndefined();
  const source = rectangle();
  source.grain_axis = 'y';
  expect(stock(source).grainAxis).toBe('y');
});

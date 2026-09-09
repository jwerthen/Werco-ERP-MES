import type { ObservedShape, StockPieceEvidence } from '../../../types/stockPiece';
import { emptyEvidence } from '../../../validation/stockPiece';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { canonicalPath, SCALE } from './guarded-geometry';
import { domainContains, prepareDomainBoundary } from './domain-containment';
import { prepareStockDomain, stockForRecordedPiece } from './remnant-domain';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

const shape = (geometry: ObservedShape): StockPieceEvidence => ({
  ...emptyEvidence(),
  measurement_method: 'Independent synthetic measurement',
  geometry,
  thickness: '0.125',
  grade: 'A36',
});
const points = (values: number[][]) => values.map(([x, y]) => ({ x: String(x), y: String(y) }));
const ring = (values: number[][], positive = true) =>
  canonicalPath(
    values.map(([X, Y]) => ({ X, Y })),
    positive
  );
const rectangle = (x: number, y: number, w: number, h: number, positive = true) =>
  ring(
    [
      [x, y],
      [x + w, y],
      [x + w, y + h],
      [x, y + h],
    ],
    positive
  );
const stock = (evidence: StockPieceEvidence, marginIn = 0) =>
  stockForRecordedPiece(evidence, {
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    margin: marginIn * 25.4,
    gap: 0.125 * 25.4,
    zoneClearanceIn: '0',
  });

test('independent circle formula bounds a conservatively inset domain and every returned vertex', () => {
  const source = shape({ kind: 'circle', cx: '-100', cy: '10', r: '3' });
  const before = JSON.stringify(source),
    s = stock(source, 0.25),
    prepared = prepareStockDomain(s);
  const radius = 3 * 25.4,
    limit = 2.75 * 25.4;
  const protection =
    REMNANT_DOMAIN_RULES.numerics.boundaryProtectionMm + REMNANT_DOMAIN_RULES.numerics.circleRadialErrorMm + 2 / SCALE;
  expect(prepared.grossArea).toBeCloseTo(Math.PI * radius * radius, 8);
  expect(prepared.usableArea).toBeLessThan(Math.PI * limit * limit);
  expect(prepared.usableArea).toBeGreaterThan(Math.PI * (limit - protection) ** 2);
  for (const path of prepared.usable)
    for (const p of path) expect(Math.hypot(p.X / SCALE - radius, p.Y / SCALE - radius)).toBeLessThan(limit);
  expect(JSON.stringify(source)).toBe(before);
  expect(s.domain.sourceOriginIn).toEqual({ x: '-103', y: '7' });
});

test('all four rectangle edges enforce the exact inset and distinguish contact from one grid beyond', () => {
  const s = stock(shape({ kind: 'rectangle', width: '10', height: '8' }), 0.125);
  const u = prepareStockDomain(s).prepared;
  const inset = Math.ceil((0.125 * 25.4 + REMNANT_DOMAIN_RULES.numerics.boundaryProtectionMm) * SCALE);
  const p = prepareDomainBoundary([rectangle(0, 0, 100, 100)]);
  const maxX = 254 * SCALE - inset - 100,
    maxY = 203.2 * SCALE - inset - 100;
  for (const [x, y] of [
    [inset, inset],
    [maxX, inset],
    [inset, maxY],
    [maxX, maxY],
  ])
    expect(domainContains(p, u, x, y)).toBe(true);
  for (const [x, y] of [
    [inset - 1, inset],
    [maxX + 1, inset],
    [inset, inset - 1],
    [inset, maxY + 1],
  ])
    expect(domainContains(p, u, x, y)).toBe(false);
});

test('split-boundary tests reject a disconnected-domain bridge whose long midpoint is in material', () => {
  const u = prepareDomainBoundary([rectangle(0, 0, 2, 4), rectangle(3, 0, 7, 4)]);
  const p = prepareDomainBoundary([rectangle(0, 0, 10, 4)]);
  expect(domainContains(p, u)).toBe(false);
  expect(domainContains(prepareDomainBoundary([rectangle(0, 0, 2, 4)]), u)).toBe(true);
});

test('a fully covered triangular physical hole is rejected even when all its vertices touch P boundary', () => {
  const u = prepareDomainBoundary([
    rectangle(0, 0, 10, 10),
    ring(
      [
        [4, 4],
        [6, 4],
        [5, 6],
      ],
      false
    ),
  ]);
  const p = prepareDomainBoundary([rectangle(2, 4, 6, 2)]);
  expect(domainContains(p, u)).toBe(false);
  const same = prepareDomainBoundary([
    rectangle(0, 0, 10, 10),
    ring(
      [
        [4, 4],
        [6, 4],
        [5, 6],
      ],
      false
    ),
  ]);
  expect(domainContains(same, u)).toBe(true);
  const largerEmptyHole = prepareDomainBoundary([rectangle(1, 1, 8, 8), rectangle(3, 3, 4, 4, false)]);
  expect(domainContains(largerEmptyHole, u)).toBe(true);
});

test('a concave material notch remains empty while fully covered holes and box corners stay out of gross area', () => {
  const s = stock(
    shape({
      kind: 'polygon',
      outer: points([
        [0, 0],
        [10, 0],
        [10, 4],
        [4, 4],
        [4, 10],
        [0, 10],
      ]),
      holes: [
        points([
          [1, 1],
          [2, 1],
          [2, 2],
          [1, 2],
        ]),
      ],
    })
  );
  const prepared = prepareStockDomain(s);
  expect(prepared.grossArea).toBeCloseTo(63 * 25.4 * 25.4, 8);
  const p = prepareDomainBoundary([rectangle(0, 0, 254000, 254000)]);
  expect(domainContains(p, prepared.prepared, 6 * 254000, 6 * 254000)).toBe(false);
  expect(prepared.edgeAndProtectionArea + prepared.unavailableArea + prepared.usableArea).toBeCloseTo(
    prepared.grossArea,
    8
  );
});

test('positive sub-grid triangular material violation survives rounded Boolean loss at two large origins', () => {
  const n = 100_000_000;
  expect((n - 1) / (2 * n)).toBeGreaterThan(0);
  expect((n - 1) / (2 * n)).toBeLessThan(0.5);
  for (const origin of [0, 100_000_000]) {
    const translated = (pairs: number[][]) => pairs.map(([x, y]) => [x + origin, y + origin]);
    const p = prepareDomainBoundary([
      ring(
        translated([
          [0, 0],
          [n, 0],
          [n, 1],
        ])
      ),
    ]);
    const u = prepareDomainBoundary([
      ring(
        translated([
          [-1, -1],
          [n + 1, -1],
          [n + 1, 2],
          [-1, 2],
        ])
      ),
      ring(
        translated([
          [n - 1, 0],
          [n, 1],
          [n - 1, 1],
        ]),
        false
      ),
    ]);
    expect(domainContains(p, u)).toBe(false);
  }
});

test('source topology errors cannot be normalized into usable stock', () => {
  const bad: ObservedShape[] = [
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
          [0, 2],
          [1, 2],
          [1, 3],
          [0, 3],
        ]),
      ],
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
          [1, 1],
          [8, 1],
          [8, 8],
          [1, 8],
        ]),
        points([
          [2, 2],
          [3, 2],
          [3, 3],
          [2, 3],
        ]),
      ],
    },
    { kind: 'rectangle', width: '0.000000001', height: '1' },
  ];
  for (const geometry of bad) expect(() => stock(shape(geometry))).toThrow();
});

test('a circle zone tangent to a polygon source is inclusive on the exact original geometry', () => {
  const source = shape({ kind: 'rectangle', width: '10', height: '10' });
  source.unavailable_zones = [
    {
      id: 'tangent',
      label: 'Reported tangent zone',
      reason: 'Synthetic measured boundary',
      outline: { kind: 'circle', cx: '1', cy: '5', r: '1' },
    },
  ];
  expect(() => stock(source)).not.toThrow();
});

test('a sub-grid source-zone protrusion is refused before its coordinates can round onto the outer boundary', () => {
  const source = shape({ kind: 'rectangle', width: '10', height: '10' });
  source.unavailable_zones = [
    {
      id: 'outside',
      label: 'Outside original material',
      reason: 'Synthetic invalid observation',
      outline: {
        kind: 'polygon',
        pts: [
          { x: '-0.000000001', y: '1' },
          { x: '1', y: '1' },
          { x: '1', y: '2' },
          { x: '-0.000000001', y: '2' },
        ],
      },
    },
  ];
  expect(() => stock(source)).toThrow();
});

test('prepared source and indexes own immutable copies across caller mutations and repeat poses', () => {
  const s = stock(shape({ kind: 'rectangle', width: '10', height: '10' }));
  const prepared = prepareStockDomain(s),
    before = JSON.stringify(prepared.source);
  expect(prepared.source).not.toBe(s.domain);
  expect(Object.isFrozen(prepared.source)).toBe(true);
  const p = prepareDomainBoundary([rectangle(0, 0, 100, 100)]);
  expect(domainContains(p, prepared.prepared, 10000, 10000)).toBe(true);
  if (s.domain.outer.type === 'poly') s.domain.outer.points[0].x = 999;
  expect(JSON.stringify(prepared.source)).toBe(before);
  expect(domainContains(p, prepared.prepared, 10000, 10000)).toBe(true);
});

test('predicate work budget refuses expensive exact classification without turning it into clear stock', () => {
  const p = prepareDomainBoundary([rectangle(0, 0, 10, 10)]),
    u = prepareDomainBoundary([rectangle(0, 0, 100, 100)]);
  expect(() => domainContains(p, u, 20, 20, 5)).toThrow(/work budget/);
  expect(domainContains(p, u, 20, 20)).toBe(true);
  expect(() => prepareDomainBoundary([rectangle(1_000_000_001, 0, 10, 10)])).toThrow(/precision/);
});

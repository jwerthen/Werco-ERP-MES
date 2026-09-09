import type { InchPoint, ObservedLoop, ObservedShape, StockPieceEvidence } from '../../../types/stockPiece';
import { emptyEvidence } from '../../../validation/stockPiece';
import { assertReportedZonesContained } from './recorded-source-containment';

const points = (values: [string | number, string | number][]): InchPoint[] =>
  values.map(([x, y]) => ({ x: String(x), y: String(y) }));
const rectangle = (
  left: string | number,
  bottom: string | number,
  right: string | number,
  top: string | number
): ObservedLoop => ({
  kind: 'polygon',
  pts: points([
    [left, bottom],
    [right, bottom],
    [right, top],
    [left, top],
  ]),
});
const circle = (cx: string | number, cy: string | number, r: string | number): ObservedLoop & { kind: 'circle' } => ({
  kind: 'circle',
  cx: String(cx),
  cy: String(cy),
  r: String(r),
});
const sheet: ObservedShape = { kind: 'rectangle', width: '10', height: '10' };
const sourceWithHole: ObservedShape = {
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
};
const evidence = (geometry: ObservedShape, ...outlines: ObservedLoop[]): StockPieceEvidence => ({
  ...emptyEvidence(),
  geometry,
  unavailable_zones: outlines.map((outline, i) => ({
    id: `zone-${i}`,
    label: `Zone ${i}`,
    reason: 'Reported damage',
    outline,
  })),
});

test.each([
  rectangle('-0.000000001', 1, 1, 2),
  rectangle(9, 1, '10.000000001', 2),
  rectangle(1, '-0.000000001', 2, 1),
  rectangle(1, 9, 2, '10.000000001'),
])('rejects a one-nanoinch polygon protrusion that the mm integer grid would erase: %#', zone => {
  expect(() => assertReportedZonesContained(evidence(sheet, zone))).toThrow(/original recorded material/);
});

test('accepts shared corners and collinear edges without changing original measured strings', () => {
  const value = evidence(sheet, rectangle(0, 0, 10, 10), rectangle(0, 1, 1, 2));
  const before = JSON.stringify(value);
  expect(() => assertReportedZonesContained(value)).not.toThrow();
  expect(JSON.stringify(value)).toBe(before);
});

test('circle containment is analytic, including off-axis internal tangency and one-nanoinch rejection', () => {
  const source = circle(0, 0, 10);
  expect(() => assertReportedZonesContained(evidence(source, circle(3, 4, 5)))).not.toThrow();
  expect(() => assertReportedZonesContained(evidence(source, circle(3, '4.000000001', 5)))).toThrow(
    /original recorded material/
  );
  expect(() => assertReportedZonesContained(evidence(source, circle(0, 0, 10)))).not.toThrow();
  expect(() => assertReportedZonesContained(evidence(source, circle(0, 0, '10.000000001')))).toThrow();
});

test('polygon vertices on a true circular source are accepted without inscribed-circle approximation', () => {
  const source = circle(0, 0, 5);
  const zone: ObservedLoop = {
    kind: 'polygon',
    pts: points([
      [3, 4],
      [0, 0],
      [4, 3],
    ]),
  };
  expect(() => assertReportedZonesContained(evidence(source, zone))).not.toThrow();
  expect(() =>
    assertReportedZonesContained(
      evidence(source, {
        ...zone,
        pts: points([
          [3, '4.000000001'],
          [0, 0],
          [4, 3],
        ]),
      })
    )
  ).toThrow();
});

test('circle/segment distances preserve diagonal tangency without a square-root rounding decision', () => {
  // The sloping edge is 3x+4y=20; distance from the origin is exactly4.
  const source: ObservedShape = {
    kind: 'polygon',
    outer: points([
      [-20, -20],
      [20, -20],
      [0, 5],
      [-20, 5],
    ]),
    holes: [],
  };
  // A simpler exact edge through (0,5),(4,2) gives 3x+4y=20.
  source.outer = points([
    [-20, -20],
    [20, -20],
    [4, 2],
    [0, 5],
    [-20, 5],
  ]);
  expect(() => assertReportedZonesContained(evidence(source, circle(0, 0, 4)))).not.toThrow();
  expect(() => assertReportedZonesContained(evidence(source, circle(0, 0, '4.000000001')))).toThrow();
});

test('circle zones may touch rectangle edges but may not cross them by one nanoinch', () => {
  expect(() => assertReportedZonesContained(evidence(sheet, circle(5, 5, 5)))).not.toThrow();
  expect(() => assertReportedZonesContained(evidence(sheet, circle('4.999999999', 5, 5)))).toThrow();
});

test.each([
  rectangle(3, 3, 7, 7), // Encloses the entire hole; its own edges never intersect the hole.
  rectangle(4, 4, 6, 6), // Coincident hole boundary still contains unavailable hole interior.
  rectangle(0, 0, 10, 10),
  circle(5, 5, 2),
  rectangle(2, 4, '4.000000001', 6),
])('rejects zones occupying physical hole interior, including boundary-coincident and tiny overlaps: %#', zone => {
  expect(() => assertReportedZonesContained(evidence(sourceWithHole, zone))).toThrow(/original recorded material/);
});

test('boundary-only contact with a physical hole remains legal for polygon and circle zones', () => {
  expect(() =>
    assertReportedZonesContained(evidence(sourceWithHole, rectangle(2, 4, 4, 6), circle(2, 5, 2)))
  ).not.toThrow();
  expect(() => assertReportedZonesContained(evidence(sourceWithHole, circle('2.000000001', 5, 2)))).toThrow();
});

test('a concave bridge with every vertex in material still fails along an exact edge interval', () => {
  const source: ObservedShape = {
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 10],
      [6, 10],
      [6, 4],
      [4, 4],
      [4, 10],
      [0, 10],
    ]),
    holes: [],
  };
  const bridge: ObservedLoop = {
    kind: 'polygon',
    pts: points([
      [2, 2],
      [8, 2],
      [8, 8],
      [2, 8],
    ]),
  };
  expect(() => assertReportedZonesContained(evidence(source, bridge))).toThrow(/original recorded material/);
  expect(() => assertReportedZonesContained(evidence(source, rectangle(1, 1, 9, 4)))).not.toThrow();
});

test('vertex-crossing concave boundaries cannot be missed by testing only proper segment crossings', () => {
  const source: ObservedShape = {
    kind: 'polygon',
    outer: points([
      [0, 0],
      [8, 0],
      [8, 8],
      [4, 4],
      [0, 8],
    ]),
    holes: [],
  };
  const bridge: ObservedLoop = {
    kind: 'polygon',
    pts: points([
      [1, 1],
      [7, 1],
      [7, 7],
      [1, 7],
    ]),
  };
  expect(() => assertReportedZonesContained(evidence(source, bridge))).toThrow();
});

test('large translated and reverse-winding source coordinates keep exact nanoinch decisions', () => {
  const source: ObservedShape = {
    kind: 'polygon',
    outer: points([
      ['99999', '-99999'],
      ['99999', '-99998'],
      ['100000', '-99998'],
      ['100000', '-99999'],
    ]),
    holes: [],
  };
  expect(() =>
    assertReportedZonesContained(evidence(source, rectangle('99999', '-99999', '100000', '-99998')))
  ).not.toThrow();
  expect(() =>
    assertReportedZonesContained(evidence(source, rectangle('99998.999999999', '-99999', '100000', '-99998')))
  ).toThrow();
});

test('malformed or excessive source rings are refused before expensive containment work', () => {
  expect(() =>
    assertReportedZonesContained(
      evidence(sheet, {
        kind: 'polygon',
        pts: points([
          [1, 1],
          [3, 3],
          [1, 3],
          [3, 1],
        ]),
      })
    )
  ).toThrow(/intersects itself/);
  expect(() => assertReportedZonesContained(evidence(sheet, circle(1, 1, '0')))).toThrow(/positive/);
  expect(() => assertReportedZonesContained(evidence(sheet, circle('1.0', 1, 1)))).toThrow(/canonical/);
  expect(() =>
    assertReportedZonesContained(
      evidence(sheet, { kind: 'polygon', pts: Array.from({ length: 2001 }, (_, i) => ({ x: String(i), y: '1' })) })
    )
  ).toThrow(/vertex limit/);
});

test.each(
  [
    points([
      ['-0.000000001', 1],
      [1, 1],
      [1, 2],
      ['-0.000000001', 2],
    ]),
    points([
      [0, 1],
      [1, 1],
      [1, 2],
      [0, 2],
    ]),
    points([
      [9, 1],
      ['10.000000001', 1],
      ['10.000000001', 2],
      [9, 2],
    ]),
  ].map(hole => [hole] as const)
)('physical holes cannot touch or protrude from the original source, even by a nanoinch: %#', hole => {
  const geometry: ObservedShape = {
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]),
    holes: [hole],
  };
  expect(() => assertReportedZonesContained(evidence(geometry))).toThrow(/strictly inside/);
});

test.each(
  [
    points([
      [2, 2],
      [3, 2],
      [3, 3],
      [2, 3],
    ]), // Nested.
    points([
      [4, 2],
      [5, 2],
      [5, 3],
      [4, 3],
    ]), // Touching.
    points([
      ['3.999999999', 2],
      [5, 2],
      [5, 3],
      ['3.999999999', 3],
    ]), // Sub-grid overlap.
  ].map(hole => [hole] as const)
)('physical holes cannot be nested, touching or overlapping: %#', second => {
  const geometry: ObservedShape = {
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
        [4, 1],
        [4, 4],
        [1, 4],
      ]),
      second,
    ],
  };
  expect(() => assertReportedZonesContained(evidence(geometry))).toThrow(/must not touch, overlap or contain/);
});

test('strictly separated holes retain a one-nanoinch ligament and accept either winding before grid validation', () => {
  const geometry: ObservedShape = {
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
        [4, 1],
        [4, 4],
        [1, 4],
      ]),
      points([
        ['4.000000001', 1],
        ['4.000000001', 4],
        [6, 4],
        [6, 1],
      ]),
    ],
  };
  // Source truth is valid. A later kernel representability check may visibly reject this thin feature.
  expect(() => assertReportedZonesContained(evidence(geometry))).not.toThrow();
});

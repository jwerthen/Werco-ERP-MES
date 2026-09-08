import { DXF_JOIN_TOLERANCE_MM, readDXFGeometry } from './dxf';
import { bounds, importDXF, importDXFWithReport, partArea, type Point } from './nesting';

// Synthetic drawings only. Customer drawings stay outside the repository.
type Group = readonly [number, number | string];
type Entity = readonly Group[];
type XY = readonly [number, number];
const entity = (kind: string, ...groups: Group[]): Entity => [[0, kind], ...groups];
const drawing = (entities: readonly Entity[], units = 4) =>
  [
    [0, 'SECTION'],
    [2, 'HEADER'],
    [9, '$INSUNITS'],
    [70, units],
    [0, 'ENDSEC'],
    [0, 'SECTION'],
    [2, 'ENTITIES'],
    ...entities.flat(),
    [0, 'ENDSEC'],
    [0, 'EOF'],
  ]
    .flat()
    .join('\n') + '\n';
const line = ([x, y]: XY, [endX, endY]: XY, ...extra: Group[]) =>
  entity('LINE', [10, x], [20, y], [11, endX], [21, endY], ...extra);
const circle = (x: number, y: number, radius: number, ...extra: Group[]) =>
  entity('CIRCLE', [10, x], [20, y], [40, radius], ...extra);
const arc = (x: number, y: number, radius: number, start: number, end: number, ...extra: Group[]) =>
  entity('ARC', [10, x], [20, y], [40, radius], [50, start], [51, end], ...extra);
const rectangle = (x = 0, y = 0, width = 10, height = 5): Entity[] => [
  // Deliberately shuffled, with independently reversed edges.
  line([x + width, y + height], [x + width, y]),
  line([x, y], [x, y + height]),
  line([x, y], [x + width, y]),
  line([x, y + height], [x + width, y + height]),
];
const polyline = (vertices: readonly (readonly [number, number, number?])[], closed = true): Entity =>
  entity(
    'LWPOLYLINE',
    [90, vertices.length],
    [70, closed ? 1 : 0],
    ...vertices.flatMap(([x, y, bulge = 0]): Group[] => [
      [10, x],
      [20, y],
      [42, bulge],
    ])
  );
const polar = (radius: number, degrees: number, cx = 0, cy = 0): XY => [
  cx + radius * Math.cos((degrees * Math.PI) / 180),
  cy + radius * Math.sin((degrees * Math.PI) / 180),
];
const spline = (
  controls: readonly (readonly [number, number, number?])[] = [
    [0, 0],
    [5, 20],
    [10, 0],
  ],
  knots: readonly number[] = [0, 0, 0, 1, 1, 1],
  weights: readonly number[] = [],
  degree = 2,
  extra: readonly Group[] = []
): Entity =>
  entity(
    'SPLINE',
    [70, 8],
    [71, degree],
    [72, knots.length],
    [73, controls.length],
    [74, 0],
    ...knots.map((knot): Group => [40, knot]),
    ...weights.map((weight): Group => [41, weight]),
    ...controls.flatMap(([x, y, z = 0]): Group[] => [
      [10, x],
      [20, y],
      [30, z],
    ]),
    ...extra
  );
function polygonLines(count: number, radius = 10): Entity[] {
  const vertices = Array.from({ length: count }, (_, i) => polar(radius, (360 * i) / count));
  return vertices.map((vertex, i) => line(vertex, vertices[(i + 1) % count]));
}
function signedArea(points: Point[]) {
  return (
    points.reduce((total, a, i) => {
      const b = points[(i + 1) % points.length];
      return total + a.x * b.y - b.x * a.y;
    }, 0) / 2
  );
}

describe('LINE and ARC cut contours', () => {
  it('imports a shuffled and reversed LINE rectangle that the original importer rejected', () => {
    const parts = importDXF(drawing(rectangle(-37, 82)), 'line-plate.dxf');
    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({ name: 'line-plate', quantity: 1, rotate: true });
    expect(bounds(parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 10, height: 5 });
    expect(partArea(parts[0])).toBe(50);
  });

  it('retains exact cardinal extrema of a major arc closed by a reversed LINE', () => {
    const source = drawing([line(polar(10, 40, 50, 20), polar(10, 310, 50, 20)), arc(50, 20, 10, 40, 310)]);
    const [loop] = readDXFGeometry(source, 'mm').loops;
    const box = bounds(loop);
    expect(box.x).toBeCloseTo(40, 12);
    expect(box.y).toBeCloseTo(10, 12);
    expect(box.width).toBeCloseTo(10 + 10 * Math.cos((40 * Math.PI) / 180), 12);
    expect(box.height).toBeCloseTo(20, 12);
    expect(importDXF(source, 'major-arc.dxf')).toHaveLength(1);
  });

  it('handles an arc crossing zero degrees without replacing it with the major sweep', () => {
    const source = drawing([arc(0, 0, 10, 350, 10), line(polar(10, 10), polar(10, 350))]);
    const [part] = importDXF(source, 'zero-crossing.dxf');
    expect(bounds(part.loops[0]).width).toBeCloseTo(10 - 10 * Math.cos((10 * Math.PI) / 180), 12);
    expect(bounds(part.loops[0]).height).toBeCloseTo(20 * Math.sin((10 * Math.PI) / 180), 12);
    expect(partArea(part)).toBeGreaterThan(0.3);
    expect(partArea(part)).toBeLessThan(0.4);
  });

  it('assigns holes to their enclosing LINE contour and keeps separated plates independent', () => {
    const parts = importDXF(
      drawing([circle(-10, -5, 2), ...rectangle(50, 10, 7, 3), ...rectangle(-20, -10, 40, 20)]),
      'two-plates.dxf'
    );
    expect(parts).toHaveLength(2);
    const large = parts.find(part => bounds(part.loops[0]).width === 40);
    const small = parts.find(part => bounds(part.loops[0]).width === 7);
    if (!large || !small) throw new Error('The two expected plates were not imported.');
    expect(large.loops).toHaveLength(2);
    expect(large.loops[1]).toEqual({ type: 'circle', cx: 10, cy: 5, r: 2 });
    expect(partArea(large)).toBeCloseTo(800 - Math.PI * 4, 10);
    expect(small.loops).toHaveLength(1);
    expect(partArea(small)).toBe(21);
  });

  it('treats an island inside a hole as another part', () => {
    const parts = importDXF(
      drawing([...rectangle(0, 0, 20, 20), ...rectangle(4, 4, 12, 12), ...rectangle(8, 8, 4, 4)]),
      'island.dxf'
    );
    expect(parts).toHaveLength(2);
    expect(parts.map(partArea).sort((a, b) => a - b)).toEqual([16, 256]);
  });

  it('permits more than 300 LINE records when they form one valid bounded contour', () => {
    const [part] = importDXF(drawing(polygonLines(400)), '400-lines.dxf');
    expect(part.loops).toHaveLength(1);
    expect(part.loops[0].type === 'poly' && part.loops[0].points.length).toBe(400);
    expect(bounds(part.loops[0]).width).toBeCloseTo(20, 12);
    expect(bounds(part.loops[0]).height).toBeCloseTo(20, 12);
  });
});

describe('physical scale and endpoint tolerance', () => {
  it.each([
    { units: 4, fallback: 'in' as const, scale: 1 },
    { units: 1, fallback: 'mm' as const, scale: 25.4 },
    { units: 0, fallback: 'in' as const, scale: 25.4 },
    { units: 0, fallback: 'mm' as const, scale: 1 },
  ])('uses INSUNITS $units with unitless fallback $fallback', ({ units, fallback, scale }) => {
    const [part] = importDXF(drawing(rectangle(), units), 'scaled.dxf', fallback);
    expect(bounds(part.loops[0]).width).toBeCloseTo(10 * scale, 12);
    expect(bounds(part.loops[0]).height).toBeCloseTo(5 * scale, 12);
  });

  it.each([
    { units: 4, delta: 0.001, scale: 1 },
    { units: 1, delta: 0.00004, scale: 25.4 },
  ])('joins tiny roundoff in units $units without shrinking the supplied outer extent', ({ units, delta, scale }) => {
    expect(DXF_JOIN_TOLERANCE_MM).toBe(0.0001 * 25.4);
    const source = drawing(
      [line([0, 0], [10, 0]), line([10 + delta, 0], [10, 5]), line([10, 5], [0, 5]), line([0, 5], [0, 0])],
      units
    );
    const report = importDXFWithReport(source, 'roundoff.dxf');
    expect(report.footprintOnly).toBe(false);
    const [part] = report.parts;
    expect(bounds(part.loops[0]).width).toBeCloseTo((10 + delta) * scale, 10);
    expect(bounds(part.loops[0]).height).toBeCloseTo(5 * scale, 10);
  });

  it.each([
    { units: 4, gap: 0.01 },
    { units: 1, gap: 0.001 },
  ])('labels a real gap in units $units as a whole-drawing footprint rather than a closed part', ({ units, gap }) => {
    const source = drawing(
      [line([0, 0], [10, 0]), line([10 + gap, 0], [10, 5]), line([10, 5], [0, 5]), line([0, 5], [0, 0])],
      units
    );
    const report = importDXFWithReport(source, 'gapped.dxf');
    expect(report.footprintOnly).toBe(true);
    expect(report.warnings.join(' ')).toMatch(/open|branch|footprint/i);
    expect(report.parts).toHaveLength(1);
    expect(report.parts[0].importMode).toBe('drawing-bounds');
    expect(bounds(report.parts[0].loops[0]).width).toBeCloseTo((10 + gap) * (units === 1 ? 25.4 : 1), 10);
  });

  it.each([
    ['duplicate', line([0, 0], [10, 0]), 10, 5],
    ['branch', line([0, 0], [-5, 10]), 15, 10],
  ] as const)('uses all geometry in a warned footprint for %s edges', (_label, extra, width, height) => {
    const report = importDXFWithReport(drawing([...rectangle(), extra]), 'ambiguous.dxf');
    expect(report.footprintOnly).toBe(true);
    expect(report.warnings.length).toBeGreaterThan(0);
    expect(report.parts).toHaveLength(1);
    expect(report.parts[0].importMode).toBe('drawing-bounds');
    expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width, height });
  });
});

describe('polyline compatibility and bulges', () => {
  it('rejects a duplicate bulge field instead of silently replacing a major curve with a straight edge', () => {
    const ambiguous = entity(
      'LWPOLYLINE',
      [90, 4],
      [70, 1],
      [10, 0],
      [20, 0],
      [42, 2],
      [42, 0],
      [10, 10],
      [20, 0],
      [10, 10],
      [20, 10],
      [10, 0],
      [20, 10]
    );
    expect(() => importDXFWithReport(drawing([ambiguous]), 'duplicate-bulge.dxf')).toThrow(/duplicate.*bulge/i);
  });

  it.each([-2, 2])('preserves the full major bulge %s and its cardinal extrema', bulge => {
    const source = drawing([
      polyline([
        [0, 0, bulge],
        [10, 0],
      ]),
    ]);
    const [loop] = readDXFGeometry(source, 'mm').loops;
    expect(bounds(loop).x).toBeCloseTo(-1.25, 12);
    expect(bounds(loop).y).toBeCloseTo(bulge < 0 ? 0 : -10, 12);
    expect(bounds(loop).width).toBeCloseTo(12.5, 12);
    expect(bounds(loop).height).toBeCloseTo(10, 12);
    if (loop.type !== 'poly') throw new Error('A bulge must produce a polygonal curve.');
    expect(Math.sign(signedArea(loop.points))).toBe(Math.sign(bulge));
    expect(importDXF(source, 'major-bulge.dxf')).toHaveLength(1);
  });

  it('reads a legacy 2D POLYLINE / VERTEX / SEQEND rectangle', () => {
    const source = drawing([
      entity('POLYLINE', [70, 1]),
      ...[
        [3, 4],
        [13, 4],
        [13, 9],
        [3, 9],
      ].map(([x, y]) => entity('VERTEX', [10, x], [20, y], [30, 0])),
      entity('SEQEND'),
    ]);
    const [part] = importDXF(source, 'legacy.dxf');
    expect(bounds(part.loops[0])).toEqual({ x: 0, y: 0, width: 10, height: 5 });
    expect(partArea(part)).toBe(50);
  });

  it('stitches an open lightweight polyline to a LINE closing its fourth side', () => {
    const source = drawing([
      polyline(
        [
          [0, 0],
          [10, 0],
          [10, 5],
          [0, 5],
        ],
        false
      ),
      line([0, 5], [0, 0]),
    ]);
    expect(partArea(importDXF(source, 'mixed-polyline.dxf')[0])).toBe(50);
  });
});

describe('drawing metadata and flat coordinate systems', () => {
  it('ignores paper-layout VIEWPORT metadata without rejecting or expanding the cut footprint', () => {
    const report = importDXFWithReport(
      drawing([entity('VIEWPORT', [67, 1], [10, 10000], [20, 10000], [40, 9999], [41, 9999]), ...rectangle()]),
      'layout-viewport.dxf'
    );
    expect(report.footprintOnly).toBe(false);
    expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 10, height: 5 });
  });

  it('reports ignored FORMAT annotations and keeps unsupported text on cut layers visible as an error', () => {
    const report = importDXFWithReport(
      drawing([...rectangle(), entity('TEXT', [8, 'FORMAT'], [1, 'Synthetic annotation'], [10, 900], [20, 900])]),
      'annotated.dxf'
    );
    expect(report.footprintOnly).toBe(false);
    expect(report.warnings.join(' ')).toMatch(/ignored.*annotation/i);
    expect(bounds(report.parts[0].loops[0]).width).toBe(10);
    expect(() =>
      importDXF(drawing([...rectangle(), entity('TEXT', [8, 'CUT'], [1, 'Engraving'])]), 'cut-text.dxf')
    ).toThrow(/does not support TEXT/i);
  });

  it('reflects a negative-Z OCS circle center before assigning it as a hole', () => {
    const report = importDXFWithReport(
      drawing([...rectangle(-10, 0, 20, 10), circle(5, 3, 1, [230, -1])]),
      'ocs-hole.dxf'
    );
    expect(report.footprintOnly).toBe(false);
    expect(report.parts[0].loops[1]).toEqual({ type: 'circle', cx: 5, cy: 3, r: 1 });
  });

  it('reflects ARC geometry but keeps LINE endpoints in WCS when joining a -Z profile', () => {
    const source = drawing([arc(5, 0, 2, 0, 180, [230, -1]), line([-7, 0], [-3, 0], [230, -1])]);
    const geometry = readDXFGeometry(source, 'mm');
    expect(geometry.footprintOnly).toBe(false);
    expect(bounds(geometry.loops[0]).x).toBeCloseTo(-7, 12);
    expect(bounds(geometry.loops[0]).width).toBeCloseTo(4, 12);
    expect(bounds(geometry.loops[0]).height).toBeCloseTo(2, 12);
    expect(importDXFWithReport(source, 'ocs-arc.dxf').footprintOnly).toBe(false);
  });

  it('accepts a translated Z plane, rejects slopes, and marks several parallel elevations as a projected footprint', () => {
    const elevated = rectangle().map(edge => [...edge, [30, 7] as const, [31, 7] as const]);
    const report = importDXFWithReport(drawing(elevated), 'elevated.dxf');
    expect(report.footprintOnly).toBe(false);
    expect(partArea(report.parts[0])).toBe(50);
    expect(() => importDXF(drawing([line([0, 0], [10, 5], [30, 0], [31, 1])]), 'slope.dxf')).toThrow(
      /flat|planar|plane/i
    );
    const projected = importDXFWithReport(
      drawing([circle(0, 0, 1, [30, 0]), circle(5, 0, 1, [30, 1])]),
      'two-planes.dxf'
    );
    expect(projected.footprintOnly).toBe(true);
    expect(projected.warnings.join(' ')).toMatch(/elevation|projection/i);
    expect(projected.parts).toHaveLength(1);
    expect(bounds(projected.parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 7, height: 2 });
  });
});

describe('conservative whole-drawing fallback', () => {
  it.each([
    [
      'self-crossing path',
      [line([0, 0], [10, 10]), line([10, 10], [0, 10]), line([0, 10], [10, 0]), line([10, 0], [0, 0])],
      10,
    ],
    ['intersecting contours', [...rectangle(0, 0, 10, 10), circle(10, 5, 1)], 11],
  ] as const)(
    'retains every extent of %s and clearly marks its rectangular approximation',
    (_label, entities, width) => {
      const report = importDXFWithReport(drawing(entities), 'topology.dxf');
      expect(report.footprintOnly).toBe(true);
      expect(report.warnings.length).toBeGreaterThan(0);
      expect(report.parts).toHaveLength(1);
      expect(report.parts[0].importMode).toBe('drawing-bounds');
      expect(report.parts[0].loops).toHaveLength(1);
      expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width, height: 10 });
    }
  );

  it('uses positive-weight spline control bounds and all other geometry in one warned footprint', () => {
    const report = importDXFWithReport(
      drawing([spline(undefined, undefined, [1, 0.5, 1]), ...rectangle(20, -5, 5, 5)]),
      'spline-and-plate.dxf'
    );
    expect(report.footprintOnly).toBe(true);
    expect(report.warnings.join(' ')).toMatch(/spline.*control.*bounds/i);
    expect(report.parts).toHaveLength(1);
    expect(report.parts[0].importMode).toBe('drawing-bounds');
    // The actual rational curve lies below its y=20 control point. Using a
    // sampled curve as the bounds would make this estimate less conservative.
    expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 25, height: 25 });
    expect(partArea(report.parts[0])).toBe(625);
  });

  it('keeps spline control points in WCS even with a -Z normal', () => {
    const geometry = readDXFGeometry(
      drawing([
        spline(
          [
            [2, 0],
            [5, 20],
            [12, 0],
          ],
          undefined,
          [],
          2,
          [[230, -1]]
        ),
      ]),
      'mm'
    );
    expect(geometry.footprintOnly).toBe(true);
    expect(bounds(geometry.footprint)).toEqual({ x: 2, y: 0, width: 10, height: 20 });
  });

  it('accepts repeated knots and positive rational weights on a closed periodic-flagged spline', () => {
    const controls: XY[] = [
      [1, 0],
      [1, 1],
      [0, 1],
      [-1, 1],
      [-1, 0],
      [-1, -1],
      [0, -1],
      [1, -1],
      [1, 0],
    ];
    const rational = spline(
      controls,
      [0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4],
      controls.map((_, i) => (i % 2 ? Math.SQRT1_2 : 1))
    ).map(group => (group[0] === 70 ? ([70, 15] as const) : group));
    const report = importDXFWithReport(drawing([rational]), 'rational-closed.dxf');
    expect(report.footprintOnly).toBe(true);
    expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 2, height: 2 });
  });

  it.each([
    ['descending knots', spline(undefined, [0, 0, 0, 1, 0.5, 1]), /knot/i],
    ['zero domain', spline(undefined, [0, 0, 0, 0, 0, 0]), /knot/i],
    ['wrong knot count', spline(undefined, [0, 0, 0, 1, 1]), /knot count/i],
    ['zero weight', spline(undefined, undefined, [1, 0, 1]), /positive weight/i],
    ['negative weight', spline(undefined, undefined, [1, -1, 1]), /positive weight/i],
    ['partial weights', spline(undefined, undefined, [1, 1]), /weight/i],
    ['invalid weight', spline(undefined, undefined, [1, NaN, 1]), /number/i],
    ['invalid degree', spline(undefined, undefined, [], 0), /degree/i],
    [
      'sloping controls',
      spline([
        [0, 0, 0],
        [5, 20, 1],
        [10, 0, 0],
      ]),
      /flat|planar|plane/i,
    ],
  ] as const)('does not hide %s behind a bounding-box fallback', (_label, invalid, error) => {
    expect(() => importDXFWithReport(drawing([...rectangle(), invalid]), 'invalid-spline.dxf')).toThrow(error);
  });
});

describe('supplied SPLINE fit-point validation', () => {
  const fittedSpline = (fitGroups: readonly Group[], count = 1): Entity => [
    ...spline().map(group => (group[0] === 74 ? ([74, count] as const) : group)),
    ...fitGroups,
  ];

  it('accepts complete fit metadata sharing the control-point plane', () => {
    const report = importDXFWithReport(
      drawing([
        fittedSpline([
          [11, 0],
          [21, 0],
          [31, 0],
        ]),
      ]),
      'fit-points.dxf'
    );
    expect(report.footprintOnly).toBe(true);
    expect(report.parts[0].importMode).toBe('drawing-bounds');
    expect(bounds(report.parts[0].loops[0])).toEqual({ x: 0, y: 0, width: 10, height: 20 });
  });

  it.each([
    [
      'invalid X',
      [
        [11, 'NaN'],
        [21, 0],
        [31, 0],
      ],
      /invalid.*number/i,
    ],
    [
      'missing Y',
      [
        [11, 0],
        [31, 0],
      ],
      /missing.*fit-point Y/i,
    ],
    [
      'duplicate Y',
      [
        [11, 0],
        [21, 0],
        [21, 1],
      ],
      /duplicate.*fit-point Y/i,
    ],
    [
      'duplicate Z',
      [
        [11, 0],
        [21, 0],
        [31, 0],
        [31, 0],
      ],
      /duplicate.*fit-point Z/i,
    ],
    [
      'different Z plane',
      [
        [11, 0],
        [21, 0],
        [31, 1],
      ],
      /fit points.*plane/i,
    ],
  ] as const)('rejects %s even when control points and knots could provide bounds', (_label, fits, error) => {
    expect(() => importDXFWithReport(drawing([fittedSpline(fits)]), 'invalid-fit.dxf')).toThrow(error);
  });

  it('limits supplied fit points to 2,000 even when the declared and actual counts agree', () => {
    const fits = Array.from({ length: 2001 }, (): Group[] => [
      [11, 0],
      [21, 0],
      [31, 0],
    ]).flat();
    expect(() => importDXFWithReport(drawing([fittedSpline(fits, 2001)]), 'too-many-fits.dxf')).toThrow(
      /fit-point count/i
    );
  });
});

describe('invalid geometry and resource limits', () => {
  it.each([
    ['zero-length line', [...rectangle(), line([30, 30], [30, 30])], /zero-length/i],
    ['paper-space cuts', [line([0, 0], [10, 0], [67, 1])], /paper-space|model-space/i],
    ['nonplanar cuts', [line([0, 0], [10, 0], [30, 1])], /flat|planar/i],
    ['unsupported block', [...rectangle(), entity('INSERT', [2, 'unexpanded-block'])], /does not support INSERT/i],
    ['invalid coordinate', [entity('LINE', [10, 'NaN'], [20, 0], [11, 10], [21, 0])], /invalid.*number/i],
    ['missing coordinate', [entity('LINE', [10, 0], [20, 0], [11, 10])], /missing.*coordinates/i],
    ['missing legacy terminator', [entity('POLYLINE', [70, 1]), entity('VERTEX', [10, 0], [20, 0])], /SEQEND/i],
  ] as const)('rejects %s without silently importing a partial drawing', (_label, entities, message) => {
    expect(() => importDXF(drawing(entities), 'invalid.dxf')).toThrow(message);
  });

  it('retains the 300-contour limit independently of the increased record allowance', () => {
    expect(() =>
      importDXF(drawing(Array.from({ length: 301 }, (_, i) => circle(i * 3, 0, 1))), 'too-many.dxf')
    ).toThrow(/300 closed contours/);
  });

  it('retains the 2,000-vertex contour limit after stitching', () => {
    expect(() => importDXF(drawing(polygonLines(2001)), 'too-detailed.dxf')).toThrow(/2,000-vertex/);
  });

  it('rejects too many entity records before attempting to stitch them', () => {
    const source = drawing(Array.from({ length: 20001 }, () => entity('LINE')));
    expect(() => importDXF(source, 'too-many-records.dxf')).toThrow(/20,000 records/);
  });

  it('retains size, group-pair, unit, and end-marker validation', () => {
    expect(() => importDXF(' '.repeat(5_000_000), 'too-large.dxf')).toThrow(/5 MB/);
    expect(() => importDXF(drawing(rectangle()).replace('0\nEOF\n', ''), 'no-end.dxf')).toThrow(/end marker/);
    expect(() => importDXF(drawing(rectangle()) + '0\n', 'odd-pairs.dxf')).toThrow(/group pairs/);
    expect(() => importDXF(drawing(rectangle(), 2), 'feet.dxf')).toThrow(/mm and inch/);
  });
});

import {
  bounds,
  importDXF,
  nestParts,
  partArea,
  rect,
  transformLoops,
  validateNest,
  type Loop,
  type Nest,
  type Part,
  type Placement,
  type Point,
  type Stock,
} from './nesting';

const tolerance = 1e-6;
const makePart = (id: string, outer: Loop, quantity = 1): Part => ({
  id,
  name: id,
  loops: [outer],
  quantity,
  rotate: true,
  color: 0,
});
const makeStock = (width: number, height: number, gap = 1, margin = 0): Stock => ({
  width,
  height,
  gap,
  margin,
  maxSheets: 1,
  bedWidth: width,
  bedHeight: height,
});
const elbow: Loop = {
  type: 'poly',
  points: [
    { x: 0, y: 0 },
    { x: 60, y: 0 },
    { x: 60, y: 20 },
    { x: 20, y: 20 },
    { x: 20, y: 60 },
    { x: 0, y: 60 },
  ],
};
const circle = (radius: number): Loop => ({ type: 'circle', cx: radius, cy: radius, r: radius });

// An independent distance oracle checks physical separation of the returned
// outlines, rather than trusting the engine's own validator or its envelopes.
function segments(loop: Extract<Loop, { type: 'poly' }>): [Point, Point][] {
  return loop.points.map((point, index) => [point, loop.points[(index + 1) % loop.points.length]]);
}
function pointDistance(point: Point, a: Point, b: Point) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const t = Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(point.x - a.x - t * dx, point.y - a.y - t * dy);
}
function side(a: Point, b: Point, point: Point) {
  return (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x);
}
function segmentDistance(a: Point, b: Point, c: Point, d: Point) {
  if (side(a, b, c) * side(a, b, d) < 0 && side(c, d, a) * side(c, d, b) < 0) return 0;
  return Math.min(pointDistance(a, c, d), pointDistance(b, c, d), pointDistance(c, a, b), pointDistance(d, a, b));
}
function insidePolygon(point: Point, loop: Extract<Loop, { type: 'poly' }>) {
  let inside = false;
  for (const [a, b] of segments(loop)) {
    if (pointDistance(point, a, b) < tolerance) return false;
    if (a.y > point.y !== b.y > point.y && point.x < a.x + ((point.y - a.y) * (b.x - a.x)) / (b.y - a.y)) {
      inside = !inside;
    }
  }
  return inside;
}
function clearance(a: Loop, b: Loop): number {
  if (a.type === 'circle' && b.type === 'circle') return Math.hypot(a.cx - b.cx, a.cy - b.cy) - a.r - b.r;
  if (a.type === 'poly' && b.type === 'circle') return clearance(b, a);
  if (a.type === 'circle' && b.type === 'poly') {
    const center = { x: a.cx, y: a.cy };
    if (insidePolygon(center, b) || b.points.some(p => Math.hypot(p.x - a.cx, p.y - a.cy) < a.r)) return -1;
    return Math.min(...segments(b).map(([p, q]) => pointDistance(center, p, q))) - a.r;
  }
  if (a.type !== 'poly' || b.type !== 'poly') throw new Error('Unexpected contour combination.');
  if (insidePolygon(a.points[0], b) || insidePolygon(b.points[0], a)) return -1;
  return Math.min(...segments(a).flatMap(([p, q]) => segments(b).map(([r, s]) => segmentDistance(p, q, r, s))));
}
function assertPhysicalLayout(parts: Part[], stock: Stock, nest: Nest) {
  const outlines = nest.placements.map(placement => {
    const part = parts.find(candidate => candidate.id === placement.partId);
    if (!part) throw new Error('Unknown placed part.');
    const outer = transformLoops(part, placement)[0];
    const box = bounds(outer);
    expect(box.x).toBeGreaterThanOrEqual(stock.margin - tolerance);
    expect(box.y).toBeGreaterThanOrEqual(stock.margin - tolerance);
    expect(box.x + box.width).toBeLessThanOrEqual(stock.width - stock.margin + tolerance);
    expect(box.y + box.height).toBeLessThanOrEqual(stock.height - stock.margin + tolerance);
    return { outer, sheet: placement.sheet };
  });
  for (let i = 0; i < outlines.length; i++) {
    for (let j = i + 1; j < outlines.length; j++) {
      if (outlines[i].sheet === outlines[j].sheet) {
        expect(clearance(outlines[i].outer, outlines[j].outer)).toBeGreaterThanOrEqual(stock.gap - tolerance);
      }
    }
  }
}
function boxesOverlap(a: Placement, b: Placement) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}
function placement(part: Part, x: number, y: number, instance = 0, rotation: Placement['rotation'] = 0): Placement {
  const box = bounds(part.loops[0]);
  const sideways = rotation === 90 || rotation === 270;
  return {
    partId: part.id,
    instance,
    x,
    y,
    width: sideways ? box.height : box.width,
    height: sideways ? box.width : box.height,
    rotation,
    sheet: 0,
  };
}
function proposedNest(parts: Part[], placements: Placement[]): Nest {
  return {
    placements,
    unplaced: [],
    sheets: 1,
    area: parts.reduce((sum, part) => sum + partArea(part) * part.quantity, 0),
    utilization: 0,
    method: 'Test fixture',
  };
}

describe('contour placement', () => {
  it('interlocks two L profiles on one sheet while maintaining a positive physical gap', () => {
    const parts = [makePart('elbow', elbow, 2)];
    // Feasible layout: L at (2.5,2.5), 180-degree L at (25,5).
    // Two 60x60 envelopes cannot both fit in the 82.5x62.5 usable area.
    const stock = makeStock(87.5, 67.5, 2.5, 2.5);
    const result = nestParts(parts, stock);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(2);
    expect(result.sheets).toBe(1);
    expect(boxesOverlap(result.placements[0], result.placements[1])).toBe(true);
    expect(result.area).toBeCloseTo(4000);
    assertPhysicalLayout(parts, stock, result);
  });

  it('uses an open concavity without treating the whole bounding rectangle as occupied', () => {
    const parts = [makePart('elbow', elbow), makePart('infill', rect(20, 20))];
    const stock = makeStock(64, 64, 2.5, 2);
    const result = nestParts(parts, stock);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(2);
    expect(boxesOverlap(result.placements[0], result.placements[1])).toBe(true);
    assertPhysicalLayout(parts, stock, result);
  });

  it('staggers circles using their true radius rather than allocating square cells', () => {
    const parts = [makePart('disk', circle(10), 3)];
    const stock = makeStock(43, 40.3, 1, 1);
    const result = nestParts(parts, stock);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(3);
    expect(result.sheets).toBe(1);
    expect(result.placements.some((a, i) => result.placements.slice(i + 1).some(b => boxesOverlap(a, b)))).toBe(true);
    assertPhysicalLayout(parts, stock, result);
  });

  it('keeps internal cutouts reserved instead of placing other parts inside holes', () => {
    const ring = {
      ...makePart('ring', circle(30)),
      loops: [circle(30), { type: 'circle' as const, cx: 30, cy: 30, r: 22 }],
    };
    const disk = makePart('disk', circle(10));
    const stock = makeStock(60, 60, 1);
    const result = nestParts([ring, disk], stock);
    expect(result.placements.map(p => p.partId)).toEqual(['ring']);
    expect(result.unplaced).toEqual([{ partId: 'disk', count: 1, reason: expect.any(String) }]);
    expect(result.area).toBeCloseTo(Math.PI * (30 ** 2 - 22 ** 2));
    expect(() =>
      validateNest([ring, disk], stock, proposedNest([ring, disk], [placement(ring, 0, 0), placement(disk, 20, 20)]))
    ).toThrow();
  });

  it('honors rotation locks and accounts for every requested instance', () => {
    const locked = { ...makePart('grain-locked', rect(6, 4), 2), rotate: false };
    const stock = makeStock(4, 6, 0);
    const rejected = nestParts([locked], stock);
    expect(rejected.placements).toEqual([]);
    expect(rejected.unplaced).toEqual([{ partId: locked.id, count: 2, reason: expect.any(String) }]);
    const rotatable = { ...locked, quantity: 1, rotate: true };
    const accepted = nestParts([rotatable], stock);
    expect(accepted.unplaced).toEqual([]);
    expect(accepted.placements).toHaveLength(1);
    expect([90, 270]).toContain(accepted.placements[0].rotation);
    expect(() => validateNest([{ ...rotatable, rotate: false }], stock, accepted)).toThrow(/rotation|grain/i);
  });

  it('keeps holes and curve padding when optimizing actual rectangular outer profiles', () => {
    const part: Part = {
      ...makePart('rectangular-profile', rect(100, 50), 2),
      loops: [rect(100, 50), { type: 'circle', cx: 20, cy: 20, r: 5 }],
      geometryToleranceMm: 0.00254,
    };
    const stock = makeStock(210, 60, 1, 1);
    const result = nestParts([part], stock);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(2);
    expect(result.area).toBeCloseTo(2 * (5000 - Math.PI * 25));
    const paddedStock = { ...stock, margin: stock.margin + 0.00254, gap: stock.gap + 2 * 0.00254 };
    assertPhysicalLayout([part], paddedStock, result);
    for (const placed of result.placements) {
      expect(transformLoops(part, placed)[1]).toMatchObject({ type: 'circle', r: 5 });
    }
  });
});

describe('contour validation independently of search', () => {
  it('allows shared edges at zero gap but rejects overlapping interiors', () => {
    const part = makePart('plate', rect(10, 5), 2);
    const stock = makeStock(20, 5, 0);
    const touching = proposedNest([part], [placement(part, 0, 0), placement(part, 10, 0, 1)]);
    expect(() => validateNest([part], stock, touching)).not.toThrow();
    for (const x of [9.75, 0]) {
      const overlapping = { ...touching, placements: [touching.placements[0], placement(part, x, 0, 1)] };
      expect(() => validateNest([part], stock, overlapping)).toThrow();
    }
  });

  it('accepts diagonal circle clearance exactly at the requested gap and rejects a smaller gap', () => {
    const part = makePart('disk', circle(10), 2);
    const stock = makeStock(80, 80, 1, 1);
    // Center displacement (12.6,16.8) is exactly 21: radius 10 + gap 1 + radius 10.
    const valid = proposedNest([part], [placement(part, 1, 1), placement(part, 13.6, 17.8, 1)]);
    expect(() => validateNest([part], stock, valid)).not.toThrow();
    const tooClose = { ...valid, placements: valid.placements.map((p, i) => (i ? { ...p, x: p.x - 0.01 } : p)) };
    expect(() => validateNest([part], stock, tooClose)).toThrow();
    const outsideMargin = { ...valid, placements: valid.placements.map((p, i) => (i ? p : { ...p, x: 0.99 })) };
    expect(() => validateNest([part], stock, outsideMargin)).toThrow(/margin/i);
  });

  it('accepts separated concave outlines even when their bounding boxes overlap', () => {
    const outer = makePart('elbow', elbow);
    const infill = makePart('infill', rect(20, 20));
    const parts = [outer, infill];
    const stock = makeStock(60, 60, 2.5);
    const nest = proposedNest(parts, [placement(outer, 0, 0), placement(infill, 22.5, 22.5)]);
    assertPhysicalLayout(parts, stock, nest);
    expect(() => validateNest(parts, stock, nest)).not.toThrow();
  });

  it('measures circle-to-polygon clearance analytically inside an open concavity', () => {
    const outer = makePart('elbow', elbow);
    const disk = makePart('disk', circle(10));
    const parts = [outer, disk];
    const stock = makeStock(60, 60, 2.5);
    const nest = proposedNest(parts, [placement(outer, 0, 0), placement(disk, 22.5, 22.5)]);
    assertPhysicalLayout(parts, stock, nest);
    expect(() => validateNest(parts, stock, nest)).not.toThrow();
    const tooClose = { ...nest, placements: [nest.placements[0], placement(disk, 22.49, 22.5)] };
    expect(() => validateNest(parts, stock, tooClose)).toThrow();
  });

  it('rejects crossing edges even when neither polygon has a vertex inside the other', () => {
    const horizontal = makePart('horizontal', rect(40, 10));
    const vertical = makePart('vertical', rect(10, 40));
    const parts = [horizontal, vertical];
    const crossing = proposedNest(parts, [placement(horizontal, 10, 10), placement(vertical, 25, 0)]);
    expect(() => validateNest(parts, makeStock(100, 100), crossing)).toThrow();
  });

  it('rejects wholly contained polygons whose boundaries never intersect', () => {
    const outer = makePart('outer', rect(50, 50));
    const inner = makePart('inner', rect(10, 10));
    const parts = [outer, inner];
    const nested = proposedNest(parts, [placement(outer, 0, 0), placement(inner, 20, 20)]);
    expect(() => validateNest(parts, makeStock(100, 100), nested)).toThrow();
  });

  it.each([
    [0, [10, 20], [11, 21]],
    [90, [14, 20], [13, 21]],
    [180, [16, 24], [15, 23]],
    [270, [10, 26], [11, 25]],
  ] as const)('transforms outer vertices and holes consistently at %s degrees', (rotation, vertex, center) => {
    const part = {
      ...makePart('asymmetric', rect(6, 4)),
      loops: [rect(6, 4), { type: 'circle' as const, cx: 1, cy: 1, r: 0.25 }],
    };
    const placed = placement(part, 10, 20, 0, rotation);
    const loops = transformLoops(part, placed);
    if (loops[0].type !== 'poly') throw new Error('Expected transformed polygon.');
    expect(loops[0].points[0]).toEqual({ x: vertex[0], y: vertex[1] });
    expect(loops[1]).toEqual({ type: 'circle', cx: center[0], cy: center[1], r: 0.25 });
    expect(bounds(loops[0])).toEqual({ x: 10, y: 20, width: placed.width, height: placed.height });
    expect(() => validateNest([part], makeStock(100, 100), proposedNest([part], [placed]))).not.toThrow();
  });
});

describe('bounded contour workloads', () => {
  it('places twenty distinct circles, concave profiles, and rectangles with physical clearance', () => {
    const parts = Array.from({ length: 20 }, (_, index) => {
      const loop: Loop =
        index % 3 === 0
          ? {
              type: 'poly',
              points: [
                { x: 0, y: 0 },
                { x: 40 + index, y: 0 },
                { x: 40 + index, y: 12 },
                { x: 12, y: 12 },
                { x: 12, y: 40 + index },
                { x: 0, y: 40 + index },
              ],
            }
          : index % 3 === 1
            ? circle(10 + index / 5)
            : rect(18 + index, 12 + index / 3);
      return makePart(`mixed-${index}`, loop);
    });
    const stock = makeStock(500, 500, 2, 3);
    const started = performance.now();
    const result = nestParts(parts, stock);
    expect(performance.now() - started).toBeLessThan(15000);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(20);
    expect(new Set(result.placements.map(p => p.partId)).size).toBe(20);
    expect(result.sheets).toBe(1);
    assertPhysicalLayout(parts, stock, result);
  }, 30000);

  it('conserves 300 instances from 100 DXFs and produces the same layout on repeated runs', () => {
    const dxf = [
      0,
      'SECTION',
      2,
      'HEADER',
      9,
      '$INSUNITS',
      70,
      4,
      0,
      'ENDSEC',
      0,
      'SECTION',
      2,
      'ENTITIES',
      0,
      'LWPOLYLINE',
      90,
      4,
      70,
      1,
      10,
      0,
      20,
      0,
      10,
      10,
      20,
      0,
      10,
      10,
      20,
      5,
      10,
      0,
      20,
      5,
      0,
      'ENDSEC',
      0,
      'EOF',
      '',
    ].join('\n');
    const parts = Array.from({ length: 100 }, (_, index) => ({
      ...importDXF(dxf, `plate-${index}.dxf`)[0],
      quantity: 3,
    }));
    const stock = makeStock(600, 600, 1, 2);
    const before = JSON.stringify(parts);
    const started = performance.now();
    const result = nestParts(parts, stock);
    // A synchronous solver cannot be interrupted by Jest's async timeout.
    // This generous ceiling catches minute-long regressions, not small CI variance.
    expect(performance.now() - started).toBeLessThan(15000);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(300);
    expect(new Set(result.placements.map(p => `${p.partId}:${p.instance}`)).size).toBe(300);
    for (const part of parts)
      expect(
        result.placements
          .filter(p => p.partId === part.id)
          .map(p => p.instance)
          .sort()
      ).toEqual([0, 1, 2]);
    expect(result.sheets).toBe(1);
    expect(result.area).toBeCloseTo(15000);
    expect(nestParts(parts, stock)).toEqual(result);
    expect(JSON.stringify(parts)).toBe(before);
    assertPhysicalLayout(parts, stock, result);
  }, 30000);
});

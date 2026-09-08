import { analyzeLeftovers } from './leftovers';
import {
  bounds,
  normalizeLoops,
  rect,
  type Loop,
  type Nest,
  type Part,
  type Placement,
  type Point,
  type Stock,
} from './nesting';

const part = (outer: Loop = rect(20, 10)): Part => ({
  id: 'plate',
  name: 'Synthetic plate',
  loops: [outer],
  quantity: 1,
  rotate: true,
  color: 0,
});
const stock = (width = 100, height = 80, margin = 5, gap = 0): Stock => ({
  width,
  height,
  margin,
  gap,
  bedWidth: width,
  bedHeight: height,
  maxSheets: 3,
});
function placement(
  value: Part,
  x = 20,
  y = 20,
  sheet = 0,
  instance = 0,
  rotation: Placement['rotation'] = 0
): Placement {
  const box = bounds(value.loops[0]);
  return {
    partId: value.id,
    instance,
    x,
    y,
    sheet,
    rotation,
    width: rotation % 180 ? box.height : box.width,
    height: rotation % 180 ? box.width : box.height,
  };
}
const nest = (placements: Placement[]): Nest => ({
  placements,
  unplaced: [],
  sheets: Math.max(...placements.map(p => p.sheet)) + 1,
  area: 123456,
  utilization: 99,
  method: 'Synthetic validated layout',
});

// Separate mathematical oracle: shoelace area and point-to-segment distance.
// Neither calls the packing engine, Clipper, nor the analysis implementation.
function polygonArea(points: Point[]) {
  return (
    Math.abs(
      points.reduce((sum, p, index) => {
        const q = points[(index + 1) % points.length];
        return sum + p.x * q.y - q.x * p.y;
      }, 0)
    ) / 2
  );
}
function distanceToSegment(point: Point, a: Point, b: Point) {
  const dx = b.x - a.x,
    dy = b.y - a.y;
  const t = Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(point.x - a.x - t * dx, point.y - a.y - t * dy);
}
function inside(point: Point, polygon: Point[]) {
  let result = false;
  for (let index = 0; index < polygon.length; index++) {
    const a = polygon[index],
      b = polygon[(index + 1) % polygon.length];
    if (a.y > point.y !== b.y > point.y && point.x < a.x + ((point.y - a.y) * (b.x - a.x)) / (b.y - a.y))
      result = !result;
  }
  return result;
}
function checkLedger(result: ReturnType<typeof analyzeLeftovers>, sheet: Stock) {
  expect(result).toMatchObject({ version: 'werco-leftovers-v1', status: 'potential_review_only', creditUSD: 0 });
  expect(result.assumptions).toMatchObject({
    internalHolesReserved: true,
    boundsAreUsableRectangles: false,
    eligibilityVerified: false,
  });
  for (const row of result.sheets) {
    expect(row.grossArea).toBeCloseTo(sheet.width * sheet.height, 8);
    expect(row.usableArea).toBeCloseTo((sheet.width - 2 * sheet.margin) * (sheet.height - 2 * sheet.margin), 8);
    expect(
      row.edgeMarginArea +
        row.nominalPartArea +
        row.reservedCutoutArea +
        row.clearanceAndProtectionArea +
        row.remainingArea
    ).toBeCloseTo(row.grossArea, 7);
    expect(row.regions.reduce((area, region) => area + region.area, 0)).toBeCloseTo(row.remainingArea, 7);
    expect(row.clearanceAndProtectionArea).toBeGreaterThanOrEqual(0);
    for (const region of row.regions) {
      expect(region).toMatchObject({ classification: 'review', creditUSD: 0 });
      expect(region.explanation.length).toBeGreaterThan(0);
      expect(region.area).toBeGreaterThan(0);
      expect(polygonArea(region.outer) - region.holes.reduce((area, hole) => area + polygonArea(hole), 0)).toBeCloseTo(
        region.area,
        7
      );
      for (const point of region.outer.concat(...region.holes)) {
        expect(point.x).toBeGreaterThanOrEqual(sheet.margin);
        expect(point.y).toBeGreaterThanOrEqual(sheet.margin);
        expect(point.x).toBeLessThanOrEqual(sheet.width - sheet.margin);
        expect(point.y).toBeLessThanOrEqual(sheet.height - sheet.margin);
      }
    }
  }
}

describe('conservative leftover geometry', () => {
  it('balances an independent rectangular ledger without trusting cached nest area or utilization', () => {
    const plate = part(),
      sheet = stock();
    const result = analyzeLeftovers([plate], sheet, nest([placement(plate)]));
    checkLedger(result, sheet);
    const row = result.sheets[0];
    expect(row.nominalPartArea).toBe(200);
    expect(row.edgeMarginArea).toBe(1700);
    expect(row.reservedCutoutArea).toBe(0);
    expect(row.regions).toHaveLength(1);
    expect(row.regions[0].holes).toHaveLength(1);
    // Analytical leftover is 6100 mm². The declared 0.0004 mm inward/outward
    // numerical protection can only reduce it; 0.2 mm² bounds that loss here.
    expect(row.remainingArea).toBeLessThan(6100);
    expect(row.remainingArea).toBeGreaterThan(6099.8);
  });

  it('conservatively encloses an analytic circle plus half-gap and imported curve uncertainty', () => {
    const radius = 10,
      tolerance = 0.02,
      gap = 2;
    const disk = { ...part({ type: 'circle', cx: radius, cy: radius, r: radius }), geometryToleranceMm: tolerance };
    const sheet = stock(100, 80, 5, gap);
    const result = analyzeLeftovers([disk], sheet, nest([placement(disk, 30, 25)]));
    checkLedger(result, sheet);
    const row = result.sheets[0],
      requiredRadius = radius + gap / 2 + tolerance;
    expect(row.nominalPartArea).toBeCloseTo(Math.PI * radius ** 2, 9);
    expect(row.remainingArea).toBeLessThanOrEqual(row.usableArea - Math.PI * requiredRadius ** 2);
    const maximumRadius = radius + 0.00254 + Math.SQRT2 * (gap / 2 + tolerance + 0.0004) + 0.0002;
    const guardedUsable = (90 - 0.0008) * (70 - 0.0008);
    expect(row.remainingArea).toBeGreaterThanOrEqual(guardedUsable - Math.PI * maximumRadius ** 2);
    expect(row.regions).toHaveLength(1);
    expect(row.regions[0].holes).toHaveLength(1);
    const hole = row.regions[0].holes[0];
    for (let index = 0; index < hole.length; index++) {
      expect(distanceToSegment({ x: 40, y: 35 }, hole[index], hole[(index + 1) % hole.length])).toBeGreaterThanOrEqual(
        requiredRadius - 1e-7
      );
    }
  });

  it('reserves internal cutouts instead of listing them as reusable leftover islands', () => {
    const plate = { ...part(rect(30, 30)), loops: [rect(30, 30), { type: 'circle' as const, cx: 15, cy: 15, r: 5 }] };
    const sheet = stock();
    const result = analyzeLeftovers([plate], sheet, nest([placement(plate, 20, 20)]));
    checkLedger(result, sheet);
    expect(result.sheets[0].nominalPartArea).toBeCloseTo(900 - Math.PI * 25, 8);
    expect(result.sheets[0].reservedCutoutArea).toBeCloseTo(Math.PI * 25, 8);
    for (const region of result.sheets[0].regions) {
      const cutout = { x: 35, y: 35 };
      expect(inside(cutout, region.outer) && !region.holes.some(hole => inside(cutout, hole))).toBe(false);
    }
    expect(result.sheets[0].remainingArea).toBeLessThan(6300 - 900);
  });

  it('splits leftovers into two connected components when a part spans the usable sheet', () => {
    const divider = part(rect(10, 70)),
      sheet = stock();
    const result = analyzeLeftovers([divider], sheet, nest([placement(divider, 45, 5)]));
    checkLedger(result, sheet);
    expect(result.sheets[0].regions).toHaveLength(2);
    expect(result.sheets[0].regions.every(region => region.holes.length === 0)).toBe(true);
    expect(result.sheets[0].regions.some(region => region.bounds.x + region.bounds.width < 45)).toBe(true);
    expect(result.sheets[0].regions.some(region => region.bounds.x > 55)).toBe(true);
    expect(result.sheets[0].remainingArea).toBeLessThan(5600);
    expect(result.sheets[0].remainingArea).toBeGreaterThan(5599.8);
  });

  it('keeps a connected one-millimeter skeleton under review with no material credit', () => {
    const plate = part(rect(98, 78)),
      sheet = stock(100, 80, 0);
    const result = analyzeLeftovers([plate], sheet, nest([placement(plate, 1, 1)]));
    checkLedger(result, sheet);
    expect(result.sheets[0].regions).toHaveLength(1);
    expect(result.sheets[0].regions[0].holes).toHaveLength(1);
    expect(result.sheets[0].remainingArea).toBeGreaterThan(350);
    expect(result.sheets[0].remainingArea).toBeLessThan(356);
    expect(result.creditUSD).toBe(0);
    expect(result.sheets[0].regions[0].classification).toBe('review');
  });

  it('accounts for each placed instance on its sheet and is deterministic under placement ordering', () => {
    const plate = { ...part(), quantity: 3 },
      sheet = stock();
    const placed = [placement(plate, 10, 10, 0, 0), placement(plate, 60, 40, 0, 1), placement(plate, 20, 20, 1, 2)];
    const input = nest(placed),
      before = JSON.stringify({ plate, sheet, input });
    const first = analyzeLeftovers([plate], sheet, input);
    checkLedger(first, sheet);
    expect(first.sheets.map(row => row.nominalPartArea)).toEqual([400, 200]);
    expect(new Set(first.sheets.flatMap(row => row.regions.map(region => region.id))).size).toBe(
      first.sheets.reduce((count, row) => count + row.regions.length, 0)
    );
    expect(analyzeLeftovers([plate], sheet, { ...input, placements: [...placed].reverse() })).toEqual(first);
    expect(JSON.stringify({ plate, sheet, input })).toBe(before);
  });

  it('uses transformed placement coordinates rather than the original drawing origin', () => {
    const original = part();
    const shifted = {
      ...original,
      loops: [
        {
          type: 'poly' as const,
          points: [
            { x: 300, y: -200 },
            { x: 320, y: -200 },
            { x: 320, y: -190 },
            { x: 300, y: -190 },
          ],
        },
      ],
    };
    const sheet = stock();
    const placed = placement(original, 30, 15, 0, 0, 90);
    expect(() => analyzeLeftovers([shifted], sheet, nest([placed]))).toThrow(/normalized/i);
    expect(analyzeLeftovers([{ ...shifted, loops: normalizeLoops(shifted.loops) }], sheet, nest([placed]))).toEqual(
      analyzeLeftovers([original], sheet, nest([placed]))
    );
  });

  it('rejects invalid placement geometry and returns no phantom sheet for an entirely unplaced order', () => {
    const plate = part(),
      sheet = stock();
    expect(() => analyzeLeftovers([plate], sheet, nest([placement(plate, -1, 10)]))).toThrow(/margin/i);
    const duplicate = { ...plate, quantity: 2 };
    expect(() =>
      analyzeLeftovers([duplicate], sheet, nest([placement(duplicate), placement(duplicate, 20, 20, 0, 1)]))
    ).toThrow(/spacing/i);
    const empty: Nest = {
      placements: [],
      unplaced: [{ partId: plate.id, count: 1, reason: 'No permitted orientation' }],
      sheets: 0,
      area: 0,
      utilization: 0,
      method: 'Synthetic incomplete layout',
    };
    expect(analyzeLeftovers([plate], sheet, empty).sheets).toEqual([]);
  });
});

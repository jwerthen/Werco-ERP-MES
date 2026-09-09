import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { analyzeLeftovers } from './leftovers';
import { bounds, nestParts, rect, validateNest, type Nest, type Part, type Placement, type Stock } from './nesting';

const sheet = (): Stock => ({
  width: 100,
  height: 80,
  bedWidth: 100,
  bedHeight: 80,
  gap: 2,
  margin: 3,
  maxSheets: 1,
  geometryProfile: CURRENT_GEOMETRY_PROFILE,
});
const plate = (): Part => ({
  id: 'plate',
  name: 'Synthetic plate',
  quantity: 1,
  rotate: true,
  color: 0,
  loops: [rect(10, 4)],
  geometryToleranceMm: 0.01254,
});
const placement = (part: Part, x: number, y: number, rotation: Placement['rotation'] = 0, instance = 0): Placement => {
  const box = bounds(part.loops[0]);
  return {
    partId: part.id,
    instance,
    rotation,
    x,
    y,
    sheet: 0,
    width: rotation % 180 ? box.height : box.width,
    height: rotation % 180 ? box.width : box.height,
  };
};
const layout = (...placements: Placement[]): Nest => ({
  placements,
  unplaced: [],
  sheets: 1,
  area: 0,
  utilization: 0,
  method: 'Independent edge oracle',
});

test.each(['left', 'right', 'bottom', 'top'] as const)(
  '%s edge permits exact conservative contact but rejects one grid step beyond it',
  edge => {
    const part = plate(),
      stock = sheet(),
      rotation = 90;
    // Independently calculated in integer grid units: requested margin 30,000
    // plus four inward units; ceil((half gap + .01254 + .0004) / .0001) = 10,130.
    const inset = 30_004,
      reserve = 10_130,
      width = 40_000,
      height = 100_000;
    const min = (inset + reserve) / 10_000;
    const maxX = (1_000_000 - inset - reserve - width) / 10_000;
    const maxY = (800_000 - inset - reserve - height) / 10_000;
    const x = edge === 'left' ? min : edge === 'right' ? maxX : 20;
    const y = edge === 'bottom' ? min : edge === 'top' ? maxY : 20;
    expect(() => validateNest([part], stock, layout(placement(part, x, y, rotation)))).not.toThrow();
    const badX =
      edge === 'left'
        ? (inset + reserve - 1) / 10_000
        : edge === 'right'
          ? (1_000_000 - inset - reserve - width + 1) / 10_000
          : x;
    const badY =
      edge === 'bottom'
        ? (inset + reserve - 1) / 10_000
        : edge === 'top'
          ? (800_000 - inset - reserve - height + 1) / 10_000
          : y;
    const bad = layout(placement(part, badX, badY, rotation));
    expect(() => validateNest([part], stock, bad)).toThrow(/envelope.*usable sheet/);
    const legacy = { ...stock };
    delete legacy.geometryProfile;
    expect(() => validateNest([part], legacy, bad)).not.toThrow();
  }
);

test('exact compensated edge contact is allowed while one grid unit of overlap is rejected', () => {
  const part = { ...plate(), quantity: 2, geometryToleranceMm: 0 };
  const stock = sheet();
  const first = placement(part, 10, 10);
  // Two one-mm half gaps plus two four-grid numerical guards.
  const tangent = 220_008 / 10_000;
  expect(() => validateNest([part], stock, layout(first, placement(part, tangent, 10, 0, 1)))).not.toThrow();
  const inward = layout(first, placement(part, 220_007 / 10_000, 10, 0, 1));
  expect(() => validateNest([part], stock, inward)).toThrow(/envelopes overlap/);
  const legacy = { ...stock };
  delete legacy.geometryProfile;
  expect(() => validateNest([part], legacy, inward)).not.toThrow();
});

test('circle accounting bounds the analytical radial reserve and leaves original inputs untouched', () => {
  const part: Part = { ...plate(), loops: [{ type: 'circle', cx: 5, cy: 5, r: 5 }], quantity: 3 };
  const stock = sheet();
  const original = JSON.stringify({ part, stock });
  const result = nestParts([part], stock);
  expect(result.placements).toHaveLength(3);
  expect(result.unplaced).toEqual([]);
  const report = analyzeLeftovers([part], stock, result),
    row = report.sheets[0];
  const reserve = 1 + 0.01254 + 0.0004;
  const occupied = row.nominalPartArea + row.reservedCutoutArea + row.clearanceAndProtectionArea;
  // An outward circumscribed circle and square joins enclose this ideal disk;
  // their declared maximum radial extensions give a separate conservative cap.
  expect(occupied).toBeGreaterThanOrEqual(3 * Math.PI * (5 + reserve) ** 2);
  expect(occupied).toBeLessThan(3 * Math.PI * (5 + 0.00254 + Math.SQRT2 * (reserve + 0.0001) + 0.0001) ** 2 + 1);
  expect(row.excludedArea).toBe(0);
  expect(report.creditUSD).toBe(0);
  expect(JSON.stringify({ part, stock })).toBe(original);
  expect(nestParts([part], stock)).toEqual(result);
});

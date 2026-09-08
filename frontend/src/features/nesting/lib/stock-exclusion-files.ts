import type { Loop } from './nesting';
import { validateStockExclusions, type StockExclusion } from './stock-exclusions';

const inch = 25.4;
function check(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
function object(value: unknown, fields: string[]): Record<string, unknown> {
  check(value && typeof value === 'object' && !Array.isArray(value), 'Invalid stock exclusion object.');
  const row = value as Record<string, unknown>;
  check(
    Object.keys(row).length === fields.length && fields.every(key => Object.prototype.hasOwnProperty.call(row, key)),
    'Stock exclusion contains missing or unsupported fields.'
  );
  return row;
}
const dimension = (value: unknown, factor: number) => {
  check(typeof value === 'number' && Number.isFinite(value), 'Invalid stock exclusion dimension.');
  const scaled = value * factor;
  check(Number.isFinite(scaled), 'Stock exclusion dimension exceeds the supported range.');
  return scaled;
};
function outlineFromFile(value: unknown): Loop {
  check(value && typeof value === 'object', 'Invalid stock exclusion outline.');
  const kind = (value as { type?: unknown }).type;
  if (kind === 'circle') {
    const row = object(value, ['type', 'cx', 'cy', 'r']);
    return { type: 'circle', cx: dimension(row.cx, inch), cy: dimension(row.cy, inch), r: dimension(row.r, inch) };
  }
  check(kind === 'poly', 'Stock exclusion must be a circle or closed polygon.');
  const row = object(value, ['type', 'points']);
  check(
    Array.isArray(row.points) && row.points.length >= 3 && row.points.length <= 2000,
    'Stock exclusion polygon exceeds the supported vertex count.'
  );
  return {
    type: 'poly',
    points: row.points.map(value => {
      const point = object(value, ['x', 'y']);
      return { x: dimension(point.x, inch), y: dimension(point.y, inch) };
    }),
  };
}

/** Parse only the declared inch format, then validate the actual sheet-local geometry. */
export function exclusionsFromFile(input: unknown, widthMm: number, heightMm: number): StockExclusion[] {
  check(Array.isArray(input) && input.length <= 16, 'A sheet option supports at most 16 stock exclusions.');
  const regions = input.map(value => {
    const row = object(value, ['id', 'label', 'reason', 'outline', 'clearance']);
    return {
      id: row.id,
      label: row.label,
      reason: row.reason,
      outline: outlineFromFile(row.outline),
      clearance: dimension(row.clearance, inch),
    };
  });
  validateStockExclusions(regions, widthMm, heightMm);
  return regions;
}

export function exclusionsToFile(regions: StockExclusion[]) {
  return regions.map(region => ({
    ...region,
    clearance: region.clearance / inch,
    outline:
      region.outline.type === 'circle'
        ? {
            type: 'circle' as const,
            cx: region.outline.cx / inch,
            cy: region.outline.cy / inch,
            r: region.outline.r / inch,
          }
        : {
            type: 'poly' as const,
            points: region.outline.points.map(point => ({ x: point.x / inch, y: point.y / inch })),
          },
  }));
}

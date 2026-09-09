import type * as Clipper from 'clipper-lib';
import { bounds, validateLoop, type Loop, type Stock } from './nesting';
import { outlinesCollide } from './contour-packing';
import {
  GUARDED_GEOMETRY_PROFILE,
  guardedOuter,
  integerOuter,
  pointPath,
  prepareGuardedPaths,
  preparedEnvelopesOverlap,
  type PreparedGuardedPaths,
} from './guarded-geometry';

export type StockExclusion = {
  id: string;
  label: string;
  reason: string;
  outline: Loop;
  clearance: number;
};
/** Quote-only engineering profile; no machine zone or physical inventory meaning. */
export const EXCLUSION_PROFILE = Object.freeze({
  version: 'werco-stock-exclusions-v1' as const,
  integerGridMm: GUARDED_GEOMETRY_PROFILE.integerGridMm,
  circleRadialExcessMm: GUARDED_GEOMETRY_PROFILE.circleRadialExcessMm,
  numericalProtectionMm: GUARDED_GEOMETRY_PROFILE.numericalProtectionMm,
  partGapFraction: GUARDED_GEOMETRY_PROFILE.partGapFraction,
  offsetJoin: GUARDED_GEOMETRY_PROFILE.offsetJoin,
  maximumConvexJoinRadiusFactor: GUARDED_GEOMETRY_PROFILE.maximumConvexJoinRadiusFactor,
  maxRegions: 16,
  maxSourceVertices: 2000,
  maxProjectSourceVertices: 20000,
  maxClearanceMm: 100 * 25.4,
  maxGuardedVertices: 60000,
  maxIntersectionEdgePairs: GUARDED_GEOMETRY_PROFILE.maxIntersectionEdgePairs,
});
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Stock exclusions: ${message}`);
};
export const exclusionVertexCount = (regions: StockExclusion[]): number =>
  regions.reduce((sum, region) => sum + (region.outline.type === 'circle' ? 1 : region.outline.points.length), 0);

/** Validate exact source coordinates; never translate, crop, simplify, sort or mutate them. */
export function validateStockExclusions(
  regions: unknown,
  width: number,
  height: number
): asserts regions is StockExclusion[] {
  check(
    Array.isArray(regions) && regions.length <= EXCLUSION_PROFILE.maxRegions,
    'use at most 16 regions per sheet option.'
  );
  check(
    Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0 && width <= 20000 && height <= 20000,
    'invalid gross sheet dimensions.'
  );
  const seen = new Set<string>();
  let vertices = 0;
  for (const value of regions as StockExclusion[]) {
    check(value && typeof value === 'object', 'invalid region.');
    check(
      typeof value.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value.id) && !seen.has(value.id),
      'invalid or duplicate region ID.'
    );
    seen.add(value.id);
    check(
      typeof value.label === 'string' &&
        value.label === value.label.trim() &&
        value.label.length > 0 &&
        value.label.length <= 120,
      'region labels need 1–120 trimmed characters.'
    );
    check(
      typeof value.reason === 'string' &&
        value.reason === value.reason.trim() &&
        value.reason.length > 0 &&
        value.reason.length <= 1000,
      'region reasons need 1–1,000 trimmed characters.'
    );
    check(
      Number.isFinite(value.clearance) && value.clearance >= 0 && value.clearance <= EXCLUSION_PROFILE.maxClearanceMm,
      'entered clearance must be 0–100 inches.'
    );
    validateLoop(value.outline);
    const box = bounds(value.outline);
    check(
      box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height,
      'region outline must lie wholly inside the gross sheet; it cannot be cropped automatically.'
    );
    vertices += value.outline.type === 'circle' ? 1 : value.outline.points.length;
    check(vertices <= EXCLUSION_PROFILE.maxSourceVertices, 'sheet regions exceed the 2,000 source-vertex budget.');
    // Refuse a grid-collapsed or topologically changed obstacle instead of dropping it.
    const quantized = integerOuter(value.outline);
    if (value.outline.type === 'poly') validateLoop({ type: 'poly', points: pointPath(quantized) });
  }
}
export type PreparedExclusion = {
  source: StockExclusion;
  paths: Clipper.Paths;
  prepared: PreparedGuardedPaths;
  bounds: ReturnType<typeof bounds>;
};
export function prepareExclusions(stock: Pick<Stock, 'width' | 'height' | 'exclusions'>): PreparedExclusion[] {
  if (stock.exclusions === undefined) return [];
  validateStockExclusions(stock.exclusions, stock.width, stock.height);
  let vertices = 0;
  return stock.exclusions.map(source => {
    const paths = guardedOuter(source.outline, source.clearance);
    vertices += paths.reduce((sum, path) => sum + path.length, 0);
    check(vertices <= EXCLUSION_PROFILE.maxGuardedVertices, 'regions exceed the 60,000 guarded-vertex budget.');
    return {
      source,
      paths,
      prepared: prepareGuardedPaths(paths),
      bounds: bounds({ type: 'poly', points: paths.flatMap(pointPath) }),
    };
  });
}
/** Independent nominal constraint plus the identical compensated vector envelopes used by the ledger. */
export function exclusionCollision(
  outer: Loop,
  gap: number,
  curveTolerance: number,
  regions: PreparedExclusion[]
): StockExclusion | undefined {
  if (!regions.length) return undefined;
  const partReserve = gap * EXCLUSION_PROFILE.partGapFraction + curveTolerance;
  for (const { source } of regions)
    if (
      outlinesCollide(
        outer,
        source.outline,
        partReserve + source.clearance + 2 * EXCLUSION_PROFILE.numericalProtectionMm
      )
    )
      return source;
  const box = bounds(outer);
  const protection =
    (partReserve + EXCLUSION_PROFILE.numericalProtectionMm) * Math.SQRT2 +
    EXCLUSION_PROFILE.circleRadialExcessMm +
    EXCLUSION_PROFILE.integerGridMm;
  const nearby = regions.filter(
    ({ bounds: other }) =>
      box.x - protection <= other.x + other.width &&
      box.x + box.width + protection >= other.x &&
      box.y - protection <= other.y + other.height &&
      box.y + box.height + protection >= other.y
  );
  if (!nearby.length) return undefined;
  const envelope = prepareGuardedPaths(guardedOuter(outer, partReserve));
  for (const { source, prepared } of nearby) if (preparedEnvelopesOverlap(envelope, prepared)) return source;
  return undefined;
}

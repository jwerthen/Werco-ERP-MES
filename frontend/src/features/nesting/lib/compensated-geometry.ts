import { resolveGeometryProfile } from './geometry-profile';
import { bounds, type Loop, type Part, type Placement, type Stock } from './nesting';
import { movedOuter, outlinesCollide } from './contour-packing';
import { allowedRotations } from './orientation';
import {
  guardedOuter,
  inwardSheet,
  prepareGuardedPaths,
  preparedEnvelopesOverlap,
  SCALE,
  type GuardedGeometrySettings,
  type PreparedGuardedPaths,
} from './guarded-geometry';

export type CompensatedPart = {
  part: Part;
  rotation: Placement['rotation'];
  outer: Loop;
  envelope: PreparedGuardedPaths;
};
export type CompensatedPose = { part: CompensatedPart; x: number; y: number };

/** Per-run immutable origin envelopes. Poses are exact integer translations, never re-offset world contours. */
export function prepareCompensatedGeometry(parts: Part[], stock: Stock) {
  const profile = resolveGeometryProfile(stock.geometryProfile);
  if (!profile) return undefined;
  const settings: GuardedGeometrySettings = {
    ...profile.numerics,
    ...profile.budgets,
  };
  if (1 / profile.numerics.integerGridMm !== SCALE)
    throw new Error('The compensated profile grid is not supported by this integer geometry kernel.');
  const usable = inwardSheet(stock, settings);
  const shapes = new Map<string, CompensatedPart>();
  let vertices = 0;
  const addVertices = (count: number) => {
    vertices += count;
    if (vertices > profile.budgets.maxEnvelopeVertices)
      throw new Error('Compensated geometry exceeds the prepared-envelope vertex budget.');
  };
  const get = (part: Part, rotation: Placement['rotation']): CompensatedPart => {
    if (!parts.includes(part)) throw new Error('Unknown part in compensated geometry context.');
    const key = JSON.stringify([part.id, rotation]);
    let result = shapes.get(key);
    if (!result) {
      const b = bounds(part.loops[0]);
      const outer = movedOuter(part, {
        partId: part.id,
        instance: 0,
        sheet: 0,
        x: 0,
        y: 0,
        rotation,
        width: rotation % 180 ? b.height : b.width,
        height: rotation % 180 ? b.width : b.height,
      });
      const paths = guardedOuter(
        outer,
        stock.gap * profile.numerics.partGapFraction + (part.geometryToleranceMm ?? 0),
        settings
      );
      addVertices(paths.reduce((total, path) => total + path.length, 0));
      result = Object.freeze({ part, rotation, outer, envelope: prepareGuardedPaths(paths) });
      shapes.set(key, result);
    }
    return result;
  };
  const exclusions = (stock.exclusions ?? []).map(source => {
    const paths = guardedOuter(source.outline, source.clearance, settings);
    addVertices(paths.reduce((total, path) => total + path.length, 0));
    return { source, paths, prepared: prepareGuardedPaths(paths), bounds: bounds(source.outline) };
  });
  if (
    exclusions.reduce((total, region) => total + region.paths.reduce((n, path) => n + path.length, 0), 0) >
    profile.budgets.maxExclusionGuardedVertices
  )
    throw new Error('Compensated stock exclusions exceed the guarded-vertex budget.');
  const originRange = (part: CompensatedPart) => {
    if (!usable) return null;
    const p = part.envelope.bounds;
    const minX = usable[0].X - p.minX,
      minY = usable[0].Y - p.minY;
    const maxX = usable[2].X - p.maxX,
      maxY = usable[2].Y - p.maxY;
    return minX <= maxX && minY <= maxY ? { minX, minY, maxX, maxY } : null;
  };
  const pose = (part: Part, placement: Pick<Placement, 'x' | 'y' | 'rotation'>): CompensatedPose => {
    const x = Math.round(placement.x * SCALE),
      y = Math.round(placement.y * SCALE);
    if (!Number.isSafeInteger(x) || !Number.isSafeInteger(y) || placement.x !== x / SCALE || placement.y !== y / SCALE)
      throw new Error('Compensated placements must use canonical integer-grid coordinates.');
    return { part: get(part, placement.rotation), x, y };
  };
  const inside = (value: CompensatedPose) => {
    const range = originRange(value.part);
    return Boolean(
      range && value.x >= range.minX && value.x <= range.maxX && value.y >= range.minY && value.y <= range.maxY
    );
  };
  const overlap = (a: CompensatedPose, b: CompensatedPose) =>
    preparedEnvelopesOverlap(a.part.envelope, b.part.envelope, b.x - a.x, b.y - a.y, settings);
  const exclusionCollision = (value: CompensatedPose, nominalOuter: Loop) => {
    for (const region of exclusions) {
      // Nominal distance is checked independently on the original unrounded
      // geometry. Numeric protection is enforced by the actual guarded paths.
      const distance =
        stock.gap * profile.numerics.partGapFraction +
        (value.part.part.geometryToleranceMm ?? 0) +
        region.source.clearance +
        2 * profile.numerics.numericalProtectionMm;
      if (
        outlinesCollide(nominalOuter, region.source.outline, distance) ||
        preparedEnvelopesOverlap(region.prepared, value.part.envelope, value.x, value.y, settings)
      )
        return region.source;
    }
    return undefined;
  };
  // Allowed orientations are prepared lazily: symmetric/forbidden poses never
  // consume the bounded geometry budget unless actually needed.
  return {
    profile,
    settings,
    usable,
    exclusions,
    get,
    originRange,
    pose,
    inside,
    overlap,
    exclusionCollision,
    translatedPaths: (value: CompensatedPose) =>
      value.part.envelope.paths.map(path => path.map(point => ({ X: point.X + value.x, Y: point.Y + value.y }))),
    fits: (part: Part) => allowedRotations(part, stock).some(rotation => originRange(get(part, rotation)) !== null),
  };
}
export type CompensatedGeometry = NonNullable<ReturnType<typeof prepareCompensatedGeometry>>;

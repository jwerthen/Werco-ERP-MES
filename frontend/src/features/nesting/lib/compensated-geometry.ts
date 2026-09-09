import { resolveGeometryProfile } from './geometry-profile';
import { bounds, type Loop, type Part, type Placement, type Stock } from './nesting';
import { movedOuter, outlinesCollide } from './contour-packing';
import { allowedRotations } from './orientation';
import { prepareStockDomain, type DomainStock } from './remnant-domain';
import { domainContains, prepareDomainBoundary, type DomainBoundary } from './domain-containment';
import { nominalDomainContains } from './nominal-domain';
import { prepareDomainCandidates } from './domain-candidates';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';
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
  domainBoundary?: DomainBoundary;
  domainMinimum?: number;
};
export type CompensatedPose = { part: CompensatedPart; x: number; y: number };

/** Per-run immutable origin envelopes. Poses are exact integer translations, never re-offset world contours. */
export function prepareCompensatedGeometry(parts: Part[], stock: Stock) {
  const profile = resolveGeometryProfile(stock.geometryProfile);
  if (stock.domain && !profile) throw new Error('Recorded pieces require the compensated geometry profile.');
  if (!profile) return undefined;
  const settings: GuardedGeometrySettings = {
    ...profile.numerics,
    ...profile.budgets,
  };
  if (1 / profile.numerics.integerGridMm !== SCALE)
    throw new Error('The compensated profile grid is not supported by this integer geometry kernel.');
  const usable = inwardSheet(stock, settings);
  const domain = stock.domain ? prepareStockDomain(stock as DomainStock) : undefined;
  const domainCandidates = domain ? prepareDomainCandidates(domain) : undefined;
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
      if (domain) {
        if (outer.type === 'poly') {
          outer.points.forEach(Object.freeze);
          Object.freeze(outer.points);
        }
        Object.freeze(outer);
      }
      addVertices(paths.reduce((total, path) => total + path.length, 0));
      result = Object.freeze({
        part,
        rotation,
        outer,
        envelope: prepareGuardedPaths(paths),
        ...(domain
          ? {
              domainBoundary: prepareDomainBoundary(paths),
              domainMinimum:
                stock.margin +
                stock.gap * profile.numerics.partGapFraction +
                (part.geometryToleranceMm ?? 0) +
                2 * profile.numerics.numericalProtectionMm,
            }
          : {}),
      });
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
    if (domain ? !domain.usable.length : !usable) return null;
    const p = part.envelope.bounds;
    const u = domain?.prepared.original.bounds;
    const minX = (u ? u.minX : usable![0].X) - p.minX,
      minY = (u ? u.minY : usable![0].Y) - p.minY;
    const maxX = (u ? u.maxX : usable![2].X) - p.maxX,
      maxY = (u ? u.maxY : usable![2].Y) - p.maxY;
    return minX <= maxX && minY <= maxY ? { minX, minY, maxX, maxY } : null;
  };
  const pose = (part: Part, placement: Pick<Placement, 'x' | 'y' | 'rotation'>): CompensatedPose => {
    const x = Math.round(placement.x * SCALE),
      y = Math.round(placement.y * SCALE);
    if (!Number.isSafeInteger(x) || !Number.isSafeInteger(y) || placement.x !== x / SCALE || placement.y !== y / SCALE)
      throw new Error('Compensated placements must use canonical integer-grid coordinates.');
    return { part: get(part, placement.rotation), x, y };
  };
  const insideUncached = (value: CompensatedPose) => {
    const range = originRange(value.part);
    const bounded = Boolean(
      range && value.x >= range.minX && value.x <= range.maxX && value.y >= range.minY && value.y <= range.maxY
    );
    if (!bounded || !domain) return bounded;
    if (!domainContains(value.part.domainBoundary!, domain.prepared, value.x, value.y)) return false;
    const original = value.part.outer;
    const nominal: Loop =
      original.type === 'circle'
        ? { ...original, cx: original.cx + value.x / SCALE, cy: original.cy + value.y / SCALE }
        : { type: 'poly', points: original.points.map(p => ({ x: p.x + value.x / SCALE, y: p.y + value.y / SCALE })) };
    return nominalDomainContains(nominal, domain.source.outer, domain.source.holes, value.part.domainMinimum!);
  };
  const containment = new Map<CompensatedPart, Map<string, boolean>>();
  let containmentEntries = 0;
  const inside = (value: CompensatedPose) => {
    if (!domain) return insideUncached(value);
    const key = `${value.x},${value.y}`;
    let poses = containment.get(value.part);
    const cached = poses?.get(key);
    if (cached !== undefined) return cached;
    const result = insideUncached(value);
    // A full cache stops retaining entries, not validating new poses. Coordinates
    // and immutable prepared source/part identity completely determine this check.
    if (containmentEntries < REMNANT_DOMAIN_RULES.budgets.maxContainmentCacheEntries) {
      if (!poses) {
        poses = new Map();
        containment.set(value.part, poses);
      }
      poses.set(key, result);
      containmentEntries++;
    }
    return result;
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
    domain,
    domainCandidates,
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

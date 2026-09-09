import * as Clipper from 'clipper-lib';
import type { Loop, Point, Stock } from './nesting';

/** Fixed numerical approximation, never a physical kerf or shop clearance policy. */
export const GUARDED_GEOMETRY_PROFILE = Object.freeze({
  integerGridMm: 0.0001,
  circleRadialExcessMm: 0.0001 * 25.4,
  numericalProtectionMm: 0.0004,
  usableBoundaryInsetMm: 0.0004,
  partGapFraction: 0.5,
  offsetJoin: 'square-tangent' as const,
  maximumConvexJoinRadiusFactor: Math.SQRT2,
  maxCircleVertices: 8192,
  maxEnvelopeVertices: 120000,
  maxIntersectionEdgePairs: 8_000_000,
});
export const SCALE = 1 / GUARDED_GEOMETRY_PROFILE.integerGridMm;
export type GuardedGeometrySettings = {
  circleRadialExcessMm: number;
  numericalProtectionMm: number;
  usableBoundaryInsetMm: number;
  maxCircleVertices: number;
  maxEnvelopeVertices: number;
  maxIntersectionEdgePairs: number;
  maxOffsetReserveMm?: number;
};
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Guarded geometry: ${message}`);
};

/** Integer-grid ring areas use exact integer products, even near the sheet-size limit. */
export function pathArea(path: Clipper.Path): number {
  if (path.length < 3) return 0;
  const origin = path[0];
  let twice = BigInt(0);
  for (let index = 0; index < path.length; index++) {
    const a = path[index],
      b = path[(index + 1) % path.length];
    twice += BigInt(a.X - origin.X) * BigInt(b.Y - origin.Y) - BigInt(b.X - origin.X) * BigInt(a.Y - origin.Y);
  }
  return Number(twice < BigInt(0) ? -twice : twice) / (2 * SCALE * SCALE);
}

export function canonicalPath(path: Clipper.Path, positive?: boolean): Clipper.Path {
  const points = path.map(point => ({ X: point.X, Y: point.Y }));
  if (positive !== undefined && Clipper.Clipper.Orientation(points) !== positive) points.reverse();
  let start = 0;
  for (let index = 1; index < points.length; index++)
    if (points[index].X < points[start].X || (points[index].X === points[start].X && points[index].Y < points[start].Y))
      start = index;
  return [...points.slice(start), ...points.slice(0, start)];
}
export const pathKey = (path: Clipper.Path) => path.map(point => `${point.X},${point.Y}`).join(';');
export const compareText = (left: string, right: string) => (left < right ? -1 : left > right ? 1 : 0);
export const pointPath = (path: Clipper.Path) => path.map(point => ({ x: point.X / SCALE, y: point.Y / SCALE }));
export const sortedPaths = (paths: Clipper.Paths) =>
  paths
    .map(path => canonicalPath(path))
    .sort((a, b) => {
      const left = pathKey(a),
        right = pathKey(b);
      return left < right ? -1 : left > right ? 1 : 0;
    });

export function circleSegments(radius: number, settings: GuardedGeometrySettings = GUARDED_GEOMETRY_PROFILE): number {
  const angle = Math.acos(1 / (1 + settings.circleRadialExcessMm / radius));
  const count = Math.max(16, Math.ceil(Math.PI / angle / 4) * 4);
  check(
    Number.isFinite(count) && count <= settings.maxCircleVertices,
    'circle tessellation exceeds the numerical budget.'
  );
  return count;
}
export function integerOuter(loop: Loop, settings: GuardedGeometrySettings = GUARDED_GEOMETRY_PROFILE): Clipper.Path {
  let points: Point[];
  if (loop.type === 'poly') points = loop.points;
  else {
    const count = circleSegments(loop.r, settings),
      radius = loop.r / Math.cos(Math.PI / count);
    // Mid-step vertices give tangent sides at the exact axial circle extrema.
    points = Array.from({ length: count }, (_, index) => {
      const angle = ((2 * index + 1) * Math.PI) / count;
      return { x: loop.cx + radius * Math.cos(angle), y: loop.cy + radius * Math.sin(angle) };
    });
  }
  const path = points.map(point => ({ X: Math.round(point.x * SCALE), Y: Math.round(point.y * SCALE) }));
  check(
    path.every(point => Number.isSafeInteger(point.X) && Number.isSafeInteger(point.Y)),
    'coordinates exceed integer precision.'
  );
  check(pathArea(path) > 0, 'an outer contour collapses at the analysis grid precision.');
  return canonicalPath(path, true);
}

export function inwardSheet(
  stock: Stock,
  settings: GuardedGeometrySettings = GUARDED_GEOMETRY_PROFILE
): Clipper.Path | null {
  const inset = settings.usableBoundaryInsetMm;
  const x0 = Math.ceil((stock.margin + inset) * SCALE),
    y0 = x0;
  const x1 = Math.floor((stock.width - stock.margin - inset) * SCALE);
  const y1 = Math.floor((stock.height - stock.margin - inset) * SCALE);
  return x1 > x0 && y1 > y0
    ? [
        { X: x0, Y: y0 },
        { X: x1, Y: y0 },
        { X: x1, Y: y1 },
        { X: x0, Y: y1 },
      ]
    : null;
}

/** A circumscribed, outward protected solid outer profile. Internal cutouts stay reserved. */
export function guardedOuter(
  loop: Loop,
  clearance: number,
  settings: GuardedGeometrySettings = GUARDED_GEOMETRY_PROFILE
): Clipper.Paths {
  const reserve = clearance + settings.numericalProtectionMm;
  check(
    Number.isFinite(clearance) && clearance >= 0 && reserve <= (settings.maxOffsetReserveMm ?? 40000),
    'reserve distance exceeds the bounded offset range.'
  );
  const path = integerOuter(loop, settings);
  const offset = new Clipper.ClipperOffset();
  offset.AddPath(path, Clipper.JoinType.jtSquare, Clipper.EndType.etClosedPolygon);
  const envelopes: Clipper.Paths = [];
  offset.Execute(envelopes, Math.ceil(reserve * SCALE));
  check(envelopes.length > 0, 'offset did not preserve an outer contour.');
  check(
    envelopes.reduce((n, p) => n + p.length, 0) <= settings.maxEnvelopeVertices,
    'guarded envelopes exceed the offset-vertex budget.'
  );
  return envelopes;
}

/** Preserve all components and hole winding in bounded Boolean operations. */
export function intersectPaths(
  a: Clipper.Paths,
  b: Clipper.Paths,
  maxVertices: number = GUARDED_GEOMETRY_PROFILE.maxEnvelopeVertices
): Clipper.Paths {
  if (!a.length || !b.length) return [];
  const clip = new Clipper.Clipper(),
    result: Clipper.Paths = [];
  clip.StrictlySimple = true;
  check(clip.AddPaths(a, Clipper.PolyType.ptSubject, true), 'invalid subject envelope.');
  check(clip.AddPaths(b, Clipper.PolyType.ptClip, true), 'invalid clipping envelope.');
  check(
    clip.Execute(
      Clipper.ClipType.ctIntersection,
      result,
      Clipper.PolyFillType.pftNonZero,
      Clipper.PolyFillType.pftNonZero
    ),
    'envelope intersection failed.'
  );
  check(result.reduce((n, p) => n + p.length, 0) <= maxVertices, 'intersection exceeds the output-vertex budget.');
  return result;
}
export function filledArea(paths: Clipper.Paths): number {
  return paths.reduce((sum, path) => sum + (Clipper.Clipper.Orientation(path) ? 1 : -1) * pathArea(path), 0);
}

/** Exact sign fallback prevents sub-grid crossings disappearing in floating products. */
function turn(a: Clipper.IntPoint, b: Clipper.IntPoint, c: Clipper.IntPoint): number {
  const left = (b.X - a.X) * (c.Y - a.Y),
    right = (b.Y - a.Y) * (c.X - a.X),
    d = left - right;
  if (Math.abs(d) > 8 * Number.EPSILON * (Math.abs(left) + Math.abs(right))) return Math.sign(d);
  const exact = BigInt(b.X - a.X) * BigInt(c.Y - a.Y) - BigInt(b.Y - a.Y) * BigInt(c.X - a.X);
  return exact > BigInt(0) ? 1 : exact < BigInt(0) ? -1 : 0;
}
export type IntegerBounds = { minX: number; minY: number; maxX: number; maxY: number };
type Edge = IntegerBounds & { a: Clipper.IntPoint; b: Clipper.IntPoint };
type PreparedRing = {
  path: Clipper.Path;
  bounds: IntegerBounds;
  edges: Edge[];
  yBuckets: Edge[][];
  bucketHeight: number;
  positive: boolean;
};
export type PreparedGuardedPaths = { paths: Clipper.Paths; bounds: IntegerBounds; rings: PreparedRing[] };
const integerBounds = (points: Clipper.Path): IntegerBounds => {
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (const p of points) {
    minX = Math.min(minX, p.X);
    minY = Math.min(minY, p.Y);
    maxX = Math.max(maxX, p.X);
    maxY = Math.max(maxY, p.Y);
  }
  return Object.freeze({ minX, minY, maxX, maxY });
};
const boundsMeet = (a: IntegerBounds, b: IntegerBounds) =>
  a.minX <= b.maxX && a.maxX >= b.minX && a.minY <= b.maxY && a.maxY >= b.minY;
const containsPoint = (b: IntegerBounds, p: Clipper.IntPoint) =>
  p.X >= b.minX && p.X <= b.maxX && p.Y >= b.minY && p.Y <= b.maxY;
/** Own immutable copies: cached bounds/indexes cannot outlive a caller mutation. */
export function prepareGuardedPaths(input: Clipper.Paths): PreparedGuardedPaths {
  const paths = input.map(path => path.map(point => Object.freeze({ X: point.X, Y: point.Y })));
  const rings = paths.map(path => {
    const bounds = integerBounds(path);
    const edges = path.map((a, i) => {
      const b = path[(i + 1) % path.length];
      return Object.freeze({ a, b, ...integerBounds([a, b]) });
    });
    // This is an edge lookup index, not a raster representation of geometry.
    const bucketCount = Math.max(1, Math.min(64, Math.ceil(Math.sqrt(edges.length))));
    const bucketHeight = Math.max(1, (bounds.maxY - bounds.minY) / bucketCount);
    const yBuckets: Edge[][] = Array.from({ length: bucketCount }, () => []);
    for (const edge of edges) {
      const first = Math.max(0, Math.min(bucketCount - 1, Math.floor((edge.minY - bounds.minY) / bucketHeight)));
      const last = Math.max(0, Math.min(bucketCount - 1, Math.floor((edge.maxY - bounds.minY) / bucketHeight)));
      for (let i = first; i <= last; i++) yBuckets[i].push(edge);
    }
    yBuckets.forEach(Object.freeze);
    Object.freeze(yBuckets);
    Object.freeze(edges);
    Object.freeze(path);
    return Object.freeze({ path, bounds, edges, yBuckets, bucketHeight, positive: Clipper.Clipper.Orientation(path) });
  });
  Object.freeze(paths);
  Object.freeze(rings);
  return Object.freeze({ paths, rings, bounds: integerBounds(paths.flat()) });
}
/** Exact ray parity using only edges whose Y bounds can meet this ray. */
function pointInRing(point: Clipper.IntPoint, ring: PreparedRing): number {
  if (!containsPoint(ring.bounds, point)) return 0;
  const bucket = Math.max(
    0,
    Math.min(ring.yBuckets.length - 1, Math.floor((point.Y - ring.bounds.minY) / ring.bucketHeight))
  );
  let inside = false;
  for (const edge of ring.yBuckets[bucket]) {
    if (point.Y < edge.minY || point.Y > edge.maxY) continue;
    const direction = turn(edge.a, edge.b, point);
    if (direction === 0 && point.X >= edge.minX && point.X <= edge.maxX) return -1;
    if (edge.a.Y > point.Y !== edge.b.Y > point.Y && (edge.b.Y > edge.a.Y ? direction > 0 : direction < 0))
      inside = !inside;
  }
  return inside ? 1 : 0;
}
/** Boolean area plus original integer edge crossings/containment. Never round away a tiny collision. */
export function preparedEnvelopesOverlap(
  a: PreparedGuardedPaths,
  b: PreparedGuardedPaths,
  dx = 0,
  dy = 0,
  settings: GuardedGeometrySettings = GUARDED_GEOMETRY_PROFILE
): boolean {
  check(Number.isSafeInteger(dx) && Number.isSafeInteger(dy), 'envelope translations must be exact grid integers.');
  const shiftedBounds = (value: IntegerBounds): IntegerBounds => ({
    minX: value.minX + dx,
    maxX: value.maxX + dx,
    minY: value.minY + dy,
    maxY: value.maxY + dy,
  });
  const shiftedPoint = (p: Clipper.IntPoint) => ({ X: p.X + dx, Y: p.Y + dy });
  if (!boundsMeet(a.bounds, shiftedBounds(b.bounds))) return false;
  const moved = dx || dy ? b.paths.map(path => path.map(shiftedPoint)) : b.paths;
  if (filledArea(intersectPaths(a.paths, moved, settings.maxEnvelopeVertices)) > 0) return true;
  let edgePairs = 0;
  for (const aa of a.rings)
    for (const bb of b.rings) {
      const bbBounds = shiftedBounds(bb.bounds);
      if (!boundsMeet(aa.bounds, bbBounds)) continue;
      // Bound interacting ring pairs before edge-level culling. Immutable
      // origin indexes are reused across poses; translations never rebuild them.
      edgePairs += aa.edges.length * bb.edges.length;
      check(
        edgePairs <= settings.maxIntersectionEdgePairs,
        'envelope comparison exceeds the bounded edge-pair budget.'
      );
      for (const first of aa.edges) {
        if (!boundsMeet(first, bbBounds)) continue;
        for (const second of bb.edges) {
          if (!boundsMeet(first, shiftedBounds(second))) continue;
          const p = first.a,
            q = first.b,
            r = shiftedPoint(second.a),
            s = shiftedPoint(second.b);
          if (turn(p, q, r) * turn(p, q, s) < 0 && turn(r, s, p) * turn(r, s, q) < 0) return true;
        }
      }
    }
  const strictlyInside = (point: Clipper.IntPoint, other: PreparedGuardedPaths) => {
    if (!containsPoint(other.bounds, point)) return false;
    let winding = 0;
    for (const ring of other.rings) {
      const where = pointInRing(point, ring);
      if (where === -1) return false;
      if (where === 1) winding += ring.positive ? 1 : -1;
    }
    return winding !== 0;
  };
  return (
    a.paths.some(path => path.some(point => strictlyInside({ X: point.X - dx, Y: point.Y - dy }, b))) ||
    b.paths.some(path => path.some(point => strictlyInside(shiftedPoint(point), a)))
  );
}
/** Raw callers always prepare fresh snapshots; no mutable-array WeakMap cache. */
export function envelopesOverlap(a: Clipper.Paths, b: Clipper.Paths): boolean {
  return preparedEnvelopesOverlap(prepareGuardedPaths(a), prepareGuardedPaths(b));
}

import * as Clipper from 'clipper-lib';
import { filledArea, prepareGuardedPaths, type PreparedGuardedPaths, type IntegerBounds } from './guarded-geometry';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

type Point = Clipper.IntPoint;
type Edge = IntegerBounds & { a: Point; b: Point };
export type DomainBoundary = {
  original: PreparedGuardedPaths;
  doubled: PreparedGuardedPaths;
  edges: Edge[];
};
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Stock-domain containment: ${message}`);
};
const key = (point: Point) => `${point.X},${point.Y}`;
const boundsMeet = (a: IntegerBounds, b: IntegerBounds) =>
  a.minX <= b.maxX && a.maxX >= b.minX && a.minY <= b.maxY && a.maxY >= b.minY;
const boundsHave = (b: IntegerBounds, p: Point) => p.X >= b.minX && p.X <= b.maxX && p.Y >= b.minY && p.Y <= b.maxY;

/** Exact sign on integer coordinates; float arithmetic is only a decisive-sign filter. */
export function domainTurn(a: Point, b: Point, c: Point): number {
  const left = (b.X - a.X) * (c.Y - a.Y),
    right = (b.Y - a.Y) * (c.X - a.X);
  const determinant = left - right;
  if (Math.abs(determinant) > 8 * Number.EPSILON * (Math.abs(left) + Math.abs(right))) return Math.sign(determinant);
  const exact = BigInt(b.X - a.X) * BigInt(c.Y - a.Y) - BigInt(b.Y - a.Y) * BigInt(c.X - a.X);
  return exact > BigInt(0) ? 1 : exact < BigInt(0) ? -1 : 0;
}

/** Input must already be canonical actual Boolean boundary rings, with hole winding. */
export function prepareDomainBoundary(paths: Clipper.Paths): DomainBoundary {
  check(
    paths.every(path => path.length >= 3),
    'boundary rings need at least three vertices.'
  );
  check(
    paths.reduce((n, path) => n + path.length, 0) <= REMNANT_DOMAIN_RULES.budgets.maxBooleanVertices,
    'boundary exceeds the vertex budget.'
  );
  for (const path of paths)
    for (const p of path)
      check(
        Number.isSafeInteger(p.X) &&
          Number.isSafeInteger(p.Y) &&
          Math.abs(p.X) <= 1_000_000_000 &&
          Math.abs(p.Y) <= 1_000_000_000,
        'boundary coordinates exceed exact doubled-grid precision.'
      );
  const original = prepareGuardedPaths(paths);
  const doubled = prepareGuardedPaths(paths.map(path => path.map(p => ({ X: 2 * p.X, Y: 2 * p.Y }))));
  const edges = original.rings.flatMap(ring => ring.edges);
  Object.freeze(edges);
  return Object.freeze({ original, doubled, edges });
}

function budget(limit: number) {
  check(
    Number.isSafeInteger(limit) && limit > 0 && limit <= REMNANT_DOMAIN_RULES.budgets.maxPredicateWork,
    'invalid exact-predicate work budget.'
  );
  let count = 0;
  return (amount = 1) => {
    count += amount;
    check(count <= limit, 'exact boundary predicate exceeded its work budget.');
  };
}

/** -1 boundary, 0 outside, 1 filled; the caller supplies integer doubled coordinates. */
function where(point: Point, paths: PreparedGuardedPaths, step: () => void): -1 | 0 | 1 {
  check(Number.isSafeInteger(point.X) && Number.isSafeInteger(point.Y), 'midpoint is not on the doubled grid.');
  if (!boundsHave(paths.bounds, point)) return 0;
  let winding = 0;
  for (const ring of paths.rings) {
    step();
    if (!boundsHave(ring.bounds, point)) continue;
    const bucket = Math.max(
      0,
      Math.min(ring.yBuckets.length - 1, Math.floor((point.Y - ring.bounds.minY) / ring.bucketHeight))
    );
    let inside = false;
    for (const edge of ring.yBuckets[bucket]) {
      step();
      if (point.Y < edge.minY || point.Y > edge.maxY) continue;
      const direction = domainTurn(edge.a, edge.b, point);
      if (direction === 0 && point.X >= edge.minX && point.X <= edge.maxX) return -1;
      if (edge.a.Y > point.Y !== edge.b.Y > point.Y && (edge.b.Y > edge.a.Y ? direction > 0 : direction < 0))
        inside = !inside;
    }
    if (inside) winding += ring.positive ? 1 : -1;
  }
  return winding === 0 ? 0 : 1;
}

/**
 * Entire filled P must be in closed U. Exact checks retain crossings that Clipper's
 * rounded result might lose. Boundary contact is allowed; nominal clearances are
 * checked separately against original source geometry. Prepared origin P/U indexes
 * survive all exact translations. No raster or vertex-only containment approval.
 */
export function domainContains(
  subject: DomainBoundary,
  usable: DomainBoundary,
  dx = 0,
  dy = 0,
  limit = REMNANT_DOMAIN_RULES.budgets.maxPredicateWork
): boolean {
  check(
    Number.isSafeInteger(dx) &&
      Number.isSafeInteger(dy) &&
      Math.abs(dx) <= 1_000_000_000 &&
      Math.abs(dy) <= 1_000_000_000,
    'translations must be bounded grid integers.'
  );
  const step = budget(limit);
  if (!subject.original.paths.length) return true;
  if (!usable.original.paths.length) return false;
  const pb = subject.original.bounds,
    ub = usable.original.bounds;
  if (pb.minX + dx < ub.minX || pb.maxX + dx > ub.maxX || pb.minY + dy < ub.minY || pb.maxY + dy > ub.maxY)
    return false;
  const shift = (p: Point) => ({ X: p.X + dx, Y: p.Y + dy });
  const moved = subject.original.paths.map(path => path.map(shift));
  const difference: Clipper.Paths = [];
  const clip = new Clipper.Clipper();
  clip.StrictlySimple = true;
  check(
    clip.AddPaths(moved, Clipper.PolyType.ptSubject, true) &&
      clip.AddPaths(usable.original.paths, Clipper.PolyType.ptClip, true),
    'invalid Boolean boundary.'
  );
  check(
    clip.Execute(
      Clipper.ClipType.ctDifference,
      difference,
      Clipper.PolyFillType.pftNonZero,
      Clipper.PolyFillType.pftNonZero
    ),
    'boundary difference failed.'
  );
  check(
    difference.reduce((n, path) => n + path.length, 0) <= REMNANT_DOMAIN_RULES.budgets.maxBooleanVertices,
    'boundary difference exceeded the vertex budget.'
  );
  if (filledArea(difference) > 0) return false;

  const pEdges = subject.edges.map(edge => ({
    a: shift(edge.a),
    b: shift(edge.b),
    minX: edge.minX + dx,
    maxX: edge.maxX + dx,
    minY: edge.minY + dy,
    maxY: edge.maxY + dy,
  }));
  const pSplits = pEdges.map(
    edge =>
      new Map([
        [key(edge.a), edge.a],
        [key(edge.b), edge.b],
      ])
  );
  const uSplits = usable.edges.map(
    edge =>
      new Map([
        [key(edge.a), edge.a],
        [key(edge.b), edge.b],
      ])
  );
  step(2 * (pEdges.length + usable.edges.length));
  for (let pi = 0; pi < pEdges.length; pi++) {
    const p = pEdges[pi];
    for (let ui = 0; ui < usable.edges.length; ui++) {
      step();
      const u = usable.edges[ui];
      if (!boundsMeet(p, u)) continue;
      const pua = domainTurn(p.a, p.b, u.a),
        pub = domainTurn(p.a, p.b, u.b);
      const upa = domainTurn(u.a, u.b, p.a),
        upb = domainTurn(u.a, u.b, p.b);
      if (pua * pub < 0 && upa * upb < 0) return false;
      if (pua === 0 && boundsHave(p, u.a)) {
        step();
        pSplits[pi].set(key(u.a), u.a);
      }
      if (pub === 0 && boundsHave(p, u.b)) {
        step();
        pSplits[pi].set(key(u.b), u.b);
      }
      if (upa === 0 && boundsHave(u, p.a)) {
        step();
        uSplits[ui].set(key(p.a), p.a);
      }
      if (upb === 0 && boundsHave(u, p.b)) {
        step();
        uSplits[ui].set(key(p.b), p.b);
      }
    }
  }
  const inspect = (edges: Edge[], splits: Map<string, Point>[], reverse: boolean) => {
    const target = reverse ? subject.doubled : usable.doubled;
    const x = reverse ? 2 * dx : 0,
      y = reverse ? 2 * dy : 0;
    const acceptable = (point: Point) => {
      const result = where({ X: point.X - x, Y: point.Y - y }, target, step);
      return reverse ? result !== 1 : result !== 0;
    };
    for (let index = 0; index < edges.length; index++) {
      const edge = edges[index];
      const axis = Math.abs(edge.a.X - edge.b.X) >= Math.abs(edge.a.Y - edge.b.Y) ? 'X' : 'Y';
      const points = Array.from(splits[index].values()).sort((a, b) => {
        step();
        return a[axis] - b[axis];
      });
      for (let i = 0; i < points.length; i++) {
        step();
        if (!acceptable({ X: 2 * points[i].X, Y: 2 * points[i].Y })) return false;
        if (i && !acceptable({ X: points[i - 1].X + points[i].X, Y: points[i - 1].Y + points[i].Y })) return false;
      }
    }
    return true;
  };
  return inspect(pEdges, pSplits, false) && inspect(usable.edges, uSplits, true);
}

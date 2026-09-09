import * as Clipper from 'clipper-lib';
import type { CompensatedPart } from './compensated-geometry';
import type { PreparedStockDomain } from './remnant-domain';
import { domainTurn } from './domain-containment';
import { convexPieces } from './convex-pieces';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

type Point = Clipper.IntPoint;
type Range = { minX: number; minY: number; maxX: number; maxY: number };
export type DomainOrigins = { boundary: Clipper.Paths; forbidden: Clipper.Paths; contacts: Point[] };
const count = (paths: Clipper.Paths) => paths.reduce((sum, path) => sum + path.length, 0);
const rectangle = (r: Range): Clipper.Path => [
  { X: r.minX, Y: r.minY },
  { X: r.maxX, Y: r.minY },
  { X: r.maxX, Y: r.maxY },
  { X: r.minX, Y: r.maxY },
];
const cross = (a: Point, b: Point) => a.X * b.Y - a.Y * b.X;

/** Bounded origin-space search only. Every returned candidate still needs the
 * unchanged exact P/U containment and independent original-curve distances. */
export function prepareDomainCandidates(domain: PreparedStockDomain) {
  const limits = REMNANT_DOMAIN_RULES.budgets;
  let work = 0,
    cachedVertices = 0;
  const cache = new Map<CompensatedPart, DomainOrigins>();
  const step = (amount = 1) => {
    work += amount;
    if (work > limits.maxCandidateWork) throw new Error('Recorded-piece candidate search exceeded its work budget.');
  };
  const checkCount = (paths: Clipper.Paths, maximum: number) => {
    if (count(paths) > maximum) throw new Error('Recorded-piece candidate search exceeded its vertex budget.');
  };
  const hull = (points: Point[]): Clipper.Path => {
    const sorted = points
      .sort((a, b) => {
        step();
        return a.X - b.X || a.Y - b.Y;
      })
      .filter((p, i, a) => !i || p.X !== a[i - 1].X || p.Y !== a[i - 1].Y);
    const half = (ps: Point[]) => {
      const out: Point[] = [];
      for (const p of ps) {
        step();
        while (out.length >= 2 && domainTurn(out[out.length - 2], out[out.length - 1], p) <= 0) {
          step();
          out.pop();
        }
        out.push(p);
      }
      return out.slice(0, -1);
    };
    return [...half(sorted), ...half([...sorted].reverse())];
  };
  const union = (paths: Clipper.Paths): Clipper.Paths => {
    if (paths.length <= 1) return paths;
    checkCount(paths, limits.maxCandidateUnionInputVertices);
    step(count(paths));
    const clip = new Clipper.Clipper(),
      output: Clipper.Paths = [];
    clip.StrictlySimple = true;
    if (
      !clip.AddPaths(paths, Clipper.PolyType.ptSubject, true) ||
      !clip.Execute(Clipper.ClipType.ctUnion, output, Clipper.PolyFillType.pftNonZero, Clipper.PolyFillType.pftNonZero)
    )
      throw new Error('Recorded-piece candidate union failed.');
    checkCount(output, limits.maxBooleanVertices);
    return output;
  };
  const convex =
    domain.usable.length === 1 &&
    Clipper.Clipper.Orientation(domain.usable[0]) &&
    domain.usable[0].every((a, i, p) => {
      step();
      return domainTurn(a, p[(i + 1) % p.length], p[(i + 2) % p.length]) >= 0;
    });
  const generate = (part: CompensatedPart, range: Range): DomainOrigins => {
    const cached = cache.get(part);
    if (cached) return cached;
    let result: DomainOrigins;
    const positive = part.envelope.paths.filter(Clipper.Clipper.Orientation);
    if (convex) {
      // A convex container is the intersection of its inward half planes. The
      // minimum support of every real P vertex shifts each plane to origin space.
      let region = rectangle(range);
      const boundary = domain.usable[0];
      for (let i = 0; i < boundary.length && region.length; i++) {
        const a = boundary[i],
          b = boundary[(i + 1) % boundary.length];
        const edge = { X: b.X - a.X, Y: b.Y - a.Y };
        let support = Infinity;
        for (const path of positive)
          for (const p of path) {
            step();
            support = Math.min(support, cross(edge, p));
          }
        const constant = cross(edge, a) - support;
        const next: Point[] = [];
        for (let j = 0; j < region.length; j++) {
          step();
          const p = region[j],
            q = region[(j + 1) % region.length];
          const dp = cross(edge, p) - constant,
            dq = cross(edge, q) - constant;
          if (dp >= 0) next.push(p);
          if ((dp < 0 && dq > 0) || (dp > 0 && dq < 0)) {
            const t = dp / (dp - dq);
            next.push({ X: p.X + t * (q.X - p.X), Y: p.Y + t * (q.Y - p.Y) });
          }
        }
        region = next;
      }
      // Clipper uses rounded grid intersections, while contacts keep the original
      // support intersections for neighboring-grid search, including zero-area fits.
      result = {
        boundary: region.length >= 3 ? [region.map(p => ({ X: Math.round(p.X), Y: Math.round(p.Y) }))] : [],
        forbidden: [],
        contacts: region,
      };
    } else {
      const pieces = positive.flatMap(path => {
        step(path.length);
        // Large generated rings use their convex hull as a conservative search
        // approximation; actual envelope holes/concavities survive final validation.
        return path.length <= 2000 ? convexPieces(path) : [hull([...path])];
      });
      let inputVertices = 0;
      let groups: Clipper.Paths[] = [];
      const contacts: Point[] = [];
      for (const edge of domain.prepared.edges)
        for (const piece of pieces) {
          step(2 * piece.length);
          // Sweep the true boundary segment by the reflected convex P piece. This
          // constructs a valid polygon; a segment is never treated as a closed solid.
          const band = hull(
            piece.flatMap(p => [
              { X: edge.a.X - p.X, Y: edge.a.Y - p.Y },
              { X: edge.b.X - p.X, Y: edge.b.Y - p.Y },
            ])
          );
          inputVertices += band.length;
          if (inputVertices > limits.maxCandidateUnionInputVertices)
            throw new Error('Recorded-piece boundary sweeps exceeded their input budget.');
          if (band.length >= 3) {
            groups.push([band]);
            contacts.push(...band);
          }
        }
      while (groups.length > 1) {
        const next: Clipper.Paths[] = [];
        for (let i = 0; i < groups.length; i += 4) next.push(union(groups.slice(i, i + 4).flat()));
        groups = next;
      }
      result = { boundary: [rectangle(range)], forbidden: groups[0] ?? [], contacts };
    }
    const vertices = count(result.boundary) + count(result.forbidden) + result.contacts.length;
    if (cache.size >= limits.maxCandidateCacheEntries || cachedVertices + vertices > limits.maxCandidateCachedVertices)
      throw new Error('Recorded-piece candidate cache exceeded its memory budget.');
    cachedVertices += vertices;
    cache.set(part, result);
    return result;
  };
  return { generate };
}

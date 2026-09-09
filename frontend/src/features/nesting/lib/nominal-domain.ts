import type { Loop, Point } from './nesting';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

const EPS = 1e-7;
const sq = (n: number) => n * n;
const turn = (a: Point, b: Point, c: Point) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
function pointDistance(p: Point, a: Point, b: Point): number {
  const length = sq(b.x - a.x) + sq(b.y - a.y);
  const t = length ? Math.max(0, Math.min(1, ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / length)) : 0;
  return Math.hypot(p.x - a.x - t * (b.x - a.x), p.y - a.y - t * (b.y - a.y));
}

/** Secondary distance check on unrounded source curves. The exact guarded P/U
 * predicate remains mandatory; this helper cannot approve a placement alone. */
export function nominalDomainContains(part: Loop, outer: Loop, holes: Loop[], clearance: number): boolean {
  if (!Number.isFinite(clearance) || clearance <= EPS)
    throw new Error('Nominal domain checks need positive protection.');
  let work = 0;
  const step = () => {
    if (++work > REMNANT_DOMAIN_RULES.budgets.maxPredicateWork)
      throw new Error('Original stock boundary distance exceeded its work budget.');
  };
  const inside = (p: Point, loop: Loop) => {
    if (loop.type === 'circle') return Math.hypot(p.x - loop.cx, p.y - loop.cy) < loop.r;
    let result = false;
    for (let i = 0; i < loop.points.length; i++) {
      step();
      const a = loop.points[i],
        b = loop.points[(i + 1) % loop.points.length];
      if (a.y > p.y !== b.y > p.y && p.x < a.x + ((p.y - a.y) * (b.x - a.x)) / (b.y - a.y)) result = !result;
    }
    return result;
  };
  const sample = (loop: Loop): Point => (loop.type === 'circle' ? { x: loop.cx + loop.r, y: loop.cy } : loop.points[0]);
  const separated = (a: Loop, b: Loop, minimum: number): boolean => {
    if (a.type === 'circle' && b.type === 'circle') {
      const d = Math.hypot(a.cx - b.cx, a.cy - b.cy);
      return Math.max(d - a.r - b.r, Math.abs(a.r - b.r) - d, 0) >= minimum - EPS;
    }
    if (a.type === 'circle' || b.type === 'circle') {
      const circle = a.type === 'circle' ? a : (b as Extract<Loop, { type: 'circle' }>);
      const poly = a.type === 'poly' ? a : (b as Extract<Loop, { type: 'poly' }>);
      for (let i = 0; i < poly.points.length; i++) {
        step();
        const p = poly.points[i],
          q = poly.points[(i + 1) % poly.points.length];
        const near = pointDistance({ x: circle.cx, y: circle.cy }, p, q);
        const far = Math.max(
          Math.hypot(p.x - circle.cx, p.y - circle.cy),
          Math.hypot(q.x - circle.cx, q.y - circle.cy)
        );
        const distance = Math.max(near - circle.r, circle.r - far, 0);
        if (distance < minimum - EPS) return false;
      }
      return true;
    }
    for (let i = 0; i < a.points.length; i++)
      for (let j = 0; j < b.points.length; j++) {
        step();
        const p = a.points[i],
          q = a.points[(i + 1) % a.points.length];
        const r = b.points[j],
          s = b.points[(j + 1) % b.points.length];
        if (
          Math.max(p.x, q.x) + minimum < Math.min(r.x, s.x) ||
          Math.max(r.x, s.x) + minimum < Math.min(p.x, q.x) ||
          Math.max(p.y, q.y) + minimum < Math.min(r.y, s.y) ||
          Math.max(r.y, s.y) + minimum < Math.min(p.y, q.y)
        )
          continue;
        if (turn(p, q, r) * turn(p, q, s) < 0 && turn(r, s, p) * turn(r, s, q) < 0) return false;
        if (
          Math.min(pointDistance(p, r, s), pointDistance(q, r, s), pointDistance(r, p, q), pointDistance(s, p, q)) <
          minimum - EPS
        )
          return false;
      }
    return true;
  };
  if (!inside(sample(part), outer) || !separated(part, outer, clearance)) return false;
  // Connected simple contours with separated boundaries cannot move from inside
  // to outside. Reciprocal samples reject a physical hole covered by the part.
  return holes.every(
    hole => separated(part, hole, clearance) && !inside(sample(part), hole) && !inside(sample(hole), part)
  );
}

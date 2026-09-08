import type * as Clipper from 'clipper-lib';

type Point = Clipper.IntPoint;
const MAX_VERTICES = 2000;
const MAX_COORDINATE = 200_000_000;
const MAX_OPERATIONS = 8_000_000;
const ZERO = BigInt(0);
const key = (point: Point) => `${point.X},${point.Y}`;
const same = (a: Point, b: Point) => a.X === b.X && a.Y === b.Y;

// Products of coordinate differences can exceed Number's exact-integer range.
// Most turns have a decisive floating sign; use exact arithmetic near cancellation.
function turn(a: Point, b: Point, c: Point): number {
  const left = (b.X - a.X) * (c.Y - a.Y);
  const right = (b.Y - a.Y) * (c.X - a.X);
  const determinant = left - right;
  if (Math.abs(determinant) > 8 * Number.EPSILON * (Math.abs(left) + Math.abs(right))) return determinant > 0 ? 1 : -1;
  const exact = BigInt(b.X - a.X) * BigInt(c.Y - a.Y) - BigInt(b.Y - a.Y) * BigInt(c.X - a.X);
  return exact > ZERO ? 1 : exact < ZERO ? -1 : 0;
}

function twiceArea(path: Clipper.Path): bigint {
  let area = ZERO;
  for (let i = 0; i < path.length; i++) {
    const a = path[i],
      b = path[(i + 1) % path.length];
    area += BigInt(a.X) * BigInt(b.Y) - BigInt(a.Y) * BigInt(b.X);
  }
  return area;
}

function between(a: Point, b: Point, point: Point): boolean {
  return (
    point.X >= Math.min(a.X, b.X) &&
    point.X <= Math.max(a.X, b.X) &&
    point.Y >= Math.min(a.Y, b.Y) &&
    point.Y <= Math.max(a.Y, b.Y)
  );
}

function clean(path: Clipper.Path): Clipper.Path {
  const points = path.filter((point, index) => !index || !same(point, path[index - 1]));
  if (points.length > 1 && same(points[0], points[points.length - 1])) points.pop();
  let changed = true;
  while (changed && points.length >= 3) {
    changed = false;
    for (let i = 0; i < points.length; i++) {
      const a = points[(i + points.length - 1) % points.length],
        b = points[i],
        c = points[(i + 1) % points.length];
      if (turn(a, b, c) === 0) {
        if (!between(a, c, b)) throw new Error('Polygon boundary backtracks along an edge.');
        points.splice(i, 1);
        changed = true;
        break;
      }
    }
  }
  return points;
}

function convex(path: Clipper.Path): boolean {
  let positive = false;
  for (let i = 0; i < path.length; i++) {
    const side = turn(path[i], path[(i + 1) % path.length], path[(i + 2) % path.length]);
    if (side < 0) return false;
    if (side > 0) positive = true;
  }
  return positive;
}

function segmentsMeet(a: Point, b: Point, c: Point, d: Point): boolean {
  if (
    Math.max(a.X, b.X) < Math.min(c.X, d.X) ||
    Math.max(c.X, d.X) < Math.min(a.X, b.X) ||
    Math.max(a.Y, b.Y) < Math.min(c.Y, d.Y) ||
    Math.max(c.Y, d.Y) < Math.min(a.Y, b.Y)
  )
    return false;
  const abC = turn(a, b, c),
    abD = turn(a, b, d),
    cdA = turn(c, d, a),
    cdB = turn(c, d, b);
  return (
    (abC * abD < 0 && cdA * cdB < 0) ||
    (abC === 0 && between(a, b, c)) ||
    (abD === 0 && between(a, b, d)) ||
    (cdA === 0 && between(c, d, a)) ||
    (cdB === 0 && between(c, d, b))
  );
}

function joined(a: Clipper.Path, edgeA: number, b: Clipper.Path, edgeB: number): Clipper.Path | null {
  const result: Clipper.Path = [];
  for (let i = 1; i <= a.length; i++) result.push(a[(edgeA + i) % a.length]);
  for (let i = 2; i < b.length; i++) result.push(b[(edgeB + i) % b.length]);
  // Keep collinear shared-boundary vertices until merging is complete so later
  // adjacent pieces can still find their exact reversed edge.
  if (new Set(result.map(key)).size !== result.length || !convex(result)) return null;
  return result;
}

/**
 * Partition a simple, positively oriented integer polygon into convex pieces.
 * No coordinates are invented or rounded. Degeneracy or a resource overrun
 * throws, allowing the caller to use its existing polygon-sum implementation.
 */
export function convexPieces(path: Clipper.Path): Clipper.Paths {
  if (!Array.isArray(path) || path.length < 3 || path.length > MAX_VERTICES)
    throw new Error('Convex decomposition requires 3–2,000 polygon vertices.');
  for (const point of path) {
    if (
      !point ||
      !Number.isSafeInteger(point.X) ||
      !Number.isSafeInteger(point.Y) ||
      Math.abs(point.X) > MAX_COORDINATE ||
      Math.abs(point.Y) > MAX_COORDINATE
    )
      throw new Error('Convex decomposition requires bounded integer coordinates.');
  }
  const points = clean(path);
  if (points.length < 3 || new Set(points.map(key)).size !== points.length)
    throw new Error('Polygon has degenerate or repeated boundary vertices.');
  const area = twiceArea(points);
  if (area <= ZERO) throw new Error('Polygon must have positive nonzero orientation.');
  let operations = 0;
  const step = () => {
    if (++operations > MAX_OPERATIONS) throw new Error('Convex decomposition exceeded its operation budget.');
  };
  for (let i = 0; i < points.length; i++) {
    for (let j = i + 2; j < points.length; j++) {
      if (i === 0 && j === points.length - 1) continue;
      step();
      if (segmentsMeet(points[i], points[(i + 1) % points.length], points[j], points[(j + 1) % points.length]))
        throw new Error('Polygon boundary intersects or touches itself.');
    }
  }
  if (convex(points)) return [new Set(path.map(key)).size === path.length ? path : points];

  const remaining = [...points];
  const pieces: Clipper.Paths = [];
  while (remaining.length > 3) {
    let found = false;
    for (let i = 0; i < remaining.length; i++) {
      step();
      const previous = (i + remaining.length - 1) % remaining.length;
      const next = (i + 1) % remaining.length;
      const a = remaining[previous],
        b = remaining[i],
        c = remaining[next];
      const side = turn(a, b, c);
      if (side === 0 && between(a, c, b)) {
        remaining.splice(i, 1);
        found = true;
        break;
      }
      if (side <= 0) continue;
      let occupied = false;
      for (let j = 0; j < remaining.length; j++) {
        if (j === previous || j === i || j === next) continue;
        step();
        const point = remaining[j];
        if (turn(a, b, point) >= 0 && turn(b, c, point) >= 0 && turn(c, a, point) >= 0) {
          occupied = true;
          break;
        }
      }
      if (occupied) continue;
      pieces.push([a, b, c]);
      remaining.splice(i, 1);
      found = true;
      break;
    }
    if (!found) throw new Error('Polygon could not be triangulated without degeneracy.');
  }
  if (remaining.length === 3 && turn(remaining[0], remaining[1], remaining[2]) > 0) pieces.push(remaining);

  let merged = true;
  while (merged) {
    merged = false;
    const edges = new Map<string, { piece: number; edge: number }>();
    for (let i = 0; i < pieces.length && !merged; i++) {
      for (let j = 0; j < pieces[i].length; j++) {
        step();
        const a = pieces[i][j],
          b = pieces[i][(j + 1) % pieces[i].length];
        const neighbor = edges.get(`${key(b)}>${key(a)}`);
        if (neighbor && neighbor.piece !== i) {
          const combined = joined(pieces[neighbor.piece], neighbor.edge, pieces[i], j);
          if (combined) {
            pieces[neighbor.piece] = combined;
            pieces.splice(i, 1);
            merged = true;
            break;
          }
        }
        edges.set(`${key(a)}>${key(b)}`, { piece: i, edge: j });
      }
    }
  }
  const result = pieces.map(clean);
  if (
    !result.length ||
    result.some(piece => !convex(piece)) ||
    result.reduce((sum, piece) => sum + twiceArea(piece), ZERO) !== area
  )
    throw new Error('Convex decomposition failed its exact area check.');
  return result;
}

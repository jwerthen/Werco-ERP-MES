import { guardedOuter, pointPath as guardedPointPath } from './guarded-geometry';
import { prepareExclusions, exclusionCollision, type PreparedExclusion } from './stock-exclusions';
import { allowedRotations, orientationExplanation } from './orientation';
import { compareStableText } from './stable-order';
import { packRectangles } from './rectangular-packing';
import { convexPieces } from './convex-pieces';
import * as Clipper from 'clipper-lib';
import { bounds, partArea, type Loop, type Part, type Placement, type Point, type Stock, type Nest } from './nesting';

const EPS = 1e-7;
const SCALE = 10000;
const integerPath = (points: Point[]): Clipper.Path =>
  points.map(p => ({ X: Math.round(p.x * SCALE), Y: Math.round(p.y * SCALE) }));
const pointPath = (path: Clipper.Path): Point[] => path.map(p => ({ x: p.X / SCALE, y: p.Y / SCALE }));
const positive = (path: Clipper.Path) => (Clipper.Clipper.Orientation(path) ? path : [...path].reverse());
const sq = (n: number) => n * n;
function distanceToSegment(p: Point, a: Point, b: Point) {
  const length = sq(b.x - a.x) + sq(b.y - a.y);
  const t = length ? Math.max(0, Math.min(1, ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / length)) : 0;
  return Math.hypot(p.x - a.x - t * (b.x - a.x), p.y - a.y - t * (b.y - a.y));
}
const cross = (a: Point, b: Point, c: Point) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
function pointInside(p: Point, points: Point[]) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const a = points[i],
      b = points[j];
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}
function segmentsCross(a: Point, b: Point, c: Point, d: Point) {
  return cross(a, b, c) * cross(a, b, d) < -EPS && cross(c, d, a) * cross(c, d, b) < -EPS;
}
type SegmentBox = { a: Point; b: Point; minX: number; maxX: number; minY: number; maxY: number };
const segmentCache = new WeakMap<Loop, SegmentBox[]>();
function segmentBoxes(loop: Extract<Loop, { type: 'poly' }>): SegmentBox[] {
  let edges = segmentCache.get(loop);
  if (!edges) {
    edges = loop.points.map((a, i) => {
      const b = loop.points[(i + 1) % loop.points.length];
      return {
        a,
        b,
        minX: Math.min(a.x, b.x),
        maxX: Math.max(a.x, b.x),
        minY: Math.min(a.y, b.y),
        maxY: Math.max(a.y, b.y),
      };
    });
    segmentCache.set(loop, edges);
  }
  return edges;
}
export function outlinesCollide(a: Loop, b: Loop, gap: number): boolean {
  const aa = bounds(a),
    bb = bounds(b);
  if (
    aa.x + aa.width + gap < bb.x - EPS ||
    bb.x + bb.width + gap < aa.x - EPS ||
    aa.y + aa.height + gap < bb.y - EPS ||
    bb.y + bb.height + gap < aa.y - EPS
  )
    return false;
  if (a.type === 'circle' && b.type === 'circle') return Math.hypot(a.cx - b.cx, a.cy - b.cy) < a.r + b.r + gap - EPS;
  if (a.type === 'circle' || b.type === 'circle') {
    const circle = a.type === 'circle' ? a : (b as Extract<Loop, { type: 'circle' }>);
    const poly = a.type === 'poly' ? a : (b as Extract<Loop, { type: 'poly' }>);
    const center = { x: circle.cx, y: circle.cy };
    return (
      pointInside(center, poly.points) ||
      poly.points.some(
        (p, i) => distanceToSegment(center, p, poly.points[(i + 1) % poly.points.length]) < circle.r + gap - EPS
      )
    );
  }
  const ae = segmentBoxes(a),
    be = segmentBoxes(b);
  for (const first of ae)
    for (const second of be) {
      if (
        first.maxX + gap < second.minX - EPS ||
        second.maxX + gap < first.minX - EPS ||
        first.maxY + gap < second.minY - EPS ||
        second.maxY + gap < first.minY - EPS
      )
        continue;
      const p = first.a,
        q = first.b,
        r = second.a,
        s = second.b;
      if (segmentsCross(p, q, r, s)) return true;
      if (
        gap > EPS &&
        Math.min(
          distanceToSegment(p, r, s),
          distanceToSegment(q, r, s),
          distanceToSegment(r, p, q),
          distanceToSegment(s, p, q)
        ) <
          gap - EPS
      )
        return true;
    }
  // With positive clearance, boundaries cannot touch. One sample per closed curve
  // distinguishes containment after crossings and boundary distance are ruled out.
  if (gap > EPS) return pointInside(a.points[0], b.points) || pointInside(b.points[0], a.points);
  // Mid-edge samples detect coincident/overlapping edges at a zero requested gap.
  const strictlyInside = (points: Point[], other: Point[]) =>
    points.some((p, i) => {
      const q = points[(i + 1) % points.length];
      return [p, { x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 }].some(
        s =>
          pointInside(s, other) && !other.some((t, j) => distanceToSegment(s, t, other[(j + 1) % other.length]) < EPS)
      );
    });
  if (strictlyInside(a.points, b.points) || strictlyInside(b.points, a.points)) return true;
  // Identical polygons have no strictly interior vertices or edge midpoints.
  const clip = new Clipper.Clipper();
  clip.AddPath(integerPath(a.points), Clipper.PolyType.ptSubject, true);
  clip.AddPath(integerPath(b.points), Clipper.PolyType.ptClip, true);
  const intersection: Clipper.Paths = [];
  clip.Execute(
    Clipper.ClipType.ctIntersection,
    intersection,
    Clipper.PolyFillType.pftNonZero,
    Clipper.PolyFillType.pftNonZero
  );
  return intersection.some(path => Math.abs(Clipper.Clipper.Area(path)) > 1);
}
export function rotatePoint(p: Point, rotation: Placement['rotation'], width: number, height: number): Point {
  if (rotation === 90) return { x: height - p.y, y: p.x };
  if (rotation === 180) return { x: width - p.x, y: height - p.y };
  if (rotation === 270) return { x: p.y, y: width - p.x };
  return p;
}
export function movedOuter(part: Part, placement: Placement): Loop {
  const outer = part.loops[0],
    box = bounds(outer);
  const move = (p: Point) => {
    const r = rotatePoint(p, placement.rotation, box.width, box.height);
    return { x: r.x + placement.x, y: r.y + placement.y };
  };
  if (outer.type === 'circle') {
    const c = move({ x: outer.cx, y: outer.cy });
    return { ...outer, cx: c.x, cy: c.y };
  }
  return { type: 'poly', points: outer.points.map(move) };
}
function simplifyOpen(points: Point[], tolerance: number): Point[] {
  if (points.length <= 2) return points;
  let max = 0,
    index = 0;
  for (let i = 1; i < points.length - 1; i++) {
    const d = distanceToSegment(points[i], points[0], points[points.length - 1]);
    if (d > max) {
      max = d;
      index = i;
    }
  }
  if (max <= tolerance) return [points[0], points[points.length - 1]];
  return [
    ...simplifyOpen(points.slice(0, index + 1), tolerance).slice(0, -1),
    ...simplifyOpen(points.slice(index), tolerance),
  ];
}
function reduced(points: Point[]) {
  if (points.length <= 80) return { points, tolerance: 0 };
  let far = 1;
  for (let i = 2; i < points.length; i++)
    if (
      Math.hypot(points[i].x - points[0].x, points[i].y - points[0].y) >
      Math.hypot(points[far].x - points[0].x, points[far].y - points[0].y)
    )
      far = i;
  let tolerance = 0.00635,
    result = points;
  for (let i = 0; i < 24; i++) {
    result = [
      ...simplifyOpen(points.slice(0, far + 1), tolerance).slice(0, -1),
      ...simplifyOpen([...points.slice(far), points[0]], tolerance).slice(0, -1),
    ];
    if (result.length <= 80) break;
    tolerance *= 2;
  }
  return { points: result, tolerance };
}
type Shape = {
  key: string;
  part: Part;
  rotation: Placement['rotation'];
  width: number;
  height: number;
  outer: Loop;
  path: Clipper.Path;
  tolerance: number;
  pieces?: Clipper.Paths | null;
};
function shape(part: Part, rotation: Placement['rotation']): Shape {
  const b = bounds(part.loops[0]);
  const placement: Placement = {
    partId: part.id,
    instance: 0,
    x: 0,
    y: 0,
    width: rotation % 180 ? b.height : b.width,
    height: rotation % 180 ? b.width : b.height,
    rotation,
    sheet: 0,
  };
  const outer = movedOuter(part, placement);
  const r = outer.type === 'poly' ? reduced(outer.points) : { points: [], tolerance: 0 };
  return {
    key:
      outer.type === 'circle'
        ? `circle:${outer.r}:${part.geometryToleranceMm ?? 0}`
        : (() => {
            const ps = outer.points.map(p => `${p.x.toFixed(6)},${p.y.toFixed(6)}`);
            const variants = [ps, [...ps].reverse()].map(points => {
              let i = 0;
              for (let j = 1; j < points.length; j++) if (points[j] < points[i]) i = j;
              return [...points.slice(i), ...points.slice(0, i)].join(';');
            });
            return variants.sort()[0] + `:${part.geometryToleranceMm ?? 0}`;
          })(),
    part,
    rotation,
    width: placement.width,
    height: placement.height,
    outer,
    path: positive(integerPath(r.points)),
    tolerance: r.tolerance,
  };
}
function offset(paths: Clipper.Paths, amount: number): Clipper.Paths {
  if (amount <= 0) return paths;
  const o = new Clipper.ClipperOffset(2, 0.001 * SCALE),
    out: Clipper.Paths = [];
  o.AddPaths(paths, Clipper.JoinType.jtRound, Clipper.EndType.etClosedPolygon);
  o.Execute(out, amount * SCALE);
  return out;
}
function pieces(shape: Shape): Clipper.Paths | null {
  if (shape.pieces === undefined) {
    try {
      shape.pieces = convexPieces(shape.path);
    } catch {
      // Degenerate integer contours retain the general Minkowski implementation.
      shape.pieces = null;
    }
  }
  return shape.pieces;
}
function unionPaths(paths: Clipper.Paths): Clipper.Paths {
  if (paths.length <= 1) return paths;
  const clip = new Clipper.Clipper(),
    result: Clipper.Paths = [];
  clip.AddPaths(paths, Clipper.PolyType.ptSubject, true);
  clip.Execute(Clipper.ClipType.ctUnion, result, Clipper.PolyFillType.pftNonZero, Clipper.PolyFillType.pftNonZero);
  return result;
}
/** Collapse overlapping pieces in bounded groups before the next union stage. */
function stagedUnion(groups: Clipper.Paths[]): Clipper.Paths {
  while (groups.length > 1) {
    const next: Clipper.Paths[] = [];
    for (let i = 0; i < groups.length; i += 4) next.push(unionPaths(groups.slice(i, i + 4).flat()));
    groups = next;
  }
  return groups[0] ?? [];
}
/** Linear edge merge avoids constructing n*m quadrilaterals for convex profiles. */
function convexSum(a: Clipper.Path, b: Clipper.Path): Clipper.Path {
  const start = (p: Clipper.Path) => {
    let k = 0;
    for (let i = 1; i < p.length; i++) if (p[i].Y < p[k].Y || (p[i].Y === p[k].Y && p[i].X < p[k].X)) k = i;
    return [...p.slice(k), ...p.slice(0, k)];
  };
  a = start(a);
  b = start(b);
  let i = 0,
    j = 0;
  const out: Clipper.Path = [];
  while (i < a.length || j < b.length) {
    const aa = a[i % a.length],
      bb = b[j % b.length];
    out.push({ X: aa.X + bb.X, Y: aa.Y + bb.Y });
    if (i === a.length) {
      j++;
      continue;
    }
    if (j === b.length) {
      i++;
      continue;
    }
    const an = a[(i + 1) % a.length],
      bn = b[(j + 1) % b.length];
    const product = (an.X - aa.X) * (bn.Y - bb.Y) - (an.Y - aa.Y) * (bn.X - bb.X);
    if (product >= 0) i++;
    if (product <= 0) j++;
  }
  return positive(out);
}
/** Configuration-space obstacle for the moving part's lower-left origin. */
function obstacle(fixed: Shape, moving: Shape, gap: number): Clipper.Paths {
  const allowance =
    gap +
    fixed.tolerance +
    moving.tolerance +
    (fixed.part.geometryToleranceMm ?? 0) +
    (moving.part.geometryToleranceMm ?? 0) +
    0.0003;
  if (fixed.outer.type === 'circle' && moving.outer.type === 'circle') {
    const r = fixed.outer.r + moving.outer.r + allowance,
      count = 128,
      radius = r / Math.cos(Math.PI / count);
    return [
      integerPath(
        Array.from({ length: count }, (_, i) => ({
          x:
            fixed.outer.type === 'circle'
              ? fixed.outer.cx -
                (moving.outer as Extract<Loop, { type: 'circle' }>).cx +
                radius * Math.cos((2 * Math.PI * i) / count)
              : 0,
          y:
            fixed.outer.type === 'circle'
              ? fixed.outer.cy -
                (moving.outer as Extract<Loop, { type: 'circle' }>).cy +
                radius * Math.sin((2 * Math.PI * i) / count)
              : 0,
        }))
      ),
    ];
  }
  if (moving.outer.type === 'circle') {
    const c = moving.outer;
    return offset([fixed.path], c.r + allowance).map(p =>
      p.map(v => ({ X: v.X - c.cx * SCALE, Y: v.Y - c.cy * SCALE }))
    );
  }
  if (fixed.outer.type === 'circle') {
    const c = fixed.outer;
    return offset([positive(moving.path.map(p => ({ X: -p.X, Y: -p.Y })))], c.r + allowance).map(p =>
      p.map(v => ({ X: v.X + c.cx * SCALE, Y: v.Y + c.cy * SCALE }))
    );
  }
  const pattern = positive(moving.path.map(p => ({ X: -p.X, Y: -p.Y })));
  const fixedPieces = pieces(fixed),
    movingPieces = pieces(moving);
  let sums: Clipper.Paths;
  if (fixedPieces && movingPieces) {
    const groups = movingPieces.map(piece => {
      const reflected = piece.map(p => ({ X: -p.X, Y: -p.Y }));
      return stagedUnion(fixedPieces.map(other => [convexSum(reflected, other)]));
    });
    sums = stagedUnion(groups);
  } else sums = Clipper.Clipper.MinkowskiSum(pattern, fixed.path, true);
  // The exterior obstacle reserves the solid outer profile, including all holes.
  sums.sort((a, b) => Math.abs(Clipper.Clipper.Area(b)) - Math.abs(Clipper.Clipper.Area(a)));
  return sums.length ? offset([positive(sums[0])], allowance) : [];
}
type Placed = { placement: Placement; shape: Shape; outer: Loop };
function candidates(
  moving: Shape,
  placed: Placed[],
  stock: Stock,
  cache: Map<string, Clipper.Paths>,
  exclusions: PreparedExclusion[]
): Point[] {
  const margin = stock.margin + (moving.part.geometryToleranceMm ?? 0);
  const minX = margin,
    minY = margin,
    maxX = stock.width - margin - moving.width,
    maxY = stock.height - margin - moving.height;
  if (maxX < minX - EPS || maxY < minY - EPS) return [];
  const corners = [
    { x: minX, y: minY },
    { x: maxX, y: minY },
    { x: minX, y: maxY },
    { x: maxX, y: maxY },
  ];
  const forbidden: Clipper.Paths = [];
  if (exclusions.length) {
    const key = `exclusions|${moving.key}`;
    let paths = cache.get(key);
    if (!paths) {
      const envelope = guardedOuter(moving.outer, stock.gap / 2 + (moving.part.geometryToleranceMm ?? 0));
      const guardedShape = (path: Clipper.Path, key: string): Shape => {
        const points = guardedPointPath(path),
          r = reduced(points);
        return {
          ...moving,
          key,
          outer: { type: 'poly', points },
          path: positive(integerPath(r.points)),
          tolerance: r.tolerance,
          pieces: undefined,
          part: { ...moving.part, geometryToleranceMm: 0 },
        };
      };
      // Offset holes may be filled in this candidate approximation only. Final
      // envelope checks and the leftover ledger retain the exact winding.
      const movingPaths = envelope.filter(Clipper.Clipper.Orientation);
      const groups: Clipper.Paths[] = [];
      for (const region of exclusions)
        for (const fixed of region.paths.filter(Clipper.Clipper.Orientation))
          for (const movingPath of movingPaths)
            groups.push(obstacle(guardedShape(fixed, region.source.id), guardedShape(movingPath, moving.key), 0));
      paths = stagedUnion(groups);
      cache.set(key, paths);
    }
    forbidden.push(...paths);
  }
  for (const item of placed) {
    const key = `${item.shape.key}|${moving.key}`;
    let paths = cache.get(key);
    if (!paths) {
      paths = obstacle(item.shape, moving, stock.gap);
      cache.set(key, paths);
    }
    forbidden.push(
      ...paths.map(p =>
        p.map(v => ({ X: v.X + Math.round(item.placement.x * SCALE), Y: v.Y + Math.round(item.placement.y * SCALE) }))
      )
    );
    // Exact circle tangencies also cover perfectly tight sheet widths.
    if (moving.outer.type === 'circle' && item.outer.type === 'circle') {
      const a = item.outer,
        b = moving.outer,
        r = a.r + b.r + stock.gap + (item.shape.part.geometryToleranceMm ?? 0) + (moving.part.geometryToleranceMm ?? 0);
      for (const y of [minY, maxY]) {
        const dy = y + b.cy - a.cy;
        if (Math.abs(dy) <= r) {
          const dx = Math.sqrt(Math.max(0, r * r - dy * dy));
          corners.push({ x: a.cx - b.cx - dx, y }, { x: a.cx - b.cx + dx, y });
        }
      }
      for (const x of [minX, maxX]) {
        const dx = x + b.cx - a.cx;
        if (Math.abs(dx) <= r) {
          const dy = Math.sqrt(Math.max(0, r * r - dx * dx));
          corners.push({ x, y: a.cy - b.cy - dy }, { x, y: a.cy - b.cy + dy });
        }
      }
    }
  }
  let hasFreeRegion = false;
  if (forbidden.length && maxX > minX && maxY > minY) {
    const c = new Clipper.Clipper(),
      free: Clipper.Paths = [];
    c.AddPath(
      integerPath([
        { x: minX, y: minY },
        { x: maxX, y: minY },
        { x: maxX, y: maxY },
        { x: minX, y: maxY },
      ]),
      Clipper.PolyType.ptSubject,
      true
    );
    c.AddPaths(forbidden, Clipper.PolyType.ptClip, true);
    c.Execute(Clipper.ClipType.ctDifference, free, Clipper.PolyFillType.pftNonZero, Clipper.PolyFillType.pftNonZero);
    corners.push(...free.flatMap(pointPath));
    hasFreeRegion = free.length > 0;
  }
  // Free-region vertices already describe the usable boundaries. Only scan raw
  // obstacles when a tight, zero-area origin region can disappear in clipping.
  if (!hasFreeRegion)
    for (const path of forbidden)
      for (let i = 0; i < path.length; i++) {
        const a = { x: path[i].X / SCALE, y: path[i].Y / SCALE },
          b = { x: path[(i + 1) % path.length].X / SCALE, y: path[(i + 1) % path.length].Y / SCALE };
        if (a.x >= minX - EPS && a.x <= maxX + EPS && a.y >= minY - EPS && a.y <= maxY + EPS) corners.push(a);
        for (const x of [minX, maxX])
          if (Math.abs(b.x - a.x) > EPS) {
            const t = (x - a.x) / (b.x - a.x);
            if (t >= 0 && t <= 1) corners.push({ x, y: a.y + t * (b.y - a.y) });
          }
        for (const y of [minY, maxY])
          if (Math.abs(b.y - a.y) > EPS) {
            const t = (y - a.y) / (b.y - a.y);
            if (t >= 0 && t <= 1) corners.push({ x: a.x + t * (b.x - a.x), y });
          }
      }
  const unique = new Map<string, Point>();
  corners.forEach(p => {
    if (p.x >= minX - EPS && p.y >= minY - EPS && p.x <= maxX + EPS && p.y <= maxY + EPS)
      unique.set(`${p.x.toFixed(5)},${p.y.toFixed(5)}`, p);
  });
  return Array.from(unique.values()).sort((a, b) => a.y - b.y || a.x - b.x);
}
export function packContours(parts: Part[], stock: Stock): Nest {
  const allRectangles = parts.every(p => {
    const l = p.loops[0],
      b = bounds(l);
    return (
      l.type === 'poly' &&
      l.points.length === 4 &&
      l.points.every(
        point =>
          (Math.abs(point.x - b.x) < EPS || Math.abs(point.x - b.x - b.width) < EPS) &&
          (Math.abs(point.y - b.y) < EPS || Math.abs(point.y - b.y - b.height) < EPS)
      )
    );
  });
  if (allRectangles && !stock.exclusions?.length) {
    const tolerance = Math.max(0, ...parts.map(p => p.geometryToleranceMm ?? 0));
    return packRectangles(parts, { ...stock, gap: stock.gap + 2 * tolerance, margin: stock.margin + tolerance });
  }
  const exclusions = prepareExclusions(stock);
  // Scope memoized decisions to this validated run and exact source/pose. The
  // same physical pose recurs across sheets, instances and deterministic passes.
  const exclusionDecisions = new Map<string, boolean>();
  const shapes = new Map<string, Shape[]>();
  parts.forEach(p => {
    const seen = new Set<string>();
    shapes.set(
      p.id,
      allowedRotations(p, stock)
        .map(r => shape(p, r))
        .filter(s => {
          if (seen.has(s.key)) return false;
          seen.add(s.key);
          return true;
        })
    );
  });
  const cache = new Map<string, Clipper.Paths>();
  const instances = parts.flatMap(p => Array.from({ length: p.quantity }, (_, instance) => ({ part: p, instance })));
  const seenOrders = new Set<string>();
  const passes = [0, 1]
    .map<Nest | null>(mode => {
      const sheets: Placed[][] = [],
        unplaced: Nest['unplaced'] = [];
      const ordered = [...instances].sort((a, b) => {
        const aa = bounds(a.part.loops[0]),
          bb = bounds(b.part.loops[0]);
        return (
          (mode === 0
            ? bb.width * bb.height - aa.width * aa.height
            : Math.max(bb.width, bb.height) - Math.max(aa.width, aa.height)) ||
          partArea(b.part) - partArea(a.part) ||
          compareStableText(a.part.id, b.part.id) ||
          a.instance - b.instance
        );
      });
      const orderKey = ordered.map(p => `${p.part.id}:${p.instance}`).join('|');
      if (seenOrders.has(orderKey)) return null;
      seenOrders.add(orderKey);
      for (const { part, instance } of ordered) {
        let best: Placed | undefined;
        for (let si = 0; si <= sheets.length && si < stock.maxSheets; si++) {
          const placed = sheets[si] ?? [];
          for (const moving of shapes.get(part.id)!) {
            for (const point of candidates(moving, placed, stock, cache, exclusions)) {
              const placement: Placement = {
                partId: part.id,
                instance,
                ...point,
                width: moving.width,
                height: moving.height,
                rotation: moving.rotation,
                sheet: si,
              };
              const outer = movedOuter(part, placement);
              if (exclusions.length) {
                const key = JSON.stringify([part.id, moving.rotation, point.x, point.y]);
                let blocked = exclusionDecisions.get(key);
                if (blocked === undefined) {
                  blocked = Boolean(exclusionCollision(outer, stock.gap, part.geometryToleranceMm ?? 0, exclusions));
                  if (exclusionDecisions.size < 50000) exclusionDecisions.set(key, blocked);
                }
                if (blocked) continue;
              }
              if (
                placed.some(other =>
                  outlinesCollide(
                    outer,
                    other.outer,
                    stock.gap + (part.geometryToleranceMm ?? 0) + (other.shape.part.geometryToleranceMm ?? 0)
                  )
                )
              )
                continue;
              const score = point.y + moving.height,
                old = best ? best.placement.y + best.placement.height : Infinity;
              if (!best || score < old - EPS || (Math.abs(score - old) < EPS && point.x < best.placement.x))
                best = { placement, shape: moving, outer };
              break;
            }
          }
          if (best) {
            if (!sheets[si]) sheets.push([]);
            sheets[si].push(best);
            break;
          }
          // If this part cannot fit an empty sheet, do not allocate that sheet.
          if (si === sheets.length) break;
        }
        if (!best) {
          const u = unplaced.find(p => p.partId === part.id);
          if (u) u.count++;
          else
            unplaced.push({
              partId: part.id,
              count: 1,
              reason:
                orientationExplanation(part, stock) ??
                (exclusions.length
                  ? 'The search found no valid contour placement within stock exclusions, margins and spacing'
                  : 'No permitted contour placement within sheet margins and spacing'),
            });
        }
      }
      const placements = sheets.flatMap(s => s.map(p => p.placement));
      const area = placements.reduce((sum, p) => sum + partArea(parts.find(part => part.id === p.partId)!), 0);
      return {
        placements,
        unplaced,
        sheets: sheets.length,
        area,
        utilization: sheets.length ? (100 * area) / (stock.width * stock.height * sheets.length) : 0,
        method: 'True contour nesting · best of two deterministic orders · permitted rotations and grain',
      };
    })
    .filter((pass): pass is Nest => pass !== null);
  passes.sort(
    (a, b) =>
      b.placements.length - a.placements.length ||
      a.sheets - b.sheets ||
      Math.max(0, ...a.placements.filter(p => p.sheet === a.sheets - 1).map(p => p.y + p.height)) -
        Math.max(0, ...b.placements.filter(p => p.sheet === b.sheets - 1).map(p => p.y + p.height))
  );
  return passes[0];
}

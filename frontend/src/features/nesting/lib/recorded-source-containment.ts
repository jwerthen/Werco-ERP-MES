import type { InchPoint, ObservedLoop, StockPieceEvidence } from '../../../types/stockPiece';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

type Point = { x: bigint; y: bigint };
type Rational = { n: bigint; d: bigint };
type QueryPoint = Point & { d: bigint };
type Polygon = { kind: 'polygon'; points: Point[] };
type Circle = { kind: 'circle'; center: Point; r: bigint };
type Shape = Polygon | Circle;
const ZERO = BigInt(0),
  ONE = BigInt(1),
  TWO = BigInt(2),
  NANO = BigInt(1_000_000_000);

function check(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(`Recorded-piece geometry: ${message}`);
}
function nano(value: string, positive = false): bigint {
  check(
    typeof value === 'string' &&
      /^-?(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,9})?$/.test(value) &&
      value !== '-0' &&
      !(value.includes('.') && value.endsWith('0')),
    'source measurements must be canonical inch strings.'
  );
  const negative = value.startsWith('-');
  const [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.');
  const magnitude = BigInt(whole) * NANO + BigInt(fraction.padEnd(9, '0'));
  check(magnitude <= BigInt(100_000) * NANO, 'source measurement exceeds its coordinate limit.');
  const result = negative ? -magnitude : magnitude;
  check(!positive || result > ZERO, 'source dimensions must be positive.');
  return result;
}
const point = (p: InchPoint): Point => ({ x: nano(p.x), y: nano(p.y) });
const sub = (a: Point, b: Point): Point => ({ x: a.x - b.x, y: a.y - b.y });
const cross = (a: Point, b: Point) => a.x * b.y - a.y * b.x;
const dot = (a: Point, b: Point) => a.x * b.x + a.y * b.y;
const equal = (a: Point, b: Point) => a.x === b.x && a.y === b.y;
const fraction = (n: bigint, d: bigint): Rational => (d < ZERO ? { n: -n, d: -d } : { n, d });
const compare = (a: Rational, b: Rational) => {
  const difference = a.n * b.d - b.n * a.d;
  return difference < ZERO ? -1 : difference > ZERO ? 1 : 0;
};
const between = (p: bigint, a: bigint, b: bigint) => (a <= b ? a <= p && p <= b : b <= p && p <= a);

/** Every boundary/point predicate shares the same bounded call-local work allowance. */
class Work {
  private used = 0;
  spend() {
    check(
      ++this.used <= REMNANT_DOMAIN_RULES.budgets.maxPredicateWork,
      'exact source containment exceeds its work limit.'
    );
  }
}

/** -1 outside, 0 boundary, 1 inside. The query uses exact common-denominator coordinates. */
function location(p: QueryPoint, ring: Point[], work: Work): -1 | 0 | 1 {
  let inside = false;
  for (let i = 0; i < ring.length; i++) {
    work.spend();
    const a = ring[i],
      b = ring[(i + 1) % ring.length];
    const side = cross(sub(b, a), { x: p.x - a.x * p.d, y: p.y - a.y * p.d });
    if (side === ZERO && between(p.x, a.x * p.d, b.x * p.d) && between(p.y, a.y * p.d, b.y * p.d)) return 0;
    if (a.y * p.d > p.y !== b.y * p.d > p.y && (b.y > a.y ? side > ZERO : side < ZERO)) inside = !inside;
  }
  return inside ? 1 : -1;
}

/** Parameters on AB at every boundary intersection, including collinear endpoints. */
function cuts(a: Point, b: Point, c: Point, d: Point, work: Work): Rational[] {
  work.spend();
  if (
    (a.x < c.x && a.x < d.x && b.x < c.x && b.x < d.x) ||
    (a.x > c.x && a.x > d.x && b.x > c.x && b.x > d.x) ||
    (a.y < c.y && a.y < d.y && b.y < c.y && b.y < d.y) ||
    (a.y > c.y && a.y > d.y && b.y > c.y && b.y > d.y)
  )
    return [];
  const r = sub(b, a),
    s = sub(d, c),
    offset = sub(c, a);
  const denominator = cross(r, s);
  if (denominator !== ZERO) {
    const t = fraction(cross(offset, s), denominator),
      u = fraction(cross(offset, r), denominator);
    return t.n >= ZERO && t.n <= t.d && u.n >= ZERO && u.n <= u.d ? [t] : [];
  }
  if (cross(offset, r) !== ZERO) return [];
  const axis = r.x !== ZERO ? 'x' : 'y';
  const ends = [c, d].map(p => fraction(p[axis] - a[axis], r[axis])).sort(compare);
  if (ends[1].n < ZERO || ends[0].n > ends[0].d) return [];
  return ends.map(t => (t.n < ZERO ? { n: ZERO, d: ONE } : t.n > t.d ? { n: ONE, d: ONE } : t));
}

function polygon(raw: InchPoint[], work: Work): Polygon {
  check(Array.isArray(raw) && raw.length >= 3 && raw.length <= 2000, 'invalid source polygon vertex count.');
  const points = raw.map(point);
  check(new Set(points.map(p => `${p.x},${p.y}`)).size === points.length, 'source polygon repeats a vertex.');
  let area = ZERO;
  for (let i = 0; i < points.length; i++) {
    const a = points[i],
      b = points[(i + 1) % points.length],
      previous = points[(i + points.length - 1) % points.length];
    check(!equal(a, b), 'source polygon has a zero-length edge.');
    check(
      cross(sub(previous, a), sub(b, a)) !== ZERO || dot(sub(previous, a), sub(b, a)) <= ZERO,
      'source polygon retraces an edge.'
    );
    area += cross(a, b);
    for (let j = i + 2; j < points.length; j++) {
      if (i === 0 && j === points.length - 1) continue;
      check(
        cuts(a, b, points[j], points[(j + 1) % points.length], work).length === 0,
        'source polygon intersects itself.'
      );
    }
  }
  check(area !== ZERO, 'source polygon has no area.');
  return { kind: 'polygon', points };
}

function shape(loop: ObservedLoop, work: Work): Shape {
  check(loop && (loop.kind === 'circle' || loop.kind === 'polygon'), 'unsupported source outline.');
  return loop.kind === 'circle'
    ? { kind: 'circle', center: { x: nano(loop.cx), y: nano(loop.cy) }, r: nano(loop.r, true) }
    : polygon(loop.pts, work);
}

/** One strictly interior point of a simple polygon from its lowest open horizontal strip. */
function interiorPoint(ring: Point[], work: Work): QueryPoint {
  const ys = Array.from(new Set(ring.map(p => p.y))).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const y = ys[0] + ys[1];
  const xs: Rational[] = [];
  for (let i = 0; i < ring.length; i++) {
    work.spend();
    const a = ring[i],
      b = ring[(i + 1) % ring.length];
    if (TWO * a.y > y !== TWO * b.y > y)
      xs.push(fraction(TWO * a.x * (b.y - a.y) + (b.x - a.x) * (y - TWO * a.y), TWO * (b.y - a.y)));
  }
  xs.sort(compare);
  check(xs.length >= 2, 'source hole has no interior.');
  const x = { n: xs[0].n * xs[1].d + xs[1].n * xs[0].d, d: TWO * xs[0].d * xs[1].d };
  return { x: TWO * x.n, y: y * x.d, d: TWO * x.d };
}

function inMaterial(p: QueryPoint, outer: Polygon, holes: Polygon[], work: Work): boolean {
  return location(p, outer.points, work) >= 0 && holes.every(hole => location(p, hole.points, work) <= 0);
}

function boundariesDisjoint(a: Polygon, b: Polygon, work: Work): boolean {
  for (let i = 0; i < a.points.length; i++)
    for (let j = 0; j < b.points.length; j++)
      if (
        cuts(a.points[i], a.points[(i + 1) % a.points.length], b.points[j], b.points[(j + 1) % b.points.length], work)
          .length
      )
        return false;
  return true;
}

/** Simple connected rings with disjoint boundaries cannot cross between interior and exterior. */
function assertPhysicalHoles(outer: Polygon, holes: Polygon[], work: Work): void {
  for (let i = 0; i < holes.length; i++) {
    const hole = holes[i];
    check(
      location({ ...hole.points[0], d: ONE }, outer.points, work) === 1 && boundariesDisjoint(hole, outer, work),
      'physical holes must lie strictly inside the original outer boundary without touching or crossing it.'
    );
    for (let j = 0; j < i; j++) {
      const prior = holes[j];
      check(
        boundariesDisjoint(hole, prior, work) &&
          location({ ...hole.points[0], d: ONE }, prior.points, work) === -1 &&
          location({ ...prior.points[0], d: ONE }, hole.points, work) === -1,
        'physical holes must not touch, overlap or contain one another in the original measurements.'
      );
    }
  }
}

function polygonContained(zone: Polygon, outer: Polygon, holes: Polygon[], work: Work): boolean {
  const boundaries = [outer, ...holes];
  for (let i = 0; i < zone.points.length; i++) {
    const a = zone.points[i],
      b = zone.points[(i + 1) % zone.points.length];
    if (!inMaterial({ ...a, d: ONE }, outer, holes, work)) return false;
    const parameters: Rational[] = [
      { n: ZERO, d: ONE },
      { n: ONE, d: ONE },
    ];
    for (const boundary of boundaries)
      for (let j = 0; j < boundary.points.length; j++)
        parameters.push(...cuts(a, b, boundary.points[j], boundary.points[(j + 1) % boundary.points.length], work));
    parameters.sort(compare);
    for (let j = 1; j < parameters.length; j++) {
      const left = parameters[j - 1],
        right = parameters[j];
      if (compare(left, right) === 0) continue;
      const t = { n: left.n * right.d + right.n * left.d, d: TWO * left.d * right.d };
      if (
        !inMaterial({ x: a.x * t.d + (b.x - a.x) * t.n, y: a.y * t.d + (b.y - a.y) * t.n, d: t.d }, outer, holes, work)
      )
        return false;
    }
  }
  // Boundary tests alone miss a zone that surrounds an entire physical hole.
  return holes.every(hole => location(interiorPoint(hole.points, work), zone.points, work) <= 0);
}

function segmentAtLeastRadius(circle: Circle, a: Point, b: Point, work: Work): boolean {
  work.spend();
  const edge = sub(b, a),
    center = sub(circle.center, a),
    projection = dot(center, edge),
    length = dot(edge, edge);
  const r2 = circle.r * circle.r;
  if (projection <= ZERO) return dot(center, center) >= r2;
  if (projection >= length) {
    const end = sub(circle.center, b);
    return dot(end, end) >= r2;
  }
  const numerator = cross(edge, center);
  return numerator * numerator >= r2 * length;
}

function contained(zone: Shape, outer: Shape, holes: Polygon[], work: Work): boolean {
  if (outer.kind === 'circle') {
    if (zone.kind === 'circle') {
      work.spend();
      const distance = sub(zone.center, outer.center),
        difference = outer.r - zone.r;
      return difference >= ZERO && dot(distance, distance) <= difference * difference;
    }
    return zone.points.every(p => {
      work.spend();
      const distance = sub(p, outer.center);
      return dot(distance, distance) <= outer.r * outer.r;
    });
  }
  if (zone.kind === 'polygon') return polygonContained(zone, outer, holes, work);
  if (!inMaterial({ ...zone.center, d: ONE }, outer, holes, work)) return false;
  return [outer, ...holes].every(boundary =>
    boundary.points.every((a, i) =>
      segmentAtLeastRadius(zone, a, boundary.points[(i + 1) % boundary.points.length], work)
    )
  );
}

/**
 * Check the original recorded measurements BEFORE translating, converting units or rounding.
 * Closed-boundary contact is allowed; no polygonization or tolerance changes this source claim.
 * Physical holes must be strictly interior and mutually disjoint. The kernel separately
 * validates grid representability and the compensated usable geometry.
 */
export function assertReportedZonesContained(evidence: StockPieceEvidence): void {
  check(evidence?.version === 1 && evidence.unit === 'in', 'unsupported reported source units.');
  const source = evidence.geometry,
    zones = evidence.unavailable_zones;
  check(source && source.kind !== 'unknown', 'record a known source shape before planning.');
  check(
    Array.isArray(zones) && zones.length <= REMNANT_DOMAIN_RULES.budgets.maxZones,
    'too many reported unavailable zones.'
  );
  const sourceCount =
    source.kind === 'polygon'
      ? source.outer.length + source.holes.reduce((n, ring) => n + ring.length, 0)
      : source.kind === 'rectangle'
        ? 4
        : 1;
  check(
    source.kind !== 'polygon' || source.holes.length <= REMNANT_DOMAIN_RULES.budgets.maxHoles,
    'too many physical holes.'
  );
  check(
    sourceCount + zones.reduce((n, zone) => n + (zone.outline.kind === 'circle' ? 1 : zone.outline.pts.length), 0) <=
      REMNANT_DOMAIN_RULES.budgets.maxSourceVertices,
    'reported source geometry exceeds its vertex limit.'
  );
  const work = new Work();
  let outer: Shape,
    holes: Polygon[] = [];
  if (source.kind === 'rectangle') {
    const width = nano(source.width, true),
      height = nano(source.height, true);
    outer = {
      kind: 'polygon',
      points: [
        { x: ZERO, y: ZERO },
        { x: width, y: ZERO },
        { x: width, y: height },
        { x: ZERO, y: height },
      ],
    };
  } else if (source.kind === 'circle') outer = shape(source, work);
  else {
    outer = polygon(source.outer, work);
    holes = source.holes.map(ring => polygon(ring, work));
    assertPhysicalHoles(outer, holes, work);
  }
  for (const zone of zones)
    check(
      contained(shape(zone.outline, work), outer, holes, work),
      'reported unavailable zone must lie wholly in the original recorded material; review the observation.'
    );
}

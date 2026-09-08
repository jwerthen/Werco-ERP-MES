import type { Loop, Point } from './nesting';

// These are physical tolerances, independent of the drawing's INSUNITS.
// Curves retain their exact axial extrema so sheet-fit bounds do not shrink.
export const DXF_JOIN_TOLERANCE_MM = 0.0001 * 25.4;
export const DXF_CURVE_TOLERANCE_MM = 0.0001 * 25.4;
const MAX_RECORDS = 20000;
const MAX_VERTICES = 20000;
const MAX_LOOP_VERTICES = 2000;
const NUMERIC_EPS = 1e-7;
const TURN = Math.PI * 2;
type Pair = { code: number; value: string };
type Entity = { type: string; data: Pair[] };
type Vertex = Point & { bulge: number };
type Path = { points: Point[] };
class TopologyError extends Error {
  constructor(message: string) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = 'TopologyError';
  }
}
function topologyCheck(ok: unknown, message: string): asserts ok {
  if (!ok) throw new TopologyError(message);
}
export type DXFGeometry = { loops: Loop[]; referencePaths: Point[][]; warnings: string[]; geometryToleranceMm: number };

function check(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
function number(text: string) {
  check(text.trim() !== '' && Number.isFinite(Number(text)), 'Invalid or blank DXF number.');
  return Number(text);
}
function value(entity: Entity, code: number, fallback = 0) {
  const values = entity.data.filter(pair => pair.code === code);
  check(values.length <= 1, `Duplicate DXF ${entity.type} field ${code}.`);
  return values.length ? number(values[0].value) : fallback;
}
function required(entity: Entity, code: number) {
  check(
    entity.data.some(pair => pair.code === code),
    `${entity.type} is missing required coordinates.`
  );
  return value(entity, code);
}
function physical(n: number, scale: number) {
  const result = n * scale;
  check(Number.isFinite(result) && Math.abs(result) <= 1e9, 'DXF coordinates exceed the supported numeric range.');
  return result;
}
function point(entity: Entity, x: number, y: number, scale: number): Point {
  return { x: physical(required(entity, x), scale), y: physical(required(entity, y), scale) };
}
function separation(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}
function validateFlat(entity: Entity, scale: number) {
  check(
    value(entity, 67) === 0 &&
      entity.data.filter(pair => pair.code === 410).every(pair => pair.value.toLowerCase() === 'model'),
    'Export model-space geometry only. Paper-space entities are not supported.'
  );
  check(
    Math.abs(value(entity, 210)) < NUMERIC_EPS &&
      Math.abs(value(entity, 220)) < NUMERIC_EPS &&
      Math.abs(Math.abs(value(entity, 230, 1)) - 1) < NUMERIC_EPS &&
      entity.data.filter(pair => pair.code === 39).every(pair => Math.abs(number(pair.value) * scale) < NUMERIC_EPS),
    'Only flat XY geometry with +Z or -Z extrusion is supported.'
  );
}
function validateWidths(entity: Entity) {
  check(
    entity.data.filter(pair => [40, 41, 43].includes(pair.code)).every(pair => number(pair.value) === 0),
    'Wide polylines are not supported.'
  );
}
function flags(entity: Entity, allowed: number) {
  const result = value(entity, 70);
  check(
    Number.isInteger(result) && result >= 0 && result <= allowed && (result & ~allowed) === 0,
    `Unsupported ${entity.type} flags. Only ordinary 2D polylines are supported.`
  );
  return result;
}
function normalizeAngle(angle: number) {
  return ((angle % TURN) + TURN) % TURN;
}

/** Sample in separate intervals at every cardinal angle: regular sampling alone loses true bounds. */
function arcPoints(center: Point, radius: number, start: number, sweep: number, endpoints?: [Point, Point]): Point[] {
  check(
    Number.isFinite(radius) &&
      radius > 0 &&
      Number.isFinite(start) &&
      Number.isFinite(sweep) &&
      Math.abs(sweep) > 1e-12,
    'Invalid or zero-length circular arc.'
  );
  const direction = Math.sign(sweep);
  const span = Math.abs(sweep);
  const breaks = [0, span];
  for (let cardinal = 0; cardinal < 4; cardinal++) {
    const offset = normalizeAngle(direction * ((cardinal * Math.PI) / 2 - start));
    if (offset > 1e-12 && offset < span - 1e-12) breaks.push(offset);
  }
  breaks.sort((a, b) => a - b);
  // asin form avoids acos(1 - tiny value) rounding to zero for large radii.
  const maxStep = Math.min(Math.PI / 2, 4 * Math.asin(Math.sqrt(Math.min(1, DXF_CURVE_TOLERANCE_MM / (2 * radius)))));
  check(Number.isFinite(maxStep) && maxStep > 0, 'Arc exceeds the supported numeric range.');
  const result: Point[] = [];
  for (let interval = 1; interval < breaks.length; interval++) {
    const steps = Math.max(1, Math.ceil((breaks[interval] - breaks[interval - 1]) / maxStep));
    check(
      result.length + steps + 1 <= MAX_LOOP_VERTICES + 1,
      'Curved contour exceeds the 2,000-vertex limit at 0.0001-inch curve tolerance.'
    );
    for (let step = interval === 1 ? 0 : 1; step <= steps; step++) {
      const angle =
        start + direction * (breaks[interval - 1] + ((breaks[interval] - breaks[interval - 1]) * step) / steps);
      result.push({ x: center.x + radius * Math.cos(angle), y: center.y + radius * Math.sin(angle) });
    }
  }
  if (endpoints) {
    result[0] = endpoints[0];
    result[result.length - 1] = endpoints[1];
  }
  check(
    result.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)),
    'Invalid circular arc coordinates.'
  );
  const distinct: Point[] = [];
  for (const point of result)
    if (!distinct.length || separation(point, distinct[distinct.length - 1]) > NUMERIC_EPS) distinct.push(point);
  return distinct;
}
function bulgePoints(a: Vertex, b: Vertex): Point[] {
  if (a.bulge === 0) return [a, b];
  const chord = separation(a, b);
  check(chord > NUMERIC_EPS, 'A bulged segment has coincident endpoints.');
  const offset = (chord * (1 / a.bulge - a.bulge)) / 4;
  const center = {
    x: (a.x + b.x) / 2 - ((b.y - a.y) * offset) / chord,
    y: (a.y + b.y) / 2 + ((b.x - a.x) * offset) / chord,
  };
  const radius = (chord * (Math.abs(a.bulge) + 1 / Math.abs(a.bulge))) / 4;
  return arcPoints(center, radius, Math.atan2(a.y - center.y, a.x - center.x), 4 * Math.atan(a.bulge), [a, b]);
}
function polyline(vertices: Vertex[], closed: boolean): Point[] {
  check(vertices.length >= 2 && vertices.length <= MAX_LOOP_VERTICES + 1, 'Polylines need 2–2,000 vertices.');
  const result: Point[] = [vertices[0]];
  const count = vertices.length - (closed ? 0 : 1);
  for (let i = 0; i < count; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    // Some CAD exports repeat the first vertex even when the closed flag is set.
    if (closed && i === vertices.length - 1 && separation(a, b) <= NUMERIC_EPS && a.bulge === 0) continue;
    check(separation(a, b) > NUMERIC_EPS, 'Duplicate adjacent polyline vertices.');
    result.push(...bulgePoints(a, b).slice(1));
    check(result.length <= MAX_LOOP_VERTICES + 1, 'Contour exceeds the 2,000-vertex limit after converting curves.');
  }
  return result;
}

type Spline = { controls: (Point & { z: number })[]; knots: number[]; weights: number[]; degree: number };
function readSpline(entity: Entity, scale: number): Spline {
  const splineFlags = value(entity, 70);
  const degree = required(entity, 71);
  check(Number.isInteger(splineFlags) && splineFlags >= 0 && splineFlags <= 31, 'Unsupported SPLINE flags.');
  check(Number.isInteger(degree) && degree >= 1 && degree <= 10, 'SPLINE degree must be 1–10.');
  const controls: (Point & { z: number })[] = [];
  const zSeen = new Set<number>();
  for (const pair of entity.data) {
    if (pair.code === 10) controls.push({ x: physical(number(pair.value), scale), y: NaN, z: 0 });
    else if ([20, 30].includes(pair.code)) {
      check(controls.length > 0, 'SPLINE control-point data without X coordinate.');
      const control = controls[controls.length - 1];
      if (pair.code === 20) {
        check(Number.isNaN(control.y), 'Duplicate SPLINE control-point Y coordinate.');
        control.y = physical(number(pair.value), scale);
      } else {
        check(!zSeen.has(controls.length - 1), 'Duplicate SPLINE control-point Z coordinate.');
        zSeen.add(controls.length - 1);
        control.z = physical(number(pair.value), scale);
      }
    }
  }
  check(
    Number.isInteger(required(entity, 73)) &&
      controls.length === value(entity, 73) &&
      controls.length >= degree + 1 &&
      controls.length <= MAX_LOOP_VERTICES,
    'Invalid SPLINE control-point count (limit 2,000).'
  );
  check(
    controls.every(p => Number.isFinite(p.y)),
    'SPLINE is missing a control-point Y coordinate.'
  );
  const knots = entity.data.filter(pair => pair.code === 40).map(pair => number(pair.value));
  check(
    Number.isInteger(required(entity, 72)) &&
      knots.length === value(entity, 72) &&
      knots.length === controls.length + degree + 1,
    'Invalid SPLINE knot count.'
  );
  check(
    knots.every((knot, i) => i === 0 || knot >= knots[i - 1]) && knots[degree] < knots[controls.length],
    'Invalid SPLINE knot domain or order.'
  );
  let multiplicity = 1;
  for (let i = 1; i < knots.length; i++) {
    multiplicity = knots[i] === knots[i - 1] ? multiplicity + 1 : 1;
    check(multiplicity <= degree + 1, 'Invalid SPLINE knot multiplicity.');
  }
  const weights = entity.data.filter(pair => pair.code === 41).map(pair => number(pair.value));
  check(
    (weights.length === 0 || weights.length === controls.length) && weights.every(weight => weight > 0),
    'SPLINE needs positive weights for every control point, or omitted unit weights.'
  );
  const fitCount = value(entity, 74);
  const fits: (Point & { z: number })[] = [];
  const fitZSeen = new Set<number>();
  for (const pair of entity.data) {
    if (pair.code === 11) fits.push({ x: physical(number(pair.value), scale), y: NaN, z: 0 });
    else if ([21, 31].includes(pair.code)) {
      check(fits.length > 0, 'SPLINE fit-point data without X coordinate.');
      const fit = fits[fits.length - 1];
      if (pair.code === 21) {
        check(Number.isNaN(fit.y), 'Duplicate SPLINE fit-point Y coordinate.');
        fit.y = physical(number(pair.value), scale);
      } else {
        check(!fitZSeen.has(fits.length - 1), 'Duplicate SPLINE fit-point Z coordinate.');
        fitZSeen.add(fits.length - 1);
        fit.z = physical(number(pair.value), scale);
      }
    }
  }
  check(
    Number.isInteger(fitCount) && fitCount >= 0 && fitCount === fits.length && fitCount <= MAX_LOOP_VERTICES,
    'Invalid SPLINE fit-point count.'
  );
  check(
    fits.every(p => Number.isFinite(p.y)),
    'SPLINE is missing a fit-point Y coordinate.'
  );
  check(
    fits.every(p => Math.abs(p.z - controls[0].z) <= NUMERIC_EPS),
    'SPLINE fit points must share the control-point XY plane.'
  );
  return { controls, knots, weights: weights.length ? weights : controls.map(() => 1), degree };
}

type Homogeneous = { x: number; y: number; w: number };
const blend = (a: Homogeneous, b: Homogeneous, t: number): Homogeneous => ({
  x: a.x + (b.x - a.x) * t,
  y: a.y + (b.y - a.y) * t,
  w: a.w + (b.w - a.w) * t,
});
const project = (p: Homogeneous): Point => ({ x: p.x / p.w, y: p.y / p.w });
function segmentDistance(p: Point, a: Point, b: Point) {
  const dx = b.x - a.x,
    dy = b.y - a.y,
    length2 = dx * dx + dy * dy;
  const t = length2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / length2));
  return Math.hypot(p.x - a.x - t * dx, p.y - a.y - t * dy);
}

/** Knot insertion makes rational Bezier spans; their positive control hull bounds subdivision error. */
function splinePoints(spline: Spline): Point[] {
  const { degree } = spline;
  const knots = [...spline.knots];
  let controls: Homogeneous[] = spline.controls.map((p, i) => ({
    x: p.x * spline.weights[i],
    y: p.y * spline.weights[i],
    w: spline.weights[i],
  }));
  check(
    controls.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)),
    'SPLINE weighted coordinates exceed the supported numeric range.'
  );
  check(
    knots.slice(0, degree + 1).every(k => k === knots[degree]) &&
      knots.slice(-degree - 1).every(k => k === knots[controls.length]),
    'This SPLINE needs clamped end knots for contour import. Re-export it as clamped splines or arcs.'
  );
  const internal = Array.from(new Set(knots.slice(degree + 1, -degree - 1)));
  for (const knot of internal) {
    let multiplicity = knots.filter(k => k === knot).length;
    check(multiplicity <= degree, 'Discontinuous SPLINE spans need separate cut contours.');
    while (multiplicity < degree) {
      const span = knots.findIndex((k, i) => k <= knot && knots[i + 1] > knot);
      const next: Homogeneous[] = [];
      for (let i = 0; i <= span - degree; i++) next[i] = controls[i];
      for (let i = span - multiplicity; i < controls.length; i++) next[i + 1] = controls[i];
      for (let i = span - degree + 1; i <= span - multiplicity; i++) {
        const alpha = (knot - knots[i]) / (knots[i + degree] - knots[i]);
        next[i] = blend(controls[i - 1], controls[i], alpha);
      }
      controls = next;
      knots.splice(span + 1, 0, knot);
      multiplicity++;
      check(controls.length <= MAX_VERTICES, 'SPLINE subdivision exceeds the geometry limit.');
    }
  }
  const result: Point[] = [project(controls[0])];
  const flatten = (span: Homogeneous[], depth: number) => {
    const points = span.map(project),
      a = points[0],
      b = points[points.length - 1];
    if (points.every(p => segmentDistance(p, a, b) <= DXF_CURVE_TOLERANCE_MM)) {
      if (separation(result[result.length - 1], b) > NUMERIC_EPS) result.push(b);
      check(
        result.length <= MAX_LOOP_VERTICES + 1,
        'SPLINE exceeds the 2,000-vertex limit at 0.0001-inch curve tolerance.'
      );
      return;
    }
    check(depth < 24, 'SPLINE cannot be resolved within the 0.0001-inch curve tolerance.');
    const left = [span[0]],
      right = [span[span.length - 1]];
    let row = span;
    while (row.length > 1) {
      row = row.slice(1).map((p, i) => blend(row[i], p, 0.5));
      left.push(row[0]);
      right.push(row[row.length - 1]);
    }
    flatten(left, depth + 1);
    flatten(right.reverse(), depth + 1);
  };
  for (let i = 0; i + degree < controls.length; i += degree) flatten(controls.slice(i, i + degree + 1), 0);
  check(result.length >= 2, 'SPLINE has no resolvable curve.');
  return result;
}

/** Keep closed cycles and separate graph bridges as unresolved reference paths. Never close a gap. */
function stitch(input: Path[], warnings: string[]): { loops: Loop[]; referencePaths: Point[][] } {
  const paths: Path[] = [],
    signatures = new Set<string>();
  let duplicates = 0;
  for (const path of input) {
    const coords = path.points.map(p => `${Math.round(p.x / NUMERIC_EPS)},${Math.round(p.y / NUMERIC_EPS)}`);
    const forward = coords.join(';'),
      reverse = [...coords].reverse().join(';');
    const signature = forward < reverse ? forward : reverse;
    if (signatures.has(signature)) {
      duplicates++;
      continue;
    }
    signatures.add(signature);
    paths.push(path);
  }
  if (duplicates)
    warnings.push(
      `Removed ${duplicates} geometrically duplicate path${duplicates === 1 ? '' : 's'} without changing the outline.`
    );
  const nodes: Point[] = [],
    buckets = new Map<string, number[]>();
  const nodeFor = (point: Point) => {
    const x = Math.floor(point.x / DXF_JOIN_TOLERANCE_MM),
      y = Math.floor(point.y / DXF_JOIN_TOLERANCE_MM);
    const nearby: number[] = [];
    for (let dx = -1; dx <= 1; dx++)
      for (let dy = -1; dy <= 1; dy++) {
        for (const node of buckets.get(`${x + dx},${y + dy}`) ?? [])
          if (separation(point, nodes[node]) <= DXF_JOIN_TOLERANCE_MM) nearby.push(node);
      }
    topologyCheck(
      nearby.length <= 1,
      'Several distinct DXF junctions are within 0.0001 inch. Review the touching cut paths.'
    );
    if (nearby.length) return nearby[0];
    const id = nodes.length;
    nodes.push(point);
    const key = `${x},${y}`,
      list = buckets.get(key) ?? [];
    check(list.length < 16, 'DXF endpoint density exceeds the geometry limit.');
    list.push(id);
    buckets.set(key, list);
    return id;
  };
  const endpoints = paths.map(path => [nodeFor(path.points[0]), nodeFor(path.points[path.points.length - 1])]);
  const adjacency: { to: number; edge: number }[][] = nodes.map(() => []);
  endpoints.forEach(([a, b], edge) => {
    adjacency[a].push({ to: b, edge });
    adjacency[b].push({ to: a, edge });
  });
  const discovered = nodes.map(() => -1),
    low = [...discovered],
    bridges = new Set<number>();
  let clock = 0;
  for (let first = 0; first < nodes.length; first++) {
    if (discovered[first] >= 0) continue;
    discovered[first] = low[first] = clock++;
    const stack = [{ node: first, parent: -1, parentEdge: -1, next: 0 }];
    while (stack.length) {
      const frame = stack[stack.length - 1];
      if (frame.next === adjacency[frame.node].length) {
        stack.pop();
        if (frame.parent >= 0) {
          low[frame.parent] = Math.min(low[frame.parent], low[frame.node]);
          if (low[frame.node] > discovered[frame.parent]) bridges.add(frame.parentEdge);
        }
        continue;
      }
      const next = adjacency[frame.node][frame.next++];
      if (next.edge === frame.parentEdge) continue;
      if (discovered[next.to] >= 0) low[frame.node] = Math.min(low[frame.node], discovered[next.to]);
      else {
        discovered[next.to] = low[next.to] = clock++;
        stack.push({ node: next.to, parent: frame.node, parentEdge: next.edge, next: 0 });
      }
    }
  }
  const cutting = adjacency.map(edges => edges.filter(({ edge }) => !bridges.has(edge)));
  topologyCheck(
    cutting.every(edges => edges.length === 0 || edges.length === 2),
    'Cut paths form ambiguous overlapping or branching closed contours. Separate the intended outlines before importing.'
  );
  const used = new Set<number>(),
    loops: Loop[] = [];
  for (let first = 0; first < paths.length; first++) {
    if (bridges.has(first) || used.has(first)) continue;
    const start = endpoints[first][0];
    let node = start,
      edge = first;
    const points: Point[] = [];
    do {
      check(!used.has(edge), 'DXF cycle traversal failed.');
      used.add(edge);
      const [a, b] = endpoints[edge];
      const segment = a === node ? paths[edge].points : [...paths[edge].points].reverse();
      points.push(
        ...segment.slice(points.length && separation(points[points.length - 1], segment[0]) <= NUMERIC_EPS ? 1 : 0)
      );
      check(points.length <= MAX_LOOP_VERTICES + 1, 'Contour exceeds the 2,000-vertex limit.');
      node = a === node ? b : a;
      if (node !== start) edge = cutting[node].find(next => next.edge !== edge)!.edge;
    } while (node !== start);
    if (separation(points[0], points[points.length - 1]) <= NUMERIC_EPS) points.pop();
    loops.push({ type: 'poly', points });
  }
  return { loops, referencePaths: Array.from(bridges).map(edge => paths[edge].points) };
}

export function readDXFGeometry(text: string, unitless: 'mm' | 'in'): DXFGeometry {
  check(text.length < 5_000_000, 'DXF limit: 5 MB.');
  const raw = text
    .replace(/^\uFEFF/, '')
    .replace(/\r/g, '')
    .trimEnd()
    .split('\n');
  check(raw.length % 2 === 0, 'Malformed DXF group pairs.');
  const pairs: Pair[] = [];
  for (let i = 0; i < raw.length; i += 2) {
    check(/^\s*-?\d+\s*$/.test(raw[i]), 'Invalid DXF group code.');
    pairs.push({ code: Number(raw[i]), value: raw[i + 1].trim() });
  }
  check(
    pairs.some(pair => pair.code === 0 && pair.value === 'EOF'),
    'Missing DXF end marker.'
  );
  let unit = 0;
  const ui = pairs.findIndex(pair => pair.code === 9 && pair.value === '$INSUNITS');
  if (ui >= 0) {
    check(pairs[ui + 1]?.code === 70 && pairs[ui + 1]?.value !== '', 'Invalid DXF unit declaration.');
    unit = number(pairs[ui + 1].value);
  }
  check([0, 1, 4].includes(unit), 'Only mm and inch DXF units are supported.');
  const scale = unit === 1 || (unit === 0 && unitless === 'in') ? 25.4 : 1;
  const entities: Entity[] = [];
  let inEntities = false;
  for (let i = 0; i < pairs.length; i++) {
    const p = pairs[i];
    if (p.code === 0 && p.value === 'SECTION') {
      inEntities = pairs[i + 1]?.code === 2 && pairs[i + 1]?.value === 'ENTITIES';
      i++;
    } else if (p.code === 0 && p.value === 'ENDSEC') inEntities = false;
    else if (inEntities && p.code === 0) {
      check(entities.length < MAX_RECORDS, 'DXF entity limit: 20,000 records.');
      entities.push({ type: p.value, data: [] });
    } else if (inEntities) {
      check(entities.length > 0, 'Malformed entity section.');
      entities[entities.length - 1].data.push(p);
    }
  }
  check(entities.length > 0, 'DXF contains no model-space cut geometry.');
  const loops: Loop[] = [];
  const paths: Path[] = [];
  const warnings: string[] = [];
  let annotationCount = 0;
  let hasSplines = false;
  let hasCurves = false;
  let verticesUsed = 0;
  let planeMin = Infinity;
  let planeMax = -Infinity;
  const onPlane = (heights: number[]) => {
    check(
      Math.max(...heights) - Math.min(...heights) <= NUMERIC_EPS,
      'Sloped or nonplanar DXF geometry is not supported.'
    );
    for (const height of heights) {
      planeMin = Math.min(planeMin, height);
      planeMax = Math.max(planeMax, height);
    }
  };
  const addPath = (points: Point[], closed: boolean) => {
    verticesUsed += points.length;
    check(verticesUsed <= MAX_VERTICES, 'File geometry limit: 20,000 vertices.');
    if (closed) {
      if (separation(points[0], points[points.length - 1]) <= NUMERIC_EPS) points.pop();
      loops.push({ type: 'poly', points });
    } else paths.push({ points });
  };
  for (let i = 0; i < entities.length; i++) {
    const entity = entities[i];
    // VIEWPORT defines a paper-layout view, not a cut contour. Other entities remain fail-closed.
    if (entity.type === 'VIEWPORT') continue;
    // FORMAT is an explicitly named drawing-annotation layer in these CAD exports.
    // Text on arbitrary layers might be engraving, so it remains unsupported.
    if (entity.data.some(pair => pair.code === 8 && pair.value.toUpperCase() === 'FORMAT')) {
      annotationCount++;
      continue;
    }
    check(
      ['LINE', 'ARC', 'CIRCLE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE'].includes(entity.type),
      `This importer does not support ${entity.type} yet. Export that geometry as 2D lines, circular arcs, circles, or polylines; the original DXF may still be valid for your laser.`
    );
    validateFlat(entity, scale);
    // DXF's arbitrary-axis algorithm maps flat -Z OCS to WCS by reflecting X.
    // LINE endpoints are already WCS and must not receive this transform.
    const ocsSign = value(entity, 230, 1) < 0 ? -1 : 1;
    const fromOCS = (points: Point[]) => points.map(p => ({ x: p.x * ocsSign, y: p.y }));
    if (entity.type === 'LINE') {
      onPlane([physical(value(entity, 30), scale), physical(value(entity, 31), scale)]);
      const points = [point(entity, 10, 20, scale), point(entity, 11, 21, scale)];
      check(separation(points[0], points[1]) > NUMERIC_EPS, 'Zero-length DXF line.');
      addPath(points, false);
    } else if (entity.type === 'CIRCLE' || entity.type === 'ARC') {
      onPlane([physical(value(entity, 30), scale) * ocsSign]);
      const center = point(entity, 10, 20, scale);
      const radius = physical(required(entity, 40), scale);
      check(radius > 0, 'Circle or arc radius must be positive.');
      if (entity.type === 'CIRCLE') {
        loops.push({ type: 'circle', cx: center.x * ocsSign, cy: center.y, r: radius });
        verticesUsed++;
        check(verticesUsed <= MAX_VERTICES, 'File geometry limit: 20,000 vertices.');
      } else {
        hasCurves = true;
        const start = normalizeAngle((required(entity, 50) * Math.PI) / 180);
        const end = normalizeAngle((required(entity, 51) * Math.PI) / 180);
        addPath(fromOCS(arcPoints(center, radius, start, normalizeAngle(end - start))), false);
      }
    } else if (entity.type === 'SPLINE') {
      const spline = readSpline(entity, scale);
      hasSplines = true;
      hasCurves = true;
      onPlane(spline.controls.map(p => p.z));
      addPath(splinePoints(spline), false);
    } else {
      onPlane([physical(value(entity, entity.type === 'LWPOLYLINE' ? 38 : 30), scale) * ocsSign]);
      const closed = (flags(entity, 129) & 1) === 1;
      validateWidths(entity);
      const vertices: Vertex[] = [];
      if (entity.type === 'LWPOLYLINE') {
        let bulgeSeen = false;
        for (const pair of entity.data) {
          if (pair.code === 10) {
            vertices.push({ x: physical(number(pair.value), scale), y: NaN, bulge: 0 });
            bulgeSeen = false;
          } else if ([20, 42].includes(pair.code)) {
            check(vertices.length > 0, 'Polyline vertex data without X coordinate.');
            const vertex = vertices[vertices.length - 1];
            if (pair.code === 20) {
              check(Number.isNaN(vertex.y), 'Duplicate vertex Y coordinate.');
              vertex.y = physical(number(pair.value), scale);
            } else {
              check(!bulgeSeen, 'Duplicate polyline vertex bulge.');
              vertex.bulge = number(pair.value);
              bulgeSeen = true;
            }
          }
        }
        check(
          Number.isInteger(required(entity, 90)) && vertices.length === value(entity, 90),
          'DXF vertex count mismatch.'
        );
        check(
          vertices.every(vertex => Number.isFinite(vertex.y)),
          'Polyline is missing a Y coordinate.'
        );
      } else {
        while (entities[i + 1]?.type === 'VERTEX') {
          const vertex = entities[++i];
          validateFlat(vertex, scale);
          validateWidths(vertex);
          check(
            Math.abs(value(vertex, 30) * scale) < NUMERIC_EPS,
            'Legacy 2D vertices must use the polyline elevation.'
          );
          flags(vertex, 0);
          vertices.push({ ...point(vertex, 10, 20, scale), bulge: value(vertex, 42) });
        }
        check(entities[i + 1]?.type === 'SEQEND', 'Legacy POLYLINE is missing SEQEND.');
        validateFlat(entities[++i], scale);
      }
      if (vertices.some(vertex => vertex.bulge !== 0)) hasCurves = true;
      addPath(fromOCS(polyline(vertices, closed)), closed);
    }
  }
  if (annotationCount) warnings.push(`Ignored ${annotationCount} drawing-annotation entities on the FORMAT layer.`);
  if (planeMax - planeMin > NUMERIC_EPS)
    warnings.push('Parallel XY geometry was aligned in Z without changing its X/Y outline.');
  if (hasSplines) warnings.push('Spline curves were resolved to their actual profile within 0.0001 inch.');
  const stitched = stitch(paths, warnings);
  loops.push(...stitched.loops);
  const loopSignatures = new Set<string>();
  let duplicateLoops = 0;
  const uniqueLoops = loops.filter(loop => {
    let signature: string;
    if (loop.type === 'circle')
      signature = `C:${Math.round(loop.cx / NUMERIC_EPS)},${Math.round(loop.cy / NUMERIC_EPS)},${Math.round(loop.r / NUMERIC_EPS)}`;
    else {
      const coordinates = loop.points.map(p => `${Math.round(p.x / NUMERIC_EPS)},${Math.round(p.y / NUMERIC_EPS)}`);
      const canonical = (items: string[]) => {
        let smallest = 0;
        for (let i = 1; i < items.length; i++) if (items[i] < items[smallest]) smallest = i;
        return [...items.slice(smallest), ...items.slice(0, smallest)].join(';');
      };
      const forward = canonical(coordinates),
        reverse = canonical([...coordinates].reverse());
      signature = `P:${forward < reverse ? forward : reverse}`;
    }
    if (loopSignatures.has(signature)) {
      duplicateLoops++;
      return false;
    }
    loopSignatures.add(signature);
    return true;
  });
  if (duplicateLoops)
    warnings.push(
      `Removed ${duplicateLoops} geometrically duplicate closed contour${duplicateLoops === 1 ? '' : 's'} without changing the outline.`
    );

  check(
    loops.length > 0,
    'No closed cutting outline was found. Close the intended outer contour or separate the reference geometry.'
  );
  check(loops.length <= 300, 'DXF contour limit: 300 closed contours.');
  return {
    loops: uniqueLoops,
    referencePaths: stitched.referencePaths,
    warnings,
    geometryToleranceMm: hasCurves ? DXF_CURVE_TOLERANCE_MM : 0,
  };
}

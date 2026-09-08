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
export type DXFGeometry = { loops: Loop[]; footprint: Loop; warnings: string[]; footprintOnly: boolean };

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
  return result;
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

/** Positive-weight B-splines stay inside their control hull; use that hull's bounds for quoting. */
function splineControls(entity: Entity, scale: number): (Point & { z: number })[] {
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
    'SPLINE bounds require one positive weight per control point, or omitted unit weights.'
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
  return controls;
}

/** Join only unique endpoint pairs. Spatial buckets bound work even for large line-only exports. */
function stitch(paths: Path[]): Loop[] {
  const endpoints = paths.flatMap(path => [path.points[0], path.points[path.points.length - 1]]);
  const buckets = new Map<string, number[]>();
  const key = (x: number, y: number) => `${x},${y}`;
  const bucket = (p: Point) => [Math.floor(p.x / DXF_JOIN_TOLERANCE_MM), Math.floor(p.y / DXF_JOIN_TOLERANCE_MM)];
  for (let i = 0; i < endpoints.length; i++) {
    const [x, y] = bucket(endpoints[i]);
    const list = buckets.get(key(x, y)) ?? [];
    // More than four points in a tolerance-sized square cannot all form unambiguous pairs.
    topologyCheck(
      list.length < 4,
      'Ambiguous DXF junction: several endpoints meet. Remove duplicate or branching cut lines.'
    );
    list.push(i);
    buckets.set(key(x, y), list);
  }
  const partners = endpoints.map((p, i) => {
    const [x, y] = bucket(p);
    let partner = -1;
    for (let dx = -1; dx <= 1; dx++)
      for (let dy = -1; dy <= 1; dy++) {
        for (const candidate of buckets.get(key(x + dx, y + dy)) ?? []) {
          if (candidate === i || separation(p, endpoints[candidate]) > DXF_JOIN_TOLERANCE_MM) continue;
          topologyCheck(
            partner < 0,
            'Ambiguous DXF junction: several endpoints meet. Remove duplicate or branching cut lines.'
          );
          partner = candidate;
        }
      }
    topologyCheck(
      partner >= 0,
      'Open DXF contour: endpoints do not join within 0.0001 inch. Check for gaps or construction lines.'
    );
    return partner;
  });
  const used = new Set<number>();
  const loops: Loop[] = [];
  for (let index = 0; index < paths.length; index++) {
    if (used.has(index)) continue;
    const points = [...paths[index].points];
    used.add(index);
    let end = index * 2 + 1;
    while (partners[end] !== index * 2) {
      const nextEndpoint = partners[end];
      const nextIndex = Math.floor(nextEndpoint / 2);
      topologyCheck(!used.has(nextIndex), 'Ambiguous closed DXF chain.');
      used.add(nextIndex);
      const next = nextEndpoint % 2 === 0 ? paths[nextIndex].points : [...paths[nextIndex].points].reverse();
      // Keep both ends of a small join gap instead of snapping/shrinking the drawing.
      points.push(...next.slice(separation(points[points.length - 1], next[0]) <= NUMERIC_EPS ? 1 : 0));
      check(points.length <= MAX_LOOP_VERTICES + 1, 'Contour exceeds the 2,000-vertex limit.');
      end = nextEndpoint ^ 1;
    }
    if (separation(points[0], points[points.length - 1]) <= NUMERIC_EPS) points.pop();
    loops.push({ type: 'poly', points });
  }
  return loops;
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
  const extentPoints: Point[] = [];
  const warnings: string[] = [];
  let footprintOnly = false;
  let annotationCount = 0;
  let hasSplines = false;
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
    if (planeMax - planeMin > NUMERIC_EPS) footprintOnly = true;
  };
  const addPath = (points: Point[], closed: boolean) => {
    extentPoints.push(...points);
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
    if (
      ['TEXT', 'LEADER'].includes(entity.type) &&
      entity.data.some(pair => pair.code === 8 && pair.value.toUpperCase() === 'FORMAT')
    ) {
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
        extentPoints.push(
          { x: center.x * ocsSign - radius, y: center.y - radius },
          { x: center.x * ocsSign + radius, y: center.y + radius }
        );
        loops.push({ type: 'circle', cx: center.x * ocsSign, cy: center.y, r: radius });
        verticesUsed++;
        check(verticesUsed <= MAX_VERTICES, 'File geometry limit: 20,000 vertices.');
      } else {
        const start = normalizeAngle((required(entity, 50) * Math.PI) / 180);
        const end = normalizeAngle((required(entity, 51) * Math.PI) / 180);
        addPath(fromOCS(arcPoints(center, radius, start, normalizeAngle(end - start))), false);
      }
    } else if (entity.type === 'SPLINE') {
      const controls = splineControls(entity, scale);
      hasSplines = true;
      onPlane(controls.map(p => p.z));
      extentPoints.push(...controls);
      verticesUsed += controls.length;
      check(verticesUsed <= MAX_VERTICES, 'File geometry limit: 20,000 vertices.');
      footprintOnly = true;
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
      addPath(fromOCS(polyline(vertices, closed)), closed);
    }
  }
  check(extentPoints.length > 0, 'DXF contains no model-space cut geometry.');
  check(loops.length <= 300, 'DXF contour limit: 300 closed contours.');
  if (annotationCount)
    warnings.push(
      `Ignored ${annotationCount} text/leader annotation${annotationCount === 1 ? '' : 's'} on the FORMAT layer.`
    );
  if (planeMax - planeMin > NUMERIC_EPS)
    warnings.push(
      'Flat geometry occurs at several Z elevations; its full XY projection is imported as one rectangular footprint.'
    );
  if (hasSplines)
    warnings.push(
      'Spline geometry uses its conservative control-point bounds; the preview is a rectangular footprint, not the spline profile.'
    );
  if (!footprintOnly) {
    try {
      loops.push(...stitch(paths));
    } catch (error) {
      if (!(error instanceof TopologyError)) throw error;
      footprintOnly = true;
      warnings.push(
        'Open or branching cut/etch paths were found; all supported geometry is included in one rectangular footprint.'
      );
    }
  }
  check(loops.length <= 300, 'DXF contour limit: 300 closed contours.');
  const minX = Math.min(...extentPoints.map(p => p.x));
  const minY = Math.min(...extentPoints.map(p => p.y));
  const maxX = Math.max(...extentPoints.map(p => p.x));
  const maxY = Math.max(...extentPoints.map(p => p.y));
  const footprint: Loop = {
    type: 'poly',
    points: [
      { x: minX, y: minY },
      { x: maxX, y: minY },
      { x: maxX, y: maxY },
      { x: minX, y: maxY },
    ],
  };
  return { loops, footprint, warnings, footprintOnly };
}

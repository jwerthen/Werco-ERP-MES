import { packContours, outlinesCollide, movedOuter, rotatePoint } from './contour-packing';
import { DXF_CURVE_TOLERANCE_MM, readDXFGeometry } from './dxf';

export type Point = { x: number; y: number };
export type Loop = { type: 'poly'; points: Point[] } | { type: 'circle'; cx: number; cy: number; r: number };
export type Part = {
  id: string;
  name: string;
  loops: Loop[];
  quantity: number;
  rotate: boolean;
  color: number;
  importMode?: 'drawing-bounds';
  referencePaths?: Point[][];
  geometryToleranceMm?: number;
};
export type Stock = {
  width: number;
  height: number;
  margin: number;
  gap: number;
  maxSheets: number;
  bedWidth: number;
  bedHeight: number;
};
export type Placement = {
  partId: string;
  instance: number;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: 0 | 90 | 180 | 270;
  sheet: number;
};
export type Nest = {
  placements: Placement[];
  unplaced: { partId: string; count: number; reason: string }[];
  sheets: number;
  area: number;
  utilization: number;
  method: string;
};
export type Job = {
  version: 1;
  name: string;
  material: string;
  thickness: number;
  parts: Part[];
  stock: Stock;
  bedConfirmed: boolean;
};
const EPS = 1e-7;
const vertexCount = (loops: Loop[]) => loops.reduce((a, l) => a + (l.type === 'poly' ? l.points.length : 1), 0);
const finite = (v: number) => Number.isFinite(v);
function requireValid(ok: unknown, msg: string): asserts ok {
  if (!ok) throw new Error(msg);
}
export const rect = (w: number, h: number): Loop => ({
  type: 'poly',
  points: [
    { x: 0, y: 0 },
    { x: w, y: 0 },
    { x: w, y: h },
    { x: 0, y: h },
  ],
});
export function bounds(loop: Loop) {
  if (loop.type === 'circle')
    return {
      x: loop.cx - loop.r,
      y: loop.cy - loop.r,
      width: loop.r * 2,
      height: loop.r * 2,
    };
  const xs = loop.points.map(p => p.x),
    ys = loop.points.map(p => p.y);
  return {
    x: Math.min(...xs),
    y: Math.min(...ys),
    width: Math.max(...xs) - Math.min(...xs),
    height: Math.max(...ys) - Math.min(...ys),
  };
}
export function loopArea(l: Loop) {
  if (l.type === 'circle') return Math.PI * l.r * l.r;
  return Math.abs(
    l.points.reduce((a, p, i) => {
      const q = l.points[(i + 1) % l.points.length];
      return a + p.x * q.y - q.x * p.y;
    }, 0) / 2
  );
}
export function partArea(p: Part) {
  return loopArea(p.loops[0]) - p.loops.slice(1).reduce((a, l) => a + loopArea(l), 0);
}
export function loopLength(l: Loop) {
  if (l.type === 'circle') return 2 * Math.PI * l.r;
  return l.points.reduce((a, p, i) => {
    const q = l.points[(i + 1) % l.points.length];
    return a + Math.hypot(q.x - p.x, q.y - p.y);
  }, 0);
}
function cross(a: Point, b: Point, c: Point) {
  return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}
function on(a: Point, b: Point, p: Point) {
  return (
    Math.abs(cross(a, b, p)) < EPS &&
    p.x >= Math.min(a.x, b.x) - EPS &&
    p.x <= Math.max(a.x, b.x) + EPS &&
    p.y >= Math.min(a.y, b.y) - EPS &&
    p.y <= Math.max(a.y, b.y) + EPS
  );
}
function intersects(a: Point, b: Point, c: Point, d: Point) {
  const c1 = cross(a, b, c),
    c2 = cross(a, b, d),
    c3 = cross(c, d, a),
    c4 = cross(c, d, b);
  return (
    (((c1 > EPS && c2 < -EPS) || (c1 < -EPS && c2 > EPS)) && ((c3 > EPS && c4 < -EPS) || (c3 < -EPS && c4 > EPS))) ||
    on(a, b, c) ||
    on(a, b, d) ||
    on(c, d, a) ||
    on(c, d, b)
  );
}
function edges(l: Extract<Loop, { type: 'poly' }>) {
  return l.points.map((p, i) => [p, l.points[(i + 1) % l.points.length]]);
}
function distance(p: Point, a: Point, b: Point) {
  const dx = b.x - a.x,
    dy = b.y - a.y;
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(p.x - a.x - t * dx, p.y - a.y - t * dy);
}
function inside(p: Point, l: Loop) {
  if (l.type === 'circle') return Math.hypot(p.x - l.cx, p.y - l.cy) < l.r - EPS;
  let hit = false;
  for (const [a, b] of edges(l)) {
    if (on(a, b, p)) return false;
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) hit = !hit;
  }
  return hit;
}
function boundariesCross(a: Loop, b: Loop): boolean {
  const ab = bounds(a),
    bb = bounds(b);
  if (
    ab.x + ab.width < bb.x - EPS ||
    bb.x + bb.width < ab.x - EPS ||
    ab.y + ab.height < bb.y - EPS ||
    bb.y + bb.height < ab.y - EPS
  )
    return false;
  if (a.type === 'circle' && b.type === 'circle') {
    const d = Math.hypot(a.cx - b.cx, a.cy - b.cy);
    return d <= a.r + b.r + EPS && d >= Math.abs(a.r - b.r) - EPS;
  }
  if (a.type === 'circle' && b.type === 'poly')
    return edges(b).some(
      ([p, q]) =>
        distance({ x: a.cx, y: a.cy }, p, q) <= a.r + EPS &&
        Math.max(Math.hypot(p.x - a.cx, p.y - a.cy), Math.hypot(q.x - a.cx, q.y - a.cy)) >= a.r - EPS
    );
  if (a.type === 'poly' && b.type === 'circle') return boundariesCross(b, a);
  return edges(a as Extract<Loop, { type: 'poly' }>).some(([p, q]) =>
    edges(b as Extract<Loop, { type: 'poly' }>).some(([r, s]) => intersects(p, q, r, s))
  );
}
const sample = (l: Loop): Point => (l.type === 'circle' ? { x: l.cx + l.r, y: l.cy } : l.points[0]);
export function validateLoop(l: Loop) {
  requireValid(l && ['poly', 'circle'].includes(l.type), 'Unsupported contour type.');
  if (l.type === 'circle') {
    requireValid([l.cx, l.cy, l.r].every(finite) && l.r > 0, 'Circle radius must be positive.');
    return;
  }
  requireValid(
    Array.isArray(l.points) && l.points.length >= 3 && l.points.length <= 2000,
    'Polylines need 3–2,000 vertices.'
  );
  requireValid(
    l.points.every(p => finite(p.x) && finite(p.y)),
    'Invalid contour coordinates.'
  );
  const es = edges(l);
  es.forEach(([a, b], i) => {
    requireValid(Math.hypot(a.x - b.x, a.y - b.y) > EPS, 'Duplicate adjacent vertices.');
    const prev = l.points[(i + l.points.length - 1) % l.points.length];
    requireValid(
      !(Math.abs(cross(prev, a, b)) < EPS && (prev.x - a.x) * (b.x - a.x) + (prev.y - a.y) * (b.y - a.y) > EPS),
      'Polyline doubles back on itself.'
    );
    for (let j = i + 1; j < es.length; j++) {
      if (j === i + 1 || (i === 0 && j === es.length - 1)) continue;
      requireValid(!intersects(a, b, ...(es[j] as [Point, Point])), 'Self-intersecting contour.');
    }
  });
  requireValid(loopArea(l) > EPS, 'Contour has no area.');
}
export function normalizeLoops(loops: Loop[]): Loop[] {
  const b = bounds(loops[0]);
  return loops.map(l =>
    l.type === 'circle'
      ? { ...l, cx: l.cx - b.x, cy: l.cy - b.y }
      : { ...l, points: l.points.map(p => ({ x: p.x - b.x, y: p.y - b.y })) }
  );
}
/** Check every segment interval so a reference line cannot cross outside a concave outline. */
export function pathWithinLoop(path: Point[], loop: Loop): boolean {
  const contains = (p: Point) =>
    inside(p, loop) ||
    (loop.type === 'circle'
      ? Math.abs(Math.hypot(p.x - loop.cx, p.y - loop.cy) - loop.r) <= DXF_CURVE_TOLERANCE_MM
      : edges(loop).some(([a, b]) => distance(p, a, b) <= DXF_CURVE_TOLERANCE_MM));
  if (!path.every(contains)) return false;
  if (loop.type === 'circle') return true;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1],
      b = path[i],
      dx = b.x - a.x,
      dy = b.y - a.y;
    const crossings = [0, 1];
    for (const [c, d] of edges(loop)) {
      const ex = d.x - c.x,
        ey = d.y - c.y,
        denominator = dx * ey - dy * ex;
      if (Math.abs(denominator) <= EPS) continue;
      const t = ((c.x - a.x) * ey - (c.y - a.y) * ex) / denominator;
      const u = ((c.x - a.x) * dy - (c.y - a.y) * dx) / denominator;
      if (t > 0 && t < 1 && u >= 0 && u <= 1) crossings.push(t);
    }
    crossings.sort((x, y) => x - y);
    for (let j = 1; j < crossings.length; j++) {
      const t = (crossings[j - 1] + crossings[j]) / 2;
      if (!contains({ x: a.x + t * dx, y: a.y + t * dy })) return false;
    }
  }
  return true;
}
export function validatePart(p: Part) {
  requireValid(
    p && typeof p.id === 'string' && typeof p.name === 'string' && p.name.length > 0 && p.name.length < 200,
    'Invalid part identity.'
  );
  requireValid(
    p.geometryToleranceMm === undefined ||
      (finite(p.geometryToleranceMm) && p.geometryToleranceMm >= 0 && p.geometryToleranceMm <= 0.0254),
    'Invalid geometry approximation tolerance.'
  );
  requireValid(p.importMode === undefined || p.importMode === 'drawing-bounds', 'Invalid DXF import mode.');
  requireValid(Number.isInteger(p.quantity) && p.quantity >= 1 && p.quantity <= 300, 'Part quantity must be 1–300.');
  requireValid(
    typeof p.rotate === 'boolean' && Number.isInteger(p.color) && p.color >= 0 && p.color <= 3,
    'Invalid rotation or color.'
  );
  requireValid(Array.isArray(p.loops) && p.loops.length > 0 && p.loops.length <= 100, 'Part needs 1–100 contours.');
  p.loops.forEach(validateLoop);
  const b = bounds(p.loops[0]);
  requireValid(
    Math.abs(b.x) < EPS && Math.abs(b.y) < EPS && b.width <= 20000 && b.height <= 20000,
    'Part must be normalized and within 787.4 inches.'
  );
  for (let i = 1; i < p.loops.length; i++) {
    requireValid(
      !boundariesCross(p.loops[0], p.loops[i]) && inside(sample(p.loops[i]), p.loops[0]),
      'Hole must lie strictly inside the outer contour.'
    );
    for (let j = 1; j < i; j++)
      requireValid(
        !boundariesCross(p.loops[i], p.loops[j]) &&
          !inside(sample(p.loops[i]), p.loops[j]) &&
          !inside(sample(p.loops[j]), p.loops[i]),
        'Holes may not overlap or contain each other.'
      );
  }
  if (p.referencePaths !== undefined) {
    requireValid(
      Array.isArray(p.referencePaths) && p.referencePaths.length <= 2000,
      'Maximum 2,000 reference paths per part.'
    );
    requireValid(
      p.referencePaths.reduce((sum, path) => sum + (Array.isArray(path) ? path.length : 20001), vertexCount(p.loops)) <=
        20000,
      'Part geometry limit: 20,000 vertices including reference paths.'
    );
    for (const path of p.referencePaths) {
      requireValid(
        Array.isArray(path) &&
          path.length >= 2 &&
          path.length <= 2000 &&
          path.every(point => point && finite(point.x) && finite(point.y)),
        'Reference paths need 2–2,000 finite coordinates.'
      );
      requireValid(pathWithinLoop(path, p.loops[0]), 'Reference geometry extends outside its closed part outline.');
    }
  }
  requireValid(partArea(p) > EPS, 'Part must have positive net area.');
}
export function validateStock(s: Stock) {
  requireValid(s && Object.values(s).every(finite), 'Stock settings must be valid numbers.');
  requireValid(
    s.width > 0 && s.height > 0 && s.bedWidth > 0 && s.bedHeight > 0 && s.bedWidth <= 20000 && s.bedHeight <= 20000,
    'Stock and usable travel must be positive and at most 787.4 inches.'
  );
  requireValid(
    s.width <= s.bedWidth + EPS && s.height <= s.bedHeight + EPS,
    'Sheet exceeds the configured machine envelope.'
  );
  requireValid(
    s.gap >= 0 && s.margin >= 0 && s.width > 2 * s.margin && s.height > 2 * s.margin,
    'Margin or spacing is invalid for this sheet.'
  );
  requireValid(Number.isInteger(s.maxSheets) && s.maxSheets >= 1 && s.maxSheets <= 300, 'Sheet limit must be 1–300.');
}
export function validateJob(data: unknown): Job {
  const j = data as Job;
  requireValid(j && j.version === 1, 'Unsupported job file version.');
  requireValid(typeof j.name === 'string' && j.name.length > 0 && j.name.length < 200, 'Invalid job name.');
  requireValid(['Carbon steel', 'Stainless steel', 'Aluminum'].includes(j.material), 'Unsupported material.');
  requireValid(
    finite(j.thickness) && j.thickness > 0 && j.thickness <= 100,
    'Thickness must be greater than 0 and at most 3.937 inches.'
  );
  requireValid(typeof j.bedConfirmed === 'boolean', 'Missing machine confirmation status.');
  requireValid(Array.isArray(j.parts) && j.parts.length <= 300, 'Maximum 300 part designs.');
  j.parts.forEach(validatePart);
  requireValid(
    j.parts.reduce(
      (a, p) => a + vertexCount(p.loops) + (p.referencePaths?.reduce((n, path) => n + path.length, 0) ?? 0),
      0
    ) <= 20000,
    'Job geometry limit: 20,000 vertices.'
  );
  requireValid(new Set(j.parts.map(p => p.id)).size === j.parts.length, 'Duplicate part IDs.');
  requireValid(j.parts.reduce((a, p) => a + p.quantity, 0) <= 300, 'Maximum 300 instances per job.');
  validateStock(j.stock);
  return j;
}
export function nestParts(parts: Part[], stock: Stock): Nest {
  validateStock(stock);
  requireValid(
    parts.reduce(
      (a, p) => a + vertexCount(p.loops) + (p.referencePaths?.reduce((n, path) => n + path.length, 0) ?? 0),
      0
    ) <= 20000,
    'Job geometry limit: 20,000 vertices.'
  );
  parts.forEach(validatePart);
  requireValid(
    !parts.some(p => p.importMode === 'drawing-bounds'),
    'This saved estimate contains rectangular DXF footprints. Re-import those DXFs to use their actual contours.'
  );
  requireValid(new Set(parts.map(p => p.id)).size === parts.length, 'Duplicate part IDs.');
  requireValid(
    parts.length <= 300 && parts.reduce((a, p) => a + p.quantity, 0) <= 300,
    'Limit: 300 designs / 300 instances.'
  );
  const result = packContours(parts, stock);
  validateNest(parts, stock, result);
  return result;
}
export function validateNest(parts: Part[], s: Stock, n: Nest) {
  const seen = new Set<string>();
  const partById = new Map(parts.map(p => [p.id, p]));
  const worldOutlines = new Map<Placement, Loop>();
  for (const a of n.placements) {
    const part = partById.get(a.partId);
    requireValid(part, 'Unknown part in nest.');
    const b = bounds(part.loops[0]);
    requireValid([a.x, a.y, a.width, a.height].every(finite) && a.width > 0 && a.height > 0, 'Invalid placement.');
    requireValid(
      a.rotation === 0 || ([90, 180, 270].includes(a.rotation) && part.rotate),
      'Rotation violates grain constraint.'
    );
    requireValid(
      Math.abs(a.width - (a.rotation % 180 ? b.height : b.width)) < EPS &&
        Math.abs(a.height - (a.rotation % 180 ? b.width : b.height)) < EPS,
      'Placement dimensions mismatch.'
    );
    requireValid(Number.isInteger(a.sheet) && a.sheet >= 0 && a.sheet < n.sheets, 'Invalid sheet index.');
    requireValid(
      a.x >= s.margin + (part.geometryToleranceMm ?? 0) - EPS &&
        a.y >= s.margin + (part.geometryToleranceMm ?? 0) - EPS &&
        a.x + a.width <= s.width - s.margin - (part.geometryToleranceMm ?? 0) + EPS &&
        a.y + a.height <= s.height - s.margin - (part.geometryToleranceMm ?? 0) + EPS,
      'Placement exceeds sheet margins.'
    );
    const key = a.partId + '-' + a.instance;
    requireValid(
      !seen.has(key) && Number.isInteger(a.instance) && a.instance >= 0 && a.instance < part.quantity,
      'Duplicate or invalid part instance.'
    );
    seen.add(key);
    worldOutlines.set(a, movedOuter(part, a));
  }
  for (let i = 0; i < n.placements.length; i++)
    for (let j = i + 1; j < n.placements.length; j++) {
      const a = n.placements[i],
        b = n.placements[j];
      if (a.sheet !== b.sheet) continue;
      requireValid(
        !outlinesCollide(
          worldOutlines.get(a)!,
          worldOutlines.get(b)!,
          s.gap +
            (partById.get(a.partId)!.geometryToleranceMm ?? 0) +
            (partById.get(b.partId)!.geometryToleranceMm ?? 0)
        ),
        'Nest violates part spacing.'
      );
    }
  for (const p of parts)
    requireValid(
      n.placements.filter(pl => pl.partId === p.id).length +
        n.unplaced.filter(u => u.partId === p.id).reduce((a, u) => a + u.count, 0) ===
        p.quantity,
      'Part quantity mismatch.'
    );
}
export function transformLoops(p: Part, pl: Placement): Loop[] {
  const b = bounds(p.loops[0]);
  const pt = (x: number, y: number) => {
    const r = rotatePoint({ x, y }, pl.rotation, b.width, b.height);
    return { x: pl.x + r.x, y: pl.y + r.y };
  };
  return p.loops.map(l =>
    l.type === 'circle'
      ? {
          type: 'circle',
          ...(() => {
            const c = pt(l.cx, l.cy);
            return { cx: c.x, cy: c.y, r: l.r };
          })(),
        }
      : { type: 'poly', points: l.points.map(q => pt(q.x, q.y)) }
  );
}
export function transformReferencePaths(p: Part, pl: Placement): Point[][] {
  const b = bounds(p.loops[0]);
  return (p.referencePaths ?? []).map(path =>
    path.map(point => {
      const r = rotatePoint(point, pl.rotation, b.width, b.height);
      return { x: r.x + pl.x, y: r.y + pl.y };
    })
  );
}
export function svgPath(loops: Loop[]) {
  return loops
    .map(l =>
      l.type === 'circle'
        ? `M${l.cx - l.r},${l.cy}a${l.r},${l.r} 0 1 0 ${2 * l.r},0a${l.r},${l.r} 0 1 0 ${-2 * l.r},0Z`
        : 'M' + l.points.map(p => `${p.x},${p.y}`).join('L') + 'Z'
    )
    .join(' ');
}
export function exportDXF(parts: Part[], stock: Stock, nest: Nest, sheet: number, units: 'in' | 'mm' = 'in') {
  requireValid(units === 'in' || units === 'mm', 'Unsupported export units.');
  validateStock(stock);
  parts.forEach(validatePart);
  validateNest(parts, stock, nest);
  requireValid(Number.isInteger(sheet) && sheet >= 0 && sheet < nest.sheets, 'Select a valid sheet.');
  const lines: (string | number)[] = [
    0,
    'SECTION',
    2,
    'HEADER',
    9,
    '$ACADVER',
    1,
    'AC1015',
    9,
    '$INSUNITS',
    70,
    units === 'in' ? 1 : 4,
    0,
    'ENDSEC',
    0,
    'SECTION',
    2,
    'ENTITIES',
  ];
  const num = (n: number) => Number((units === 'in' ? n / 25.4 : n).toFixed(10));
  for (const pl of nest.placements.filter(p => p.sheet === sheet)) {
    const p = parts.find(p => p.id === pl.partId)!;
    for (const l of transformLoops(p, pl)) {
      if (l.type === 'circle')
        lines.push(
          0,
          'CIRCLE',
          100,
          'AcDbEntity',
          8,
          'CUT',
          100,
          'AcDbCircle',
          10,
          num(l.cx),
          20,
          num(l.cy),
          30,
          0,
          40,
          num(l.r)
        );
      else {
        lines.push(0, 'LWPOLYLINE', 100, 'AcDbEntity', 8, 'CUT', 100, 'AcDbPolyline', 90, l.points.length, 70, 1);
        for (const pt of l.points) lines.push(10, num(pt.x), 20, num(pt.y));
      }
    }
  }
  lines.push(0, 'ENDSEC', 0, 'EOF');
  return lines.join('\n') + '\n';
}
export type DXFImportReport = { parts: Part[]; warnings: string[]; footprintOnly: boolean };
export function importDXFWithReport(text: string, name: string, unitless: 'mm' | 'in' = 'in'): DXFImportReport {
  const { loops, referencePaths, warnings, geometryToleranceMm } = readDXFGeometry(text, unitless);
  const baseName = name.replace(/\.dxf$/i, '');
  requireValid(
    vertexCount(loops) + referencePaths.reduce((sum, path) => sum + path.length, 0) <= 20000,
    'File geometry limit: 20,000 vertices.'
  );
  loops.forEach(validateLoop);
  const parents = loops.map((l, i) => {
    let parent = -1;
    for (let j = 0; j < loops.length; j++) {
      if (i === j) continue;
      requireValid(
        !boundariesCross(l, loops[j]),
        'Cutting contours touch or intersect. Separate the intended part outlines before importing.'
      );
      if (inside(sample(l), loops[j]) && (parent < 0 || loopArea(loops[j]) < loopArea(loops[parent]))) parent = j;
    }
    return parent;
  });
  const depth = (i: number): number => (parents[i] < 0 ? 0 : 1 + depth(parents[i]));
  const outer = loops.map((_, i) => i).filter(i => depth(i) % 2 === 0);
  const referencesByOuter = new Map<number, Point[][]>();
  for (const path of referencePaths) {
    const owners = outer.filter(index => pathWithinLoop(path, loops[index]));
    requireValid(
      owners.length === 1,
      'Open geometry extends outside a closed part outline or spans several parts. Close the intended cutting outline or separate the reference lines before importing.'
    );
    const list = referencesByOuter.get(owners[0]) ?? [];
    list.push(path);
    referencesByOuter.set(owners[0], list);
  }
  if (referencePaths.length)
    warnings.push(
      `Preserved ${referencePaths.length} unclosed internal path${referencePaths.length === 1 ? '' : 's'} as reference lines. Review whether these are markings or incomplete internal cuts; they do not define part edges or holes.`
    );
  const parts: Part[] = outer.map((i, k) => {
    const origin = bounds(loops[i]),
      references = referencesByOuter.get(i);
    return {
      id: crypto.randomUUID(),
      name: baseName + (outer.length > 1 ? ` · ${k + 1}` : ''),
      loops: normalizeLoops([loops[i], ...loops.filter((_, j) => parents[j] === i)]),
      ...(references
        ? { referencePaths: references.map(path => path.map(p => ({ x: p.x - origin.x, y: p.y - origin.y }))) }
        : {}),
      ...(geometryToleranceMm ? { geometryToleranceMm } : {}),
      quantity: 1,
      rotate: true,
      color: k % 4,
    };
  });
  parts.forEach(validatePart);
  return { parts, warnings, footprintOnly: false };
}
export function importDXF(text: string, name: string, unitless: 'mm' | 'in' = 'in'): Part[] {
  return importDXFWithReport(text, name, unitless).parts;
}
export const defaultStock: Stock = {
  width: 3048,
  height: 1524,
  margin: 9.525,
  gap: 4.7625,
  maxSheets: 10,
  bedWidth: 3657.6,
  bedHeight: 2133.6,
};
const inch = (n: number) => n * 25.4;
export const demoParts: Part[] = [
  {
    id: 'plate',
    name: 'Mounting plate',
    loops: [
      rect(inch(24), inch(16)),
      { type: 'circle', cx: inch(2), cy: inch(2), r: inch(0.75) },
      { type: 'circle', cx: inch(22), cy: inch(14), r: inch(0.75) },
    ],
    quantity: 8,
    rotate: true,
    color: 0,
  },
  {
    id: 'bracket',
    name: 'Corner bracket',
    loops: [
      {
        type: 'poly',
        points: [
          { x: 0, y: 0 },
          { x: inch(16), y: 0 },
          { x: inch(16), y: inch(6) },
          { x: inch(6), y: inch(6) },
          { x: inch(6), y: inch(16) },
          { x: 0, y: inch(16) },
        ],
      },
    ],
    quantity: 6,
    rotate: true,
    color: 1,
  },
  {
    id: 'base',
    name: 'Base plate',
    loops: [rect(inch(12), inch(10)), { type: 'circle', cx: inch(6), cy: inch(5), r: inch(2) }],
    quantity: 6,
    rotate: true,
    color: 2,
  },
  {
    id: 'spacer',
    name: 'Spacer ring',
    loops: [
      { type: 'circle', cx: inch(4), cy: inch(4), r: inch(4) },
      { type: 'circle', cx: inch(4), cy: inch(4), r: inch(1.5) },
    ],
    quantity: 8,
    rotate: true,
    color: 3,
  },
];
export const demoJob: Job = {
  version: 1,
  name: 'Bracket assembly — demo',
  material: 'Carbon steel',
  thickness: 3.175,
  parts: demoParts,
  stock: defaultStock,
  bedConfirmed: false,
};

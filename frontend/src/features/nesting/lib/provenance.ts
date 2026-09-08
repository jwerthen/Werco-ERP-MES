import type { Loop, Part, Point } from './nesting';

export const GEOMETRY_VERSION = 'werco-geometry-v1' as const;
export const IMPORTER_VERSION = 'werco-dxf-v2' as const;
// Hash quantization is finer than the solver grid. It is not a geometry repair.
const HASH_GRID_MM = 1e-8;

export type PartProvenance = {
  version: 1;
  sourceName: string;
  sourceSha256: string;
  sourceHashBasis: 'original-bytes' | 'utf8-text';
  geometrySha256: string;
  geometryVersion: typeof GEOMETRY_VERSION;
  sourceUnits: 'in' | 'mm' | 'unitless';
  resolvedUnits: 'in' | 'mm';
  unitDecision: 'declared' | 'assigned';
  importerVersion: typeof IMPORTER_VERSION;
  warnings: string[];
};

/** Canonical JSON has sorted object keys, finite numbers and no implicit undefined values. */
export function canonicalJSON(value: unknown): string {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(Object.is(value, -0) ? 0 : value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJSON).join(',') + ']';
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype)
    return (
      '{' +
      Object.keys(value)
        .sort()
        .map(key => JSON.stringify(key) + ':' + canonicalJSON((value as Record<string, unknown>)[key]))
        .join(',') +
      '}'
    );
  throw new Error('Fingerprint inputs must contain only finite JSON values.');
}

export async function sha256(value: string | ArrayBuffer): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error('Secure file fingerprinting is unavailable. Open Werco over HTTPS.');
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function canonicalLoop(loop: Loop, origin: Point): unknown {
  const q = (value: number) => {
    if (!Number.isFinite(value) || Math.abs(value / HASH_GRID_MM) > Number.MAX_SAFE_INTEGER)
      throw new Error('Geometry is outside the fingerprint precision range.');
    return Math.round(value / HASH_GRID_MM) || 0;
  };
  if (loop.type === 'circle') return ['circle', q(loop.cx - origin.x), q(loop.cy - origin.y), q(loop.r)];
  const points = loop.points.map(point => [q(point.x - origin.x), q(point.y - origin.y)]);
  if (!points.length) throw new Error('Cannot fingerprint an empty contour.');
  let start = 0;
  for (let index = 1; index < points.length; index++)
    if (
      points[index][0] < points[start][0] ||
      (points[index][0] === points[start][0] && points[index][1] < points[start][1])
    )
      start = index;
  const forward = points.map((_, index) => points[(start + index) % points.length]);
  const reverse = points.map((_, index) => points[(start - index + points.length) % points.length]);
  return ['poly', canonicalJSON(forward) < canonicalJSON(reverse) ? forward : reverse];
}

/** Translation, ring start/direction and hole order normalize away; rotation and handedness do not. */
export function canonicalGeometry(part: Pick<Part, 'loops'>) {
  const outer = part.loops[0];
  if (!outer) throw new Error('Cannot fingerprint a part without geometry.');
  const origin =
    outer.type === 'circle'
      ? { x: outer.cx - outer.r, y: outer.cy - outer.r }
      : { x: Math.min(...outer.points.map(point => point.x)), y: Math.min(...outer.points.map(point => point.y)) };
  return {
    version: GEOMETRY_VERSION,
    units: 'mm',
    gridMm: HASH_GRID_MM,
    outer: canonicalLoop(outer, origin),
    holes: part.loops
      .slice(1)
      .map(loop => canonicalLoop(loop, origin))
      .sort((a, b) => {
        const left = canonicalJSON(a),
          right = canonicalJSON(b);
        return left < right ? -1 : left > right ? 1 : 0;
      }),
  };
}

export const geometryHash = (part: Pick<Part, 'loops'>) => sha256(canonicalJSON(canonicalGeometry(part)));

/** Mirrors the supported ASCII DXF unit declaration; conflicting headers are not silently accepted. */
export function dxfUnitDecision(text: string, assigned: 'in' | 'mm') {
  const lines = text
    .replace(/^\uFEFF/, '')
    .replace(/\r/g, '')
    .trimEnd()
    .split('\n');
  const declared: number[] = [];
  for (let index = 0; index + 3 < lines.length; index += 2) {
    if (Number(lines[index]) === 9 && lines[index + 1].trim() === '$INSUNITS') {
      if (
        Number(lines[index + 2]) !== 70 ||
        lines[index + 3].trim() === '' ||
        ![0, 1, 4].includes(Number(lines[index + 3]))
      )
        throw new Error('Invalid or unsupported DXF unit declaration.');
      declared.push(Number(lines[index + 3].trim()));
    }
  }
  if (new Set(declared).size > 1)
    throw new Error('Conflicting DXF unit declarations. Review source units before import.');
  const unit = declared[0] ?? 0;
  return {
    sourceUnits: unit === 0 ? ('unitless' as const) : unit === 1 ? ('in' as const) : ('mm' as const),
    resolvedUnits: unit === 0 ? assigned : unit === 1 ? ('in' as const) : ('mm' as const),
    unitDecision: unit === 0 ? ('assigned' as const) : ('declared' as const),
  };
}

export function validateProvenance(value: unknown): asserts value is PartProvenance {
  const p = value as PartProvenance;
  const hash = (s: unknown) => typeof s === 'string' && /^[a-f0-9]{64}$/.test(s);
  if (
    !p ||
    p.version !== 1 ||
    p.geometryVersion !== GEOMETRY_VERSION ||
    p.importerVersion !== IMPORTER_VERSION ||
    typeof p.sourceName !== 'string' ||
    !p.sourceName.length ||
    p.sourceName.length > 1024 ||
    !hash(p.sourceSha256) ||
    !hash(p.geometrySha256) ||
    !['original-bytes', 'utf8-text'].includes(p.sourceHashBasis) ||
    !['in', 'mm', 'unitless'].includes(p.sourceUnits) ||
    !['in', 'mm'].includes(p.resolvedUnits) ||
    !['declared', 'assigned'].includes(p.unitDecision) ||
    (p.sourceUnits === 'unitless'
      ? p.unitDecision !== 'assigned'
      : p.unitDecision !== 'declared' || p.sourceUnits !== p.resolvedUnits) ||
    !Array.isArray(p.warnings) ||
    p.warnings.length > 100 ||
    p.warnings.some(w => typeof w !== 'string' || w.length > 2000)
  )
    throw new Error('Invalid CAD source provenance.');
}

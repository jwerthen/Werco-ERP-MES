import type { Job, Loop, Point } from './nesting';
import type { Recipe } from './technology';
export const MM_PER_INCH = 25.4;
export const PSI_PER_BAR = 14.503773773020923;
export const LB_PER_KG = 2.2046226218487757;
export const inToMm = (n: number) => n * MM_PER_INCH;
export const mmToIn = (n: number) => n / MM_PER_INCH;
export const mmToFt = (n: number) => n / (MM_PER_INCH * 12);
export const formatIn = (n: number, digits = 4) => mmToIn(n).toLocaleString('en-US', { maximumFractionDigits: digits });
export function parseInches(text: string): number {
  const value = text.trim().replace(/[″"]$/, '').trim();
  if (/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) return Number(value);
  const m = value.match(/^([+-]?)(?:(\d+)[ -])?(\d+)\/(\d+)$/);
  if (!m || Number(m[4]) === 0) return NaN;
  return (m[1] === '-' ? -1 : 1) * (Number(m[2] ?? 0) + Number(m[3]) / Number(m[4]));
}
export function recipeScale(key: string) {
  return key === 'pressure'
    ? PSI_PER_BAR
    : ['feed', 'kerf', 'nozzle', 'focus', 'height'].includes(key)
      ? 1 / MM_PER_INCH
      : 1;
}
export function displayRecipeValue(value: string, key: string) {
  return value === '' ? '' : String(Number((Number(value) * recipeScale(key)).toFixed(6)));
}
export function storeRecipeValue(value: string, key: string) {
  return value === '' ? '' : String(Number(value) / recipeScale(key));
}
export function scaleLoop(loop: Loop, factor: number): Loop {
  return loop.type === 'circle'
    ? {
        type: 'circle',
        cx: loop.cx * factor,
        cy: loop.cy * factor,
        r: loop.r * factor,
      }
    : {
        type: 'poly',
        points: loop.points.map(p => ({ x: p.x * factor, y: p.y * factor })),
      };
}
const dimensions = ['width', 'height', 'margin', 'gap', 'bedWidth', 'bedHeight'] as const;
export function jobToFile(job: Job) {
  const stock = { ...job.stock };
  for (const k of dimensions) stock[k] = mmToIn(stock[k]);
  return {
    ...job,
    version: 2,
    units: 'in',
    thickness: mmToIn(job.thickness),
    stock,
    parts: job.parts.map(p => {
      const { geometryToleranceMm, ...part } = p;
      return {
        ...part,
        ...(geometryToleranceMm !== undefined ? { geometryTolerance: mmToIn(geometryToleranceMm) } : {}),
        loops: p.loops.map(l => scaleLoop(l, 1 / MM_PER_INCH)),
        ...(p.referencePaths
          ? {
              referencePaths: p.referencePaths.map(path =>
                path.map(point => ({ x: mmToIn(point.x), y: mmToIn(point.y) }))
              ),
            }
          : {}),
      };
    }),
  };
}
function numeric(n: unknown): number {
  if (typeof n !== 'number' || !Number.isFinite(n)) throw new Error('File contains an invalid dimension.');
  return n;
}
export function jobFromFile(input: unknown): unknown {
  if (!input || typeof input !== 'object') throw new Error('Invalid job file.');
  const d = input as Record<string, unknown>;
  if (d.version === 1) return d;
  if (d.version !== 2 || d.units !== 'in')
    throw new Error('Expected a version 2 job with units "in", or a legacy version 1 job.');
  if (!d.stock || typeof d.stock !== 'object' || !Array.isArray(d.parts) || d.parts.length > 300)
    throw new Error('Invalid job stock or parts.');
  const stock = { ...d.stock } as Record<string, unknown>;
  for (const k of dimensions) stock[k] = inToMm(numeric(stock[k]));
  const parts = d.parts.map(p => {
    if (!p || typeof p !== 'object' || !Array.isArray(p.loops) || p.loops.length > 100)
      throw new Error('Invalid part contours.');
    const loops = p.loops.map((l: Loop) => {
      if (!l || !['circle', 'poly'].includes(l.type)) throw new Error('Invalid contour.');
      if (l.type === 'circle')
        return {
          type: 'circle',
          cx: inToMm(numeric(l.cx)),
          cy: inToMm(numeric(l.cy)),
          r: inToMm(numeric(l.r)),
        };
      if (!Array.isArray(l.points) || l.points.length > 2000) throw new Error('Invalid polyline.');
      return {
        type: 'poly',
        points: l.points.map(p => ({
          x: inToMm(numeric(p.x)),
          y: inToMm(numeric(p.y)),
        })),
      };
    });
    if (p.referencePaths !== undefined && (!Array.isArray(p.referencePaths) || p.referencePaths.length > 2000))
      throw new Error('Invalid reference paths.');
    let referencePoints = 0;
    const referencePaths = p.referencePaths?.map((path: Point[]) => {
      if (!Array.isArray(path) || path.length < 2 || path.length > 2000) throw new Error('Invalid reference path.');
      referencePoints += path.length;
      if (referencePoints > 20000) throw new Error('Reference geometry exceeds the 20,000-point limit.');
      return path.map(point => ({ x: inToMm(numeric(point?.x)), y: inToMm(numeric(point?.y)) }));
    });
    const { geometryTolerance, geometryToleranceMm, ...part } = p;
    if (geometryToleranceMm !== undefined) throw new Error('Inch files must declare geometryTolerance in inches.');
    return {
      ...part,
      loops,
      ...(referencePaths ? { referencePaths } : {}),
      ...(geometryTolerance !== undefined ? { geometryToleranceMm: inToMm(numeric(geometryTolerance)) } : {}),
    };
  });
  const { units, ...other } = d;
  void units;
  return {
    ...other,
    version: 1,
    thickness: inToMm(numeric(d.thickness)),
    stock,
    parts,
  };
}
const recipeKeys = ['feed', 'kerf', 'pressure', 'nozzle', 'focus', 'height', 'pierce', 'power'] as const;
export function recipesToFile(recipes: Recipe[]) {
  return {
    version: 2,
    units: {
      length: 'in',
      feed: 'in/min',
      pressure: 'psi',
      power: 'W',
      time: 's',
    },
    recipes: recipes.map(r => {
      const result = { ...r, thickness: mmToIn(r.thickness) };
      for (const k of recipeKeys) result[k] = r[k] === '' ? '' : String(Number(r[k]) * recipeScale(k));
      return result;
    }),
  };
}
export function recipesFromFile(input: unknown): unknown {
  if (Array.isArray(input)) return input;
  if (!input || typeof input !== 'object') throw new Error('Invalid recipe library.');
  const d = input as Record<string, unknown>;
  const u = d.units as Record<string, unknown>;
  if (
    d.version !== 2 ||
    !u ||
    u.length !== 'in' ||
    u.feed !== 'in/min' ||
    u.pressure !== 'psi' ||
    u.power !== 'W' ||
    u.time !== 's' ||
    !Array.isArray(d.recipes) ||
    d.recipes.length > 200
  )
    throw new Error('Recipe library must declare inches, in/min, psi, W and seconds.');
  return d.recipes.map(r => {
    if (!r || typeof r !== 'object') throw new Error('Invalid recipe record.');
    const result = { ...r, thickness: inToMm(numeric(r.thickness)) };
    for (const k of recipeKeys) {
      if (typeof r[k] !== 'string' || (r[k] !== '' && (r[k].trim() === '' || !Number.isFinite(Number(r[k])))))
        throw new Error(`Invalid recipe ${k}.`);
      result[k] = storeRecipeValue(r[k], k);
    }
    return result;
  });
}

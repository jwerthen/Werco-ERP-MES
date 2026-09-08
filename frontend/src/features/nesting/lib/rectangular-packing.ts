import {
  bounds,
  validatePart,
  validateStock,
  partArea,
  type Part,
  type Stock,
  type Nest,
  type Placement,
  type Loop,
} from './nesting';
const EPS = 1e-7;
const vertexCount = (loops: Loop[]) => loops.reduce((n, l) => n + (l.type === 'circle' ? 1 : l.points.length), 0);
function requireValid(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
type Box = { x: number; y: number; width: number; height: number };
const overlap = (a: Box, b: Box) =>
  a.x < b.x + b.width - EPS && a.x + a.width > b.x + EPS && a.y < b.y + b.height - EPS && a.y + a.height > b.y + EPS;
function split(free: Box[], used: Box) {
  let out: Box[] = [];
  for (const f of free) {
    if (!overlap(f, used)) {
      out.push(f);
      continue;
    }
    if (used.x > f.x + EPS) out.push({ ...f, width: used.x - f.x });
    if (used.x + used.width < f.x + f.width - EPS)
      out.push({
        ...f,
        x: used.x + used.width,
        width: f.x + f.width - used.x - used.width,
      });
    if (used.y > f.y + EPS) out.push({ ...f, height: used.y - f.y });
    if (used.y + used.height < f.y + f.height - EPS)
      out.push({
        ...f,
        y: used.y + used.height,
        height: f.y + f.height - used.y - used.height,
      });
  }
  out = out.filter(f => f.width > EPS && f.height > EPS);
  return out.filter(
    (a, i) =>
      !out.some(
        (b, j) =>
          i !== j &&
          b.x <= a.x + EPS &&
          b.y <= a.y + EPS &&
          b.x + b.width >= a.x + a.width - EPS &&
          b.y + b.height >= a.y + a.height - EPS &&
          (j < i || b.width * b.height > a.width * a.height + EPS)
      )
  );
}
export function packRectangles(parts: Part[], stock: Stock): Nest {
  validateStock(stock);
  requireValid(parts.reduce((a, p) => a + vertexCount(p.loops), 0) <= 20000, 'Job geometry limit: 20,000 vertices.');
  parts.forEach(validatePart);
  requireValid(new Set(parts.map(p => p.id)).size === parts.length, 'Duplicate part IDs.');
  requireValid(
    parts.length <= 300 && parts.reduce((a, p) => a + p.quantity, 0) <= 300,
    'Limit: 300 designs / 300 instances.'
  );
  const instances = parts.flatMap(p =>
    Array.from({ length: p.quantity }, (_, i) => ({
      p,
      i,
      b: bounds(p.loops[0]),
    }))
  );
  const candidates = [0, 1, 2].map(mode => {
    const free: Box[][] = [];
    const placements: Placement[] = [];
    const unplaced: Nest['unplaced'] = [];
    const sorted = [...instances].sort((a, b) =>
      mode === 0
        ? b.b.width * b.b.height - a.b.width * a.b.height
        : mode === 1
          ? Math.max(b.b.width, b.b.height) - Math.max(a.b.width, a.b.height)
          : b.b.height - a.b.height
    );
    for (const { p, i, b } of sorted) {
      let best: {
        sheet: number;
        x: number;
        y: number;
        width: number;
        height: number;
        rotation: 0 | 90;
        score: number;
      } | null = null;
      const search = (si: number) => {
        for (const f of free[si])
          for (const rot of (p.rotate ? [0, 90] : [0]) as (0 | 90)[]) {
            const width = rot === 0 ? b.width : b.height,
              height = rot === 0 ? b.height : b.width;
            if (width + stock.gap <= f.width + EPS && height + stock.gap <= f.height + EPS) {
              const score =
                si * 1e12 +
                Math.min(f.width - width - stock.gap, f.height - height - stock.gap) * 1e6 +
                Math.max(f.width - width - stock.gap, f.height - height - stock.gap);
              if (!best || score < best.score)
                best = {
                  sheet: si,
                  x: f.x,
                  y: f.y,
                  width,
                  height,
                  rotation: rot,
                  score,
                };
            }
          }
      };
      free.forEach((_, si) => search(si));
      if (!best && free.length < stock.maxSheets) {
        const w = stock.width - 2 * stock.margin,
          h = stock.height - 2 * stock.margin;
        const fits =
          (b.width <= w + EPS && b.height <= h + EPS) || (p.rotate && b.height <= w + EPS && b.width <= h + EPS);
        if (fits) {
          free.push([
            {
              x: stock.margin,
              y: stock.margin,
              width: w + stock.gap,
              height: h + stock.gap,
            },
          ]);
          search(free.length - 1);
        }
      }
      if (best) {
        const found = best as {
          sheet: number;
          x: number;
          y: number;
          width: number;
          height: number;
          rotation: 0 | 90;
          score: number;
        };
        const { score, ...placement } = found;
        void score;
        placements.push({ ...placement, partId: p.id, instance: i });
        free[found.sheet] = split(free[found.sheet], {
          ...found,
          width: found.width + stock.gap,
          height: found.height + stock.gap,
        });
      } else {
        const existing = unplaced.find(u => u.partId === p.id);
        if (existing) existing.count++;
        else {
          const w = stock.width - 2 * stock.margin,
            h = stock.height - 2 * stock.margin;
          const fits =
            (b.width <= w + EPS && b.height <= h + EPS) || (p.rotate && b.height <= w + EPS && b.width <= h + EPS);
          unplaced.push({
            partId: p.id,
            count: 1,
            reason: fits ? 'Sheet limit reached' : 'Exceeds usable sheet at permitted rotations',
          });
        }
      }
    }
    const area = placements.reduce((a, pl) => a + partArea(parts.find(p => p.id === pl.partId)!), 0);
    return {
      placements,
      unplaced,
      sheets: free.length,
      area,
      utilization: free.length ? (100 * area) / (stock.width * stock.height * free.length) : 0,
      method: 'True contour nesting · rectangular outer profiles',
    };
  });
  candidates.sort(
    (a, b) =>
      b.placements.length - a.placements.length ||
      a.sheets - b.sheets ||
      Math.max(0, ...a.placements.filter(p => p.sheet === a.sheets - 1).map(p => p.y + p.height)) -
        Math.max(0, ...b.placements.filter(p => p.sheet === b.sheets - 1).map(p => p.y + p.height))
  );
  const result = candidates[0];
  return result;
}

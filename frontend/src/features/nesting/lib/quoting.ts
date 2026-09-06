import { bounds, validatePart, validateJob, nestParts, type Part, type Stock, type Nest, demoJob } from './nesting';
import { jobFromFile, jobToFile, mmToIn, inToMm } from './units';
export type SheetOption = {
  id: string;
  width: number;
  height: number;
  enabled: boolean;
  price: number | null;
};
export function editSheetOption(option: SheetOption, patch: Partial<SheetOption>): SheetOption {
  return {
    ...option,
    ...patch,
    ...(patch.width !== undefined || patch.height !== undefined ? { price: null } : {}),
  };
}
export type Quote = {
  version: 1;
  name: string;
  material: string;
  thickness: number;
  parts: Part[];
  margin: number;
  gap: number;
  objective: 'area' | 'cost';
  options: SheetOption[];
};
export type OptionResult = {
  option: SheetOption;
  nest: Nest | null;
  error: string | null;
  complete: boolean;
  area: number;
  cost: number | null;
};
export type Comparison = {
  results: OptionResult[];
  recommendedId: string | null;
  reason: string;
  requested: number;
};
const check = (ok: unknown, message: string) => {
  if (!ok) throw new Error(message);
};
const dim = (value: unknown) => {
  check(typeof value === 'number' && Number.isFinite(value), 'Invalid sheet dimension.');
  return value as number;
};
export const standardOptions: SheetOption[] = [
  ['48x96', 96, 48],
  ['48x120', 120, 48],
  ['60x120', 120, 60],
  ['60x144', 144, 60],
  ['72x144', 144, 72],
  ['84x144', 144, 84],
].map(([id, w, h]) => ({
  id: String(id),
  width: inToMm(Number(w)),
  height: inToMm(Number(h)),
  enabled: ['48x96', '60x120', '60x144'].includes(String(id)),
  price: null,
}));
export const demoQuote: Quote = {
  version: 1,
  name: 'Bracket assembly — demo',
  material: 'Carbon steel',
  thickness: 3.175,
  parts: demoJob.parts,
  margin: 9.525,
  gap: 4.7625,
  objective: 'area',
  options: standardOptions,
};
export function validateQuote(value: unknown): Quote {
  const q = value as Quote;
  check(q && q.version === 1, 'Unsupported estimate version.');
  check(typeof q.name === 'string' && q.name.length > 0 && q.name.length < 200, 'Enter an estimate name.');
  check(['Carbon steel', 'Stainless steel', 'Aluminum'].includes(q.material), 'Choose a supported material.');
  check(
    Number.isFinite(q.thickness) && q.thickness > 0 && q.thickness <= 100,
    'Enter a positive thickness, up to 3.937 inches.'
  );
  check(Array.isArray(q.parts) && q.parts.length <= 300, 'Maximum 300 part designs.');
  q.parts.forEach(validatePart);
  check(new Set(q.parts.map(p => p.id)).size === q.parts.length, 'Duplicate part IDs.');
  check(q.parts.reduce((a, p) => a + p.quantity, 0) <= 300, 'Maximum 300 total parts per estimate.');
  check(
    q.parts.reduce((a, p) => a + p.loops.reduce((a, l) => a + (l.type === 'circle' ? 1 : l.points.length), 0), 0) <=
      20000,
    'Maximum 20,000 geometry vertices.'
  );
  check(
    Number.isFinite(q.margin) && q.margin >= 0 && Number.isFinite(q.gap) && q.gap >= 0,
    'Enter valid nonnegative margins and part spacing.'
  );
  check(['area', 'cost'].includes(q.objective), 'Invalid comparison priority.');
  check(Array.isArray(q.options) && q.options.length > 0 && q.options.length <= 12, 'Choose 1–12 stock sizes.');
  check(new Set(q.options.map(o => o.id)).size === q.options.length, 'Duplicate stock option IDs.');
  q.options.forEach(o => {
    check(typeof o.id === 'string' && o.id.length > 0 && typeof o.enabled === 'boolean', 'Invalid stock option.');
    check(
      Number.isFinite(o.width) &&
        Number.isFinite(o.height) &&
        o.width > 0 &&
        o.height > 0 &&
        o.width <= 20000 &&
        o.height <= 20000,
      'Stock dimensions must be positive, up to 787.4 inches.'
    );
    check(
      o.price === null || (Number.isFinite(o.price) && o.price >= 0),
      'Enter a valid sheet price or leave it blank.'
    );
    check(
      o.price === null ||
        Number.isFinite(
          o.price *
            Math.max(
              1,
              q.parts.reduce((a, p) => a + p.quantity, 0)
            )
        ),
      'Sheet price is too large for the requested quantity.'
    );
  });
  check(
    q.options.some(o => o.enabled),
    'Enable at least one stock size to compare.'
  );
  return q;
}
export function stockFor(q: Quote, o: SheetOption): Stock {
  return {
    width: o.width,
    height: o.height,
    margin: q.margin,
    gap: q.gap,
    maxSheets: Math.max(
      1,
      q.parts.reduce((a, p) => a + p.quantity, 0)
    ),
    bedWidth: o.width,
    bedHeight: o.height,
  };
}
export function compareSheets(q: Quote): Comparison {
  validateQuote(q);
  const requested = q.parts.reduce((a, p) => a + p.quantity, 0);
  const results = q.options
    .filter(o => o.enabled)
    .map(option => {
      try {
        const nest = nestParts(q.parts, stockFor(q, option));
        const complete = nest.unplaced.length === 0 && requested > 0;
        return {
          option,
          nest,
          complete,
          area: nest.sheets * option.width * option.height,
          cost: option.price === null ? null : option.price * nest.sheets,
          error: null,
        };
      } catch (e) {
        return {
          option,
          nest: null,
          complete: false,
          area: 0,
          cost: null,
          error: (e as Error).message,
        };
      }
    });
  if (!requested)
    return {
      results,
      recommendedId: null,
      reason: 'Add parts and quantities to calculate a sheet order.',
      requested,
    };
  const feasible = results.filter(r => r.complete);
  if (!feasible.length)
    return {
      results,
      recommendedId: null,
      reason:
        'No enabled stock size fits all parts. Review oversize parts, rotation locks, margins, or add a larger sheet.',
      requested,
    };
  if (q.objective === 'cost' && feasible.some(r => r.cost === null))
    return {
      results,
      recommendedId: null,
      reason: 'Enter a price for every size that fits the full job to compare material cost, or choose least material.',
      requested,
    };
  feasible.sort(
    (a, b) =>
      (q.objective === 'cost' ? a.cost! - b.cost! : a.area - b.area) ||
      a.area - b.area ||
      a.nest!.sheets - b.nest!.sheets ||
      a.option.id.localeCompare(b.option.id)
  );
  return {
    results,
    recommendedId: feasible[0].option.id,
    reason:
      q.objective === 'cost'
        ? 'Lowest entered material cost among complete options.'
        : 'Least total sheet area to buy among complete options.',
    requested,
  };
}
export function quoteToFile(q: Quote) {
  validateQuote(q);
  const o = q.options[0];
  const job = {
    ...demoJob,
    name: q.name,
    material: q.material,
    thickness: q.thickness,
    parts: q.parts,
    stock: stockFor(q, o),
  };
  return {
    version: 3,
    units: 'in',
    currency: 'USD',
    name: q.name,
    material: q.material,
    thickness: mmToIn(q.thickness),
    parts: jobToFile(job).parts,
    margin: mmToIn(q.margin),
    gap: mmToIn(q.gap),
    objective: q.objective,
    options: q.options.map(o => ({
      ...o,
      width: mmToIn(o.width),
      height: mmToIn(o.height),
    })),
  };
}
export function quoteFromFile(input: unknown): Quote {
  if (!input || typeof input !== 'object') throw new Error('Invalid estimate file.');
  const d = input as Record<string, unknown>;
  if (d.version === 3) {
    check(d.units === 'in', 'Estimate file must explicitly declare inches.');
    check(d.currency === undefined || d.currency === 'USD', 'This estimate uses USD sheet prices.');
    check(Array.isArray(d.options) && d.options.length <= 12, 'Invalid stock options.');
    const parts = (
      jobFromFile({
        ...d,
        version: 2,
        stock: {
          width: 1,
          height: 1,
          margin: 0,
          gap: 0,
          bedWidth: 1,
          bedHeight: 1,
          maxSheets: 1,
        },
        bedConfirmed: false,
      }) as { parts: Part[] }
    ).parts;
    return validateQuote({
      version: 1,
      name: d.name,
      material: d.material,
      thickness: inToMm(dim(d.thickness)),
      parts,
      margin: inToMm(dim(d.margin)),
      gap: inToMm(dim(d.gap)),
      objective: d.objective,
      options: (d.options as SheetOption[]).map(o => ({
        ...o,
        width: inToMm(dim(o.width)),
        height: inToMm(dim(o.height)),
      })),
    });
  }
  const old = validateJob(jobFromFile(input));
  return validateQuote({
    version: 1,
    name: old.name,
    material: old.material,
    thickness: old.thickness,
    parts: old.parts,
    margin: old.stock.margin,
    gap: old.stock.gap,
    objective: 'area',
    options: [
      {
        id: 'saved-stock',
        width: old.stock.width,
        height: old.stock.height,
        enabled: true,
        price: null,
      },
      ...standardOptions
        .filter(o => Math.abs(o.width - old.stock.width) > 1e-6 || Math.abs(o.height - old.stock.height) > 1e-6)
        .map(o => ({ ...o, enabled: false })),
    ],
  });
}
export function oversizeParts(q: Quote, o: SheetOption) {
  const w = o.width - 2 * q.margin,
    h = o.height - 2 * q.margin;
  return q.parts.filter(p => {
    const b = bounds(p.loops[0]);
    return !(
      (b.width <= w + 1e-7 && b.height <= h + 1e-7) ||
      (p.rotate && b.height <= w + 1e-7 && b.width <= h + 1e-7)
    );
  });
}

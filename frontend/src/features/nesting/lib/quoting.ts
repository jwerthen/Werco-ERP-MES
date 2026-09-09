import {
  CURRENT_GEOMETRY_PROFILE,
  resolveGeometryProfile,
  requireCurrentGeometryProfile,
  type GeometryProfileRef,
} from './geometry-profile';
import { analyzeLeftovers, type LeftoverAnalysis } from './leftovers';
import { hasOrientationConstraints, orientationExplanation, type GrainAxis } from './orientation';
import { autoQuotingSpacing } from './spacing';
import {
  validatePolicySnapshot,
  validateSpacingOverride,
  type SpacingPolicySnapshot,
  type SpacingOverride,
} from './spacing-policy';
import {
  validatePart,
  validateJob,
  nestParts,
  partFitsUsableStock,
  type Part,
  type Stock,
  type Nest,
  demoJob,
} from './nesting';
import { compareStableText } from './stable-order';
import { exclusionVertexCount, validateStockExclusions, type StockExclusion } from './stock-exclusions';
import { exclusionsFromFile, exclusionsToFile } from './stock-exclusion-files';
import { jobFromFile, jobToFile, mmToIn, inToMm } from './units';
import {
  catalogFamily,
  hasAcknowledgedCatalogPricing,
  validateMaterialBinding,
  type MaterialBinding,
} from './material-binding';
export type SheetOption = {
  exclusions?: StockExclusion[];
  id: string;
  width: number;
  height: number;
  enabled: boolean;
  price: number | null;
};
export function editSheetOption(option: SheetOption, patch: Partial<SheetOption>): SheetOption {
  const changed = {
    ...option,
    ...patch,
    ...(patch.width !== undefined || patch.height !== undefined ? { price: null } : {}),
  };
  if (changed.exclusions !== undefined) validateStockExclusions(changed.exclusions, changed.width, changed.height);
  return changed;
}
export type Quote = {
  version: 1;
  geometryProfile?: GeometryProfileRef;
  grainAxis?: GrainAxis;
  materialBinding?: MaterialBinding;
  spacingMode?: 'auto' | 'manual' | 'policy';
  spacingPolicy?: SpacingPolicySnapshot;
  spacingOverride?: SpacingOverride;
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
  leftovers?: LeftoverAnalysis;
  leftoverError?: string;
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
/** Every workspace mount gets its own empty estimate and editable stock options. */
export function createBlankQuote(): Quote {
  return {
    version: 1,
    geometryProfile: { ...CURRENT_GEOMETRY_PROFILE },
    name: 'New material estimate',
    material: 'Carbon steel',
    thickness: inToMm(0.125),
    parts: [],
    ...autoQuotingSpacing(inToMm(0.125)),
    spacingMode: 'auto',
    objective: 'area',
    options: standardOptions.map(option => ({ ...option })),
  };
}
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
  resolveGeometryProfile(q.geometryProfile);
  check(q.grainAxis === undefined || ['x', 'y'].includes(q.grainAxis), 'Invalid sheet grain axis.');
  check(q.spacingMode === undefined || ['auto', 'manual', 'policy'].includes(q.spacingMode), 'Invalid spacing mode.');
  check(
    (q.spacingMode === 'policy') === (q.spacingPolicy !== undefined),
    'Applied policy requires policy spacing mode.'
  );
  check(!(q.spacingPolicy && q.spacingOverride), 'Policy conformance and a custom override cannot both apply.');
  if (q.spacingPolicy !== undefined) validatePolicySnapshot(q.spacingPolicy, q.material, q.thickness, q.gap, q.margin);
  if (q.spacingOverride !== undefined) {
    validateSpacingOverride(q.spacingOverride);
    check(q.spacingMode === 'manual', 'A custom-spacing reason requires manual mode.');
  }
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
    q.parts.reduce(
      (a, p) =>
        a +
        p.loops.reduce((a, l) => a + (l.type === 'circle' ? 1 : l.points.length), 0) +
        (p.referencePaths?.reduce((n, path) => n + path.length, 0) ?? 0),
      0
    ) <= 20000,
    'Maximum 20,000 geometry vertices.'
  );
  check(
    Number.isFinite(q.margin) && q.margin >= 0 && Number.isFinite(q.gap) && q.gap >= 0,
    'Enter valid nonnegative margins and part spacing.'
  );
  check(['area', 'cost'].includes(q.objective), 'Invalid comparison priority.');
  check(Array.isArray(q.options) && q.options.length > 0 && q.options.length <= 12, 'Choose 1–12 stock sizes.');
  if (q.materialBinding !== undefined) {
    validateMaterialBinding(q.materialBinding);
    check(
      catalogFamily(q.materialBinding.catalog.category) === q.material,
      'ERP catalog material does not match this material family.'
    );
    if (q.options.some(option => option.price !== null)) {
      check(
        hasAcknowledgedCatalogPricing(q),
        'Resolve the ERP source and acknowledge USD/review before using its sheet prices.'
      );
      check(
        q.options.every(
          option =>
            option.price ===
            Number(q.materialBinding!.resolution!.stocks.find(stock => stock.id === option.id)!.sheet_cost)
        ),
        'Sheet prices do not match the acknowledged ERP source.'
      );
    }
  }
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
    if (o.exclusions !== undefined) validateStockExclusions(o.exclusions, o.width, o.height);
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
    q.parts.reduce(
      (sum, part) =>
        sum +
        part.loops.reduce((n, loop) => n + (loop.type === 'circle' ? 1 : loop.points.length), 0) +
        (part.referencePaths?.reduce((n, path) => n + path.length, 0) ?? 0),
      0
    ) +
      q.options.reduce((sum, option) => sum + exclusionVertexCount(option.exclusions ?? []), 0) <=
      20000,
    'Maximum 20,000 source vertices across parts and stock exclusions.'
  );
  check(
    q.options.some(o => o.enabled),
    'Enable at least one stock size to compare.'
  );
  return q;
}
export function stockFor(q: Quote, o: SheetOption): Stock {
  return {
    ...(q.geometryProfile !== undefined ? { geometryProfile: q.geometryProfile } : {}),
    ...(q.grainAxis !== undefined ? { grainAxis: q.grainAxis } : {}),
    ...(o.exclusions !== undefined ? { exclusions: o.exclusions } : {}),
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
/** Shared browser/server calculation. Call validateQuote before evaluating options. */
export function calculateSheetOption(q: Quote, option: SheetOption): OptionResult {
  requireCurrentGeometryProfile(q.geometryProfile);
  const requested = q.parts.reduce((a, p) => a + p.quantity, 0);
  try {
    const nest = nestParts(q.parts, stockFor(q, option));
    const complete = nest.unplaced.length === 0 && requested > 0;
    let leftovers: LeftoverAnalysis | undefined;
    let leftoverError: string | undefined;
    try {
      leftovers = analyzeLeftovers(q.parts, stockFor(q, option), nest);
    } catch (error) {
      leftoverError = error instanceof Error ? error.message : 'Leftover analysis could not be completed.';
    }
    return {
      leftovers,
      leftoverError,
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
}

export function compareSheets(q: Quote): Comparison {
  validateQuote(q);
  requireCurrentGeometryProfile(q.geometryProfile);
  const requested = q.parts.reduce((a, p) => a + p.quantity, 0);
  const results = q.options.filter(o => o.enabled).map(option => calculateSheetOption(q, option));
  if (!requested)
    return {
      results,
      recommendedId: null,
      reason: 'Add parts and quantities to calculate a sheet order.',
      requested,
    };
  const feasible = results.filter(r => r.complete);
  if (!feasible.length) {
    const orientationIssues = Array.from(new Set(q.parts.map(part => orientationExplanation(part, q)).filter(Boolean)));
    return {
      results,
      recommendedId: null,
      reason: orientationIssues.length
        ? orientationIssues.join(' ')
        : 'The search did not place every part on an enabled stock size. Review oversize parts, rotation rules, sheet grain, margins, or add a larger sheet.',
      requested,
    };
  }
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
      compareStableText(a.option.id, b.option.id)
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
    // Quote 7 is distinct from project 6 and legacy job 8; old readers must
    // reject a constraint-bearing file instead of silently relaxing its rules.
    version:
      q.geometryProfile !== undefined
        ? 14
        : q.options.some(option => option.exclusions !== undefined)
          ? 11
          : q.spacingPolicy || q.spacingOverride
            ? 9
            : hasOrientationConstraints(q.parts, q)
              ? 7
              : 3,
    ...(q.geometryProfile !== undefined ? { geometryProfile: q.geometryProfile } : {}),
    ...(q.spacingPolicy ? { spacingPolicy: q.spacingPolicy } : {}),
    ...(q.spacingOverride ? { spacingOverride: q.spacingOverride } : {}),
    ...(q.grainAxis !== undefined ? { grainAxis: q.grainAxis } : {}),
    units: 'in',
    currency: 'USD',
    spacingMode: q.spacingMode ?? 'manual',
    ...(q.materialBinding ? { materialBinding: q.materialBinding } : {}),
    name: q.name,
    material: q.material,
    thickness: mmToIn(q.thickness),
    parts: jobToFile(job).parts,
    margin: q.spacingPolicy ? Number(q.spacingPolicy.margin_in) : mmToIn(q.margin),
    gap: q.spacingPolicy ? Number(q.spacingPolicy.gap_in) : mmToIn(q.gap),
    objective: q.objective,
    options: q.options.map(o => ({
      ...o,
      width: mmToIn(o.width),
      height: mmToIn(o.height),
      ...(o.exclusions !== undefined ? { exclusions: exclusionsToFile(o.exclusions) } : {}),
    })),
  };
}
export function quoteFromFile(input: unknown): Quote {
  if (!input || typeof input !== 'object') throw new Error('Invalid estimate file.');
  const d = input as Record<string, unknown>;
  if (d.version === 14) requireCurrentGeometryProfile(d.geometryProfile);
  else
    check(
      !Object.prototype.hasOwnProperty.call(d, 'geometryProfile'),
      'Geometry profiles require a version 14 estimate.'
    );
  check(
    d.version === 9 ||
      d.version === 11 ||
      d.version === 14 ||
      (d.spacingPolicy === undefined && d.spacingOverride === undefined && d.spacingMode !== 'policy'),
    'Spacing policies require a version 9 or 11 estimate.'
  );
  check(
    d.version === 11 ||
      d.version === 14 ||
      !Array.isArray(d.options) ||
      d.options.every(option => !option || !Object.prototype.hasOwnProperty.call(option, 'exclusions')),
    'Stock exclusions require a version 11 estimate.'
  );
  if (d.version === 3 || d.version === 7 || d.version === 9 || d.version === 11 || d.version === 14) {
    check(d.units === 'in', 'Estimate file must explicitly declare inches.');
    check(d.version !== 3 || d.grainAxis === undefined, 'Sheet grain requires estimate version 7 or 9.');
    check(d.currency === undefined || d.currency === 'USD', 'This estimate uses USD sheet prices.');
    check(Array.isArray(d.options) && d.options.length <= 12, 'Invalid stock options.');
    const partSource = { ...d };
    delete partSource.geometryProfile;
    const parts = (
      jobFromFile({
        ...partSource,
        version: d.version === 7 || d.version === 9 || d.version === 11 || d.version === 14 ? 8 : 2,
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
      ...(d.version === 14 ? { geometryProfile: d.geometryProfile } : {}),
      ...(d.grainAxis !== undefined ? { grainAxis: d.grainAxis } : {}),
      name: d.name,
      spacingMode: d.spacingMode ?? 'manual',
      ...(d.spacingPolicy !== undefined ? { spacingPolicy: d.spacingPolicy } : {}),
      ...(d.spacingOverride !== undefined ? { spacingOverride: d.spacingOverride } : {}),
      ...(d.materialBinding !== undefined ? { materialBinding: d.materialBinding } : {}),
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
        ...(o.exclusions !== undefined
          ? { exclusions: exclusionsFromFile(o.exclusions, inToMm(dim(o.width)), inToMm(dim(o.height))) }
          : {}),
      })),
    });
  }
  const old = validateJob(jobFromFile(input));
  return validateQuote({
    version: 1,
    name: old.name,
    ...(old.stock.geometryProfile !== undefined ? { geometryProfile: old.stock.geometryProfile } : {}),
    ...(old.stock.grainAxis !== undefined ? { grainAxis: old.stock.grainAxis } : {}),
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
        ...(old.stock.exclusions !== undefined ? { exclusions: old.stock.exclusions } : {}),
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
  try {
    const stock = stockFor(q, o);
    return q.parts.filter(part => !partFitsUsableStock(part, stock));
  } catch {
    // A bounded geometry-analysis failure is reported by the option result.
    // Do not mislabel unassessed parts as oversized or crash the review panel.
    return [];
  }
}

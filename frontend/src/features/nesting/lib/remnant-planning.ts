import type { RemnantPlan } from '../../../types/remnantPlanning';
import { canonicalRemnantEvidence, validateRemnantPlan } from './remnant-evidence';
import { projectFromFile, requireCurrentProjectGeometry } from './quote-project';
import { calculateSheetOption, quoteFromFile, quoteToFile, stockFor, type OptionResult } from './quoting';
import { nestParts, partArea, validateNest, type Nest, type Part, type Stock } from './nesting';
import { prepareStockDomain, stockForRecordedPiece, type DomainStock } from './remnant-domain';
import { analyzeLeftovers, type LeftoverAnalysis } from './leftovers';

export type RawQuote14 = ReturnType<typeof quoteToFile>;
export type InstanceMap = { part_id: string; originals: number[] }[];
export type RecordedPieceResult = {
  nest: Nest | null;
  error: string | null;
  complete: boolean;
  area: number;
  leftovers?: LeftoverAnalysis;
  leftoverError?: string;
};
type StageBase = {
  type: 'stage';
  protocol: 2;
  input_sha256: string;
  sequence: number;
  stage_id: string;
  group_id: string;
  units: 'mm';
  requested: number;
};
export type BaselineStageMessage = StageBase & {
  stage_kind: 'baseline';
  option_id: string;
  depends_on: null;
  instance_map: null;
  stock: Stock;
  result: OptionResult;
};
export type RecordedPieceStageMessage = StageBase & {
  stage_kind: 'recorded_piece';
  option_id: null;
  depends_on: null;
  instance_map: null;
  stock: DomainStock | null;
  result: RecordedPieceResult;
};
export type ResidualStageMessage = StageBase & {
  stage_kind: 'residual';
  option_id: string;
  depends_on: string;
  instance_map: InstanceMap;
  stock: Stock | null;
  result: OptionResult | null;
};
export type RemnantStageMessage = BaselineStageMessage | RecordedPieceStageMessage | ResidualStageMessage;
export type StagePlan = Pick<
  RemnantStageMessage,
  'sequence' | 'stage_id' | 'stage_kind' | 'group_id' | 'option_id' | 'depends_on'
>;
export type RemnantSummaryMessage = {
  type: 'summary';
  protocol: 2;
  input_sha256: string;
  evaluated_keys: { stage_id: string; group_id: string }[];
  evaluated_count: number;
  complete_option_count: number;
  total_options: number;
  stop_reason: 'completed';
};
type RawProject = { version: 18; groups: { id: string; quote: RawQuote14 }[]; remnantPlan: RemnantPlan };
const check = (ok: unknown, text: string): void => {
  if (!ok) throw new Error(`Recorded-piece comparison: ${text}`);
};
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const requested = (parts: { quantity: number }[]) => parts.reduce((n, p) => n + p.quantity, 0);
const sameNumber = (a: number, b: number) =>
  Number.isFinite(a) && Math.abs(a - b) <= Math.max(1e-7, Math.abs(b) * 1e-12);
const errorText = (error: unknown) =>
  (error instanceof Error ? error.message : 'The recorded-piece calculation could not finish.').slice(0, 1000);
function emitted<T>(value: T): T {
  const json = JSON.stringify(value, (_key, item: unknown) => {
    if (typeof item === 'number' && !Number.isFinite(item)) throw new Error('nonfinite_output');
    return item;
  });
  if (new TextEncoder().encode(json).length + 1 > 8 * 1024 * 1024) throw new Error('output_limit');
  // Yield a transport-normalized owned copy. Consumer edits can never change
  // the later residual calculation or its predecessor's original-instance map.
  return JSON.parse(json) as T;
}

function rawProject(value: unknown): RawProject {
  check(object(value) && value.version === 18 && object(value.remnantPlan), 'a source-bound project18 is required.');
  // Validate the project and aggregate budgets while retaining its original inch
  // spelling for evidence fingerprints. Never hash a mm→inch round trip.
  const project = projectFromFile(value);
  requireCurrentProjectGeometry(project);
  const raw = value as RawProject;
  check(
    raw.groups.some(g => g.id === raw.remnantPlan.groupId && g.quote.parts.length > 0),
    'the assigned material group must contain parts.'
  );
  return raw;
}

/** Input-derived ordinal identities; they are not inventory or stock-option IDs. */
export function buildStagePlan(value: unknown): StagePlan[] {
  const raw = rawProject(value);
  const plan: StagePlan[] = [];
  const add = (stage: Omit<StagePlan, 'sequence' | 'stage_id'>) => {
    const sequence = plan.length + 1;
    plan.push({ ...stage, sequence, stage_id: `stage-${String(sequence).padStart(2, '0')}` });
  };
  for (const group of raw.groups)
    if (group.quote.parts.length)
      for (const option of group.quote.options.filter(o => o.enabled))
        add({ stage_kind: 'baseline', group_id: group.id, option_id: option.id, depends_on: null });
  check(plan.length > 0, 'enable a full-sheet baseline option.');
  const group = raw.groups.find(g => g.id === raw.remnantPlan.groupId)!;
  check(
    group.quote.options.some(o => o.enabled),
    'the recorded-piece group needs an enabled full-sheet option.'
  );
  add({ stage_kind: 'recorded_piece', group_id: group.id, option_id: null, depends_on: null });
  const predecessor = plan[plan.length - 1].stage_id;
  for (const option of group.quote.options.filter(o => o.enabled))
    add({ stage_kind: 'residual', group_id: group.id, option_id: option.id, depends_on: predecessor });
  check(
    plan.length <= 36,
    'limit baseline plus recorded-piece/residual evaluations to 36 by disabling unneeded stock options.'
  );
  return plan;
}

function validateRecorded(parts: Part[], stock: DomainStock | null, result: RecordedPieceResult): void {
  if (result.error !== null) {
    check(
      typeof result.error === 'string' &&
        result.error.length > 0 &&
        result.error.length <= 1000 &&
        result.nest === null &&
        result.complete === false &&
        result.area === 0 &&
        result.leftovers === undefined &&
        result.leftoverError === undefined,
      'invalid failed recorded-piece result.'
    );
    if (stock) prepareStockDomain(stock);
    return;
  }
  check(stock?.domain && result.nest, 'successful recorded-piece results require actual stock and placements.');
  const s = stock!,
    nest = result.nest!;
  validateNest(parts, s, nest);
  const gross = prepareStockDomain(s).grossArea;
  const area = nest.placements.reduce(
    (sum, placement) => sum + partArea(parts.find(p => p.id === placement.partId)!),
    0
  );
  check(
    s.maxSheets === 1 &&
      nest.sheets <= 1 &&
      sameNumber(nest.area, area) &&
      sameNumber(nest.utilization, nest.sheets ? (100 * area) / gross : 0) &&
      sameNumber(result.area, gross * nest.sheets) &&
      result.complete === (nest.placements.length === requested(parts) && !nest.unplaced.length),
    'recorded-piece area, quantity or capacity does not reconcile.'
  );
  check(!(result.leftovers && result.leftoverError), 'conflicting leftover status.');
}

/** The compact solver retains original geometry and part order. Every original
 * instance belongs to exactly one of recorded placements or the residual map. */
export function deriveResidual(
  rawQuote: unknown,
  recorded: RecordedPieceStageMessage
): {
  quote: RawQuote14;
  instanceMap: InstanceMap;
  requested: number;
} {
  const quote = quoteFromFile(rawQuote);
  check(
    recorded.stage_kind === 'recorded_piece' &&
      recorded.option_id === null &&
      recorded.depends_on === null &&
      recorded.instance_map === null &&
      recorded.requested === requested(quote.parts),
    'invalid recorded-piece predecessor.'
  );
  validateRecorded(quote.parts, recorded.stock, recorded.result);
  const placed = new Map(quote.parts.map(part => [part.id, new Set<number>()]));
  for (const p of recorded.result.nest?.placements ?? []) placed.get(p.partId)!.add(p.instance);
  const instanceMap: InstanceMap = [];
  for (const part of quote.parts) {
    const originals = Array.from({ length: part.quantity }, (_, i) => i).filter(i => !placed.get(part.id)!.has(i));
    if (originals.length) instanceMap.push({ part_id: part.id, originals });
  }
  const source = rawQuote as RawQuote14;
  const residual: RawQuote14 = {
    ...source,
    parts: source.parts.flatMap(part => {
      const map = instanceMap.find(item => item.part_id === part.id);
      return map ? [{ ...part, quantity: map.originals.length }] : [];
    }),
  };
  const count = instanceMap.reduce((sum, item) => sum + item.originals.length, 0);
  check(
    count + (recorded.result.nest?.placements.length ?? 0) === requested(quote.parts),
    'original instance partition is incomplete.'
  );
  return { quote: residual, instanceMap, requested: count };
}

/** Remnant-only async path; ordinary protocol1 calculation remains unchanged.
 * Runtime owners enforce the single wall-clock/heap/message budgets. */
export async function* calculateRemnantProject(
  value: unknown,
  inputSha256: string
): AsyncGenerator<RemnantStageMessage | RemnantSummaryMessage> {
  check(/^[a-f0-9]{64}$/.test(inputSha256), 'invalid input fingerprint.');
  canonicalRemnantEvidence(value); // reject non-JSON data before creating an owned snapshot
  const raw = rawProject(JSON.parse(JSON.stringify(value)));
  const plan = buildStagePlan(raw);
  const target = raw.groups.find(g => g.id === raw.remnantPlan.groupId)!;
  await validateRemnantPlan(raw.remnantPlan, {
    companyId: raw.remnantPlan.snapshot.companyId,
    groupId: target.id,
    quote: target.quote,
  });
  const project = projectFromFile(raw);
  let recorded: RecordedPieceStageMessage | undefined;
  let residual: ReturnType<typeof deriveResidual> | undefined;
  let complete = 0;
  const keys: RemnantSummaryMessage['evaluated_keys'] = [];
  for (const stage of plan) {
    const group = project.groups.find(g => g.id === stage.group_id)!;
    const base = {
      type: 'stage' as const,
      protocol: 2 as const,
      input_sha256: inputSha256,
      units: 'mm' as const,
      sequence: stage.sequence,
      stage_id: stage.stage_id,
      group_id: stage.group_id,
    };
    let output: RemnantStageMessage;
    if (stage.stage_kind === 'baseline') {
      const option = group.quote.options.find(o => o.id === stage.option_id && o.enabled)!;
      const stock = stockFor(group.quote, option),
        result = calculateSheetOption(group.quote, option);
      if (result.nest) validateNest(group.quote.parts, stock, result.nest);
      output = {
        ...base,
        stage_kind: 'baseline',
        option_id: option.id,
        depends_on: null,
        instance_map: null,
        requested: requested(group.quote.parts),
        stock,
        result,
      };
      if (result.complete) complete++;
    } else if (stage.stage_kind === 'recorded_piece') {
      let stock: DomainStock | null = null,
        result: RecordedPieceResult;
      try {
        const candidate = stockForRecordedPiece(raw.remnantPlan.snapshot.evidence, {
          geometryProfile: group.quote.geometryProfile!,
          margin: group.quote.margin,
          gap: group.quote.gap,
          zoneClearanceIn: raw.remnantPlan.zoneClearanceIn,
        });
        const domain = prepareStockDomain(candidate);
        stock = candidate;
        const nest = nestParts(group.quote.parts, stock);
        result = {
          nest,
          error: null,
          complete: !nest.unplaced.length && nest.placements.length === requested(group.quote.parts),
          area: domain.grossArea * nest.sheets,
        };
        try {
          result.leftovers = analyzeLeftovers(group.quote.parts, stock, nest);
        } catch (error) {
          result.leftoverError = errorText(error);
        }
      } catch (error) {
        result = { nest: null, error: errorText(error), complete: false, area: 0 };
      }
      validateRecorded(group.quote.parts, stock, result);
      recorded = {
        ...base,
        stage_kind: 'recorded_piece',
        option_id: null,
        depends_on: null,
        instance_map: null,
        requested: requested(group.quote.parts),
        stock,
        result,
      };
      output = recorded;
      residual = deriveResidual(target.quote, recorded);
    } else {
      check(recorded && residual && stage.depends_on === recorded.stage_id, 'missing recorded-piece predecessor.');
      let stock: Stock | null = null,
        result: OptionResult | null = null;
      if (residual!.requested) {
        const quote = quoteFromFile(residual!.quote),
          option = quote.options.find(o => o.id === stage.option_id && o.enabled)!;
        stock = stockFor(quote, option);
        result = calculateSheetOption(quote, option);
        if (result.nest) validateNest(quote.parts, stock, result.nest);
      }
      output = {
        ...base,
        stage_kind: 'residual',
        option_id: stage.option_id!,
        depends_on: stage.depends_on!,
        instance_map: residual!.instanceMap,
        requested: residual!.requested,
        stock,
        result,
      };
      if (!residual!.requested || result?.complete) complete++;
    }
    keys.push({ stage_id: output.stage_id, group_id: output.group_id });
    yield emitted(output);
  }
  yield emitted({
    type: 'summary',
    protocol: 2,
    input_sha256: inputSha256,
    evaluated_keys: keys,
    evaluated_count: keys.length,
    complete_option_count: complete,
    total_options: plan.length,
    stop_reason: 'completed',
  });
}

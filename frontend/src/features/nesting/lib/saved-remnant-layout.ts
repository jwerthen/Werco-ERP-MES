import type { NestingRunCheckpoint, NestingRunDetail } from '../../../types/nestingRun';
import { CURRENT_GEOMETRY_PROFILE, requireCurrentGeometryProfile } from './geometry-profile';
import { leftoversToFile } from './leftovers';
import { canonicalJSON } from './provenance';
import { projectFromFile } from './quote-project';
import { comparisonFromResults, quoteFromFile, type Quote } from './quoting';
import { stockForRecordedPiece, prepareStockDomain } from './remnant-domain';
import { REMNANT_DOMAIN_PROFILE, requireRemnantDomainProfile } from './remnant-domain-profile';
import { validateRemnantFile } from './remnant-evidence';
import {
  buildStagePlan,
  deriveResidual,
  type InstanceMap,
  type RecordedPieceStageMessage,
  type RemnantStageMessage,
} from './remnant-planning';
import { validateSheetGeometry, type SavedGeometryRun } from './saved-layout';
import type { ServerOptionMessage } from './server-run';

const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Saved planning geometry: ${message}`);
};
const same = (a: unknown, b: unknown) =>
  canonicalJSON(JSON.parse(JSON.stringify(a))) === canonicalJSON(JSON.parse(JSON.stringify(b)));
export type ValidatedPlanningStage = {
  quote: Quote;
  output: RemnantStageMessage;
  instanceMap?: InstanceMap;
  profileLabel: string;
};

/** Validate a staged frame against its original inch source and exact predecessor.
 * Called at explicit preview/export boundaries, never as a render-time calculation. */
export function validatePlanningStage(
  raw: unknown,
  output: RemnantStageMessage,
  predecessor?: RecordedPieceStageMessage
): ValidatedPlanningStage {
  const project = projectFromFile(raw),
    plan = buildStagePlan(raw);
  const expected = plan[output.sequence - 1];
  check(
    output.type === 'stage' &&
      output.protocol === 2 &&
      output.units === 'mm' &&
      expected &&
      Object.keys(expected).every(
        key => output[key as keyof typeof expected] === expected[key as keyof typeof expected]
      ),
    'stage identity differs from the input schedule.'
  );
  const group = project.groups.find(g => g.id === output.group_id)!;
  const rawQuote = (raw as { groups: { id: string; quote: unknown }[] }).groups.find(g => g.id === group.id)!.quote;
  const count = group.quote.parts.reduce((n, p) => n + p.quantity, 0);
  let quote = group.quote;
  let instanceMap: InstanceMap | undefined;
  if (output.stage_kind === 'recorded_piece') {
    check(output.requested === count && output.instance_map === null, 'recorded-piece quantity differs.');
    const selection = project.remnantPlan!;
    let stock: ReturnType<typeof stockForRecordedPiece> | null = null;
    try {
      const candidate = stockForRecordedPiece(selection.snapshot.evidence, {
        geometryProfile: CURRENT_GEOMETRY_PROFILE,
        margin: quote.margin,
        gap: quote.gap,
        zoneClearanceIn: selection.zoneClearanceIn,
      });
      prepareStockDomain(candidate);
      stock = candidate;
    } catch {
      check(
        output.stock === null && typeof output.result.error === 'string' && !!output.result.error,
        'a source reconstruction failure requires a failed stage.'
      );
    }
    check(same(stock, output.stock), 'recorded material differs from its immutable observation.');
    deriveResidual(rawQuote, output); // original geometry, counts, one-piece capacity and partition
    if (output.result.leftovers) {
      check(
        output.result.leftovers.version === 'werco-leftovers-v4' &&
          output.stock &&
          output.result.nest &&
          !output.result.leftoverError,
        'invalid recorded-piece leftover evidence.'
      );
      leftoversToFile(output.result.leftovers, { parts: quote.parts, stock: output.stock!, nest: output.result.nest! });
    }
  } else {
    if (output.stage_kind === 'residual') {
      check(
        predecessor &&
          predecessor.stage_id === output.depends_on &&
          predecessor.group_id === output.group_id &&
          predecessor.input_sha256 === output.input_sha256,
        'the exact recorded-piece predecessor is missing.'
      );
      // Revalidate predecessor source authority before trusting its original-instance set.
      validatePlanningStage(raw, predecessor!);
      const residual = deriveResidual(rawQuote, predecessor!);
      check(
        output.requested === residual.requested && same(output.instance_map, residual.instanceMap),
        'residual original-instance partition differs.'
      );
      instanceMap = residual.instanceMap;
      quote = quoteFromFile(residual.quote);
      if (!residual.requested) {
        check(
          output.stock === null && output.result === null && output.instance_map.length === 0,
          'zero residual must not invent a stock or result.'
        );
        return { quote, output, instanceMap, profileLabel: 'Recorded-piece domain and compensated envelopes' };
      }
    } else check(output.requested === count && output.instance_map === null, 'baseline quantity differs.');
    check(output.stock && output.result, 'nonempty full-sheet stages need a stock and result.');
    const ordinary: ServerOptionMessage = {
      type: 'option',
      protocol: 1,
      input_sha256: output.input_sha256,
      sequence: output.sequence,
      group_id: output.group_id,
      option_id: output.option_id,
      units: 'mm',
      requested: output.requested,
      stock: output.stock!,
      result: output.result!,
    };
    // Runtime authority is checked by the saved entry point. The option adapter
    // reuses source-bound v7 validation without weakening protocol2 authority.
    validateSheetGeometry(quote, ordinary, 'werco-leftovers-v3');
    comparisonFromResults(quote, [output.result!]);
  }
  return { quote, output, instanceMap, profileLabel: 'Recorded-piece domain and compensated envelopes' };
}

export function validateStagedRunIdentity(run: SavedGeometryRun): void {
  const settings = run.settings,
    runtime = settings.runtime as Record<string, unknown> | undefined;
  check(
    run.solver_version === 'werco-contour-v7' &&
      settings.solver_version === run.solver_version &&
      settings.protocol === 2 &&
      settings.units === 'mm' &&
      runtime?.protocol === 1 &&
      runtime.solver_version === run.solver_version &&
      typeof run.bundle_sha256 === 'string' &&
      /^[a-f0-9]{64}$/.test(run.bundle_sha256) &&
      runtime.bundle_sha256 === run.bundle_sha256 &&
      typeof run.node_version === 'string' &&
      /^v22\.\d+\.\d+$/.test(run.node_version) &&
      runtime.node_version === run.node_version &&
      typeof run.release_identity === 'string' &&
      run.release_identity.length > 0 &&
      runtime.release === run.release_identity,
    'recorded runtime and staged protocol do not agree.'
  );
  requireCurrentGeometryProfile(settings.geometry_profile);
  requireRemnantDomainProfile(settings.remnant_domain_profile);
  check(same(settings.remnant_domain_profile, REMNANT_DOMAIN_PROFILE), 'unknown recorded domain profile.');
}

/** Metadata came from this permissioned run detail; bind every fetched frame to it. */
export function bindSavedStage(run: NestingRunDetail, checkpoint: NestingRunCheckpoint): RemnantStageMessage {
  const output = checkpoint.result,
    metadata = run.checkpoints.find(c => c.sequence === checkpoint.sequence);
  check(
    metadata &&
      same(
        metadata,
        Object.fromEntries(Object.entries(checkpoint).filter(([key]) => key !== 'result' && key !== 'schema_version'))
      ) &&
      /^[a-f0-9]{64}$/.test(checkpoint.content_sha256),
    'checkpoint receipt differs from this run.'
  );
  check(
    output.type === 'stage' &&
      output.protocol === 2 &&
      output.input_sha256 === run.input_sha256 &&
      output.sequence === checkpoint.sequence &&
      checkpoint.option_id === output.stage_id &&
      checkpoint.group_id === output.group_id &&
      checkpoint.stage_kind === output.stage_kind &&
      checkpoint.source_option_id === output.option_id &&
      checkpoint.depends_on === output.depends_on,
    'checkpoint is not this stage.'
  );
  if (output.type !== 'stage') throw new Error('Expected a saved planning stage.');
  const nest = output.result?.nest;
  check(
    checkpoint.schema_version === undefined || checkpoint.schema_version === 1,
    'unknown checkpoint receipt schema.'
  );
  check(
    checkpoint.complete === (output.result ? output.result.complete : output.requested === 0) &&
      checkpoint.sheets === (nest?.sheets ?? 0) &&
      checkpoint.placed === (nest?.placements.length ?? 0) &&
      checkpoint.unplaced === (nest ? nest.unplaced.reduce((n, p) => n + p.count, 0) : output.requested),
    'checkpoint quantity summary differs from its actual stage.'
  );
  return output;
}

export async function validateSavedPlanningStage(
  run: NestingRunDetail,
  raw: unknown,
  checkpoint: NestingRunCheckpoint,
  predecessor?: NestingRunCheckpoint
): Promise<ValidatedPlanningStage> {
  validateStagedRunIdentity(run);
  await validateRemnantFile(raw, run.company_id);
  const output = bindSavedStage(run, checkpoint);
  const prior = predecessor ? bindSavedStage(run, predecessor) : undefined;
  check(!prior || prior.stage_kind === 'recorded_piece', 'wrong predecessor kind.');
  return validatePlanningStage(raw, output, prior as RecordedPieceStageMessage | undefined);
}

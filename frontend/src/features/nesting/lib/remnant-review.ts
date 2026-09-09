import type { NestingRunReport } from '../../../types/nestingRun';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { leftoversToFile } from './leftovers';
import type { Loop } from './nesting';
import { canonicalJSON, sha256 } from './provenance';
import { validateRemnantFile } from './remnant-evidence';
import { REMNANT_DOMAIN_PROFILE } from './remnant-domain-profile';
import { buildStagePlan, type RemnantStageMessage } from './remnant-planning';
import { validatePlanningStage, validateSavedPlanningStage, validateStagedRunIdentity } from './saved-remnant-layout';
import { mmToIn } from './units';

const loopInches = (loop: Loop) =>
  loop.type === 'circle'
    ? { type: 'circle', cx: mmToIn(loop.cx), cy: mmToIn(loop.cy), r: mmToIn(loop.r) }
    : { type: 'poly', points: loop.points.map(p => ({ x: mmToIn(p.x), y: mmToIn(p.y) })) };

/** An explicit local export rechecks every retained stage, including regenerated
 * domain leftovers. Partial plans remain partial; this is no server audit. */
export async function buildRemnantReview(
  raw: unknown,
  stages: RemnantStageMessage[],
  identity: { companyId: number; estimatorId: number | null },
  options: { searchFinished?: boolean } = {}
) {
  const input = JSON.parse(JSON.stringify(raw)) as unknown;
  const frames = JSON.parse(JSON.stringify(stages)) as RemnantStageMessage[];
  await validateRemnantFile(input, identity.companyId);
  const plan = buildStagePlan(input),
    inputSha256 = await sha256(canonicalJSON(input));
  if (!frames.length || frames.length > plan.length) throw new Error('No valid planning stage prefix is available.');
  if (options.searchFinished && frames.length !== plan.length)
    throw new Error('A finished review requires every planned stage.');
  const predecessor = frames.find(frame => frame.stage_kind === 'recorded_piece');
  const outputs = frames.map((frame, index) => {
    if (frame.sequence !== index + 1 || frame.input_sha256 !== inputSha256)
      throw new Error('Planning stages differ from the current input or order.');
    const checked = validatePlanningStage(input, frame, predecessor);
    const { stock, result } = frame;
    const originalMap = new Map(checked.instanceMap?.map(item => [item.part_id, item.originals]));
    return {
      stageId: frame.stage_id,
      stageKind: frame.stage_kind,
      groupId: frame.group_id,
      stockOptionId: frame.option_id,
      dependsOn: frame.depends_on,
      originalInstanceMap: checked.instanceMap ?? null,
      requested: frame.requested,
      complete: frame.stage_kind === 'residual' && frame.requested === 0 ? true : (result?.complete ?? false),
      pieceCount: stock?.domain ? (result?.nest?.sheets ?? 0) : 0,
      fullSheetCount: stock?.domain ? 0 : (result?.nest?.sheets ?? 0),
      areaIn2: result ? result.area / 25.4 ** 2 : 0,
      error: result?.error ?? null,
      stock: stock
        ? {
            units: 'in',
            width: mmToIn(stock.width),
            height: mmToIn(stock.height),
            margin: mmToIn(stock.margin),
            gap: mmToIn(stock.gap),
            grainAxis: stock.grainAxis ?? null,
            domain: stock.domain
              ? {
                  version: 1,
                  profile: stock.domain.profile,
                  outer: loopInches(stock.domain.outer),
                  holes: stock.domain.holes.map(loopInches),
                  sourceOriginIn: stock.domain.sourceOriginIn,
                }
              : null,
            exclusions:
              stock.exclusions?.map(e => ({ ...e, outline: loopInches(e.outline), clearance: mmToIn(e.clearance) })) ??
              [],
          }
        : null,
      placements:
        result?.nest?.placements.map(p => ({
          ...p,
          originalInstance: originalMap.get(p.partId)?.[p.instance] ?? p.instance,
          x: mmToIn(p.x),
          y: mmToIn(p.y),
          width: mmToIn(p.width),
          height: mmToIn(p.height),
        })) ?? [],
      unplaced: result?.nest?.unplaced ?? [],
      leftovers:
        result?.leftovers && stock && result.nest
          ? leftoversToFile(result.leftovers, { parts: checked.quote.parts, stock, nest: result.nest })
          : null,
      leftoverError: result?.leftoverError ?? null,
    };
  });
  const content = {
    version: 1,
    status: 'DRAFT_REVIEW',
    units: 'in',
    solverVersion: 'werco-contour-v7',
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    remnantDomainProfile: REMNANT_DOMAIN_PROFILE,
    identity,
    inputProject: input,
    inputSha256,
    evaluatedStages: frames.length,
    totalStages: plan.length,
    searchFinished: options.searchFinished === true,
    alternatives: outputs,
    review:
      'Alternatives are separate. One recorded piece may be used at most once per conditional alternative. Source availability and eligibility are unverified. No reservation, consumption, financial credit or manufacturing approval.',
    creditUSD: 0,
  };
  return { exportedAt: new Date().toISOString(), contentSha256: await sha256(canonicalJSON(content)), content };
}

/** Server report bytes retain their authoritative server hash. Before download,
 * validate the accepted staged prefix and its exact predecessor relationships. */
export async function validateRemnantReport(report: NestingRunReport): Promise<void> {
  if (report.run.settings.protocol !== 2) {
    if (Object.prototype.hasOwnProperty.call(report.estimate, 'remnantPlan'))
      throw new Error('A recorded-piece report requires staged protocol2.');
    return;
  }
  validateStagedRunIdentity(report.run);
  await validateRemnantFile(report.estimate, report.run.company_id);
  const plan = buildStagePlan(report.estimate);
  if (
    report.checkpoints.length > plan.length ||
    report.checkpoints.length !== report.run.evaluated_count ||
    report.checkpoints.length !== report.run.checkpoints.length
  )
    throw new Error('The report stage prefix is incomplete.');
  for (let i = 0; i < report.checkpoints.length; i++) {
    const checkpoint = report.checkpoints[i];
    if (checkpoint.sequence !== i + 1) throw new Error('The report stage order is inconsistent.');
    const predecessor = checkpoint.depends_on
      ? report.checkpoints.find(c => c.option_id === checkpoint.depends_on)
      : undefined;
    await validateSavedPlanningStage(report.run, report.estimate, checkpoint, predecessor);
  }
  const complete = report.checkpoints.filter(
    checkpoint => checkpoint.stage_kind !== 'recorded_piece' && checkpoint.complete
  ).length;
  if (report.run.completed_count !== complete)
    throw new Error('The report complete-alternative count is inconsistent.');
  if (report.run.summary) {
    const summary = report.run.summary;
    const expectedKeys = plan
      .slice(0, report.checkpoints.length)
      .map(stage => ({ stage_id: stage.stage_id, group_id: stage.group_id }));
    if (
      summary.protocol !== 2 ||
      summary.type !== 'summary' ||
      summary.input_sha256 !== report.run.input_sha256 ||
      summary.evaluated_count !== report.checkpoints.length ||
      summary.complete_option_count !== complete ||
      summary.total_options !== plan.length ||
      summary.stop_reason !== 'completed' ||
      canonicalJSON(summary.evaluated_keys) !== canonicalJSON(expectedKeys)
    )
      throw new Error('The report completion summary is inconsistent.');
  }
  if (report.run.status === 'COMPLETED' && report.checkpoints.length !== plan.length)
    throw new Error('The completed report is missing planned stages.');
}

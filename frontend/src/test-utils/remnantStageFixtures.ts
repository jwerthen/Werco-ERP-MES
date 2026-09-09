import { remnantPlanningFixture } from './remnantPlanningFixtures';
import { buildRemnantPlan, remnantEvidenceHash } from '../features/nesting/lib/remnant-evidence';
import { canonicalJSON, sha256 } from '../features/nesting/lib/provenance';
import { rect } from '../features/nesting/lib/nesting';
import { createBlankQuote, quoteToFile } from '../features/nesting/lib/quoting';
import {
  calculateRemnantProject,
  type RemnantStageMessage,
  type RemnantSummaryMessage,
} from '../features/nesting/lib/remnant-planning';
import { CURRENT_GEOMETRY_PROFILE } from '../features/nesting/lib/geometry-profile';
import { REMNANT_DOMAIN_PROFILE } from '../features/nesting/lib/remnant-domain-profile';
import { savedRunFixture } from './nestingRunFixtures';
import type { NestingRunCheckpoint, NestingRunDetail } from '../types/nestingRun';
export const jsonCopy = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export async function remnantStageInput(quantity = 2) {
  const f = await remnantPlanningFixture();
  f.snapshot.evidence.geometry = { kind: 'rectangle', width: '3', height: '3' };
  const bytes = canonicalJSON(f.snapshot.evidence);
  f.snapshot.payloadSha256 = await sha256(bytes);
  f.snapshot.payloadBytes = new TextEncoder().encode(bytes).length;
  f.resolution.snapshot_sha256 = await remnantEvidenceHash(f.snapshot);
  const quote = jsonCopy(
    quoteToFile({
      ...createBlankQuote(),
      margin: 3.175,
      gap: 3.175,
      parts: [{ id: 'plate', name: 'Synthetic plate', quantity, rotate: true, color: 0, loops: [rect(50.8, 50.8)] }],
      options: [{ id: 'sheet', width: 203.2, height: 203.2, enabled: true, price: null }],
    })
  );
  const remnantPlan = await buildRemnantPlan({
    resolution: f.resolution,
    companyId: 2,
    groupId: 'g1',
    quote,
    family: 'Carbon steel',
    requiredGrade: 'A36',
    reason: 'Plan the measured piece conditionally',
    zoneClearanceIn: '0.125',
  });
  return {
    version: 18,
    units: 'in',
    currency: 'USD',
    name: 'Synthetic staged nest',
    activeGroupId: 'g1',
    groups: [{ id: 'g1', quote }],
    remnantPlan,
  };
}
export async function remnantStageFixture(quantity = 2) {
  const raw = await remnantStageInput(quantity),
    digest = await sha256(canonicalJSON(raw));
  const stages: RemnantStageMessage[] = [];
  let summary: RemnantSummaryMessage | undefined;
  for await (const frame of calculateRemnantProject(raw, digest)) {
    if (frame.type === 'stage') stages.push(frame);
    else summary = frame;
  }
  const base = savedRunFixture();
  const checkpoints: NestingRunCheckpoint[] = stages.map(frame => ({
    schema_version: 1,
    sequence: frame.sequence,
    group_id: frame.group_id,
    option_id: frame.stage_id,
    stage_kind: frame.stage_kind,
    source_option_id: frame.option_id,
    depends_on: frame.depends_on,
    content_sha256: String(frame.sequence).repeat(64),
    payload_bytes: JSON.stringify(frame).length,
    created_at: '2026-09-09T12:00:00Z',
    complete: frame.stage_kind === 'residual' && frame.requested === 0 ? true : (frame.result?.complete ?? false),
    sheets: frame.result?.nest?.sheets ?? 0,
    placed: frame.result?.nest?.placements.length ?? 0,
    unplaced: frame.result?.nest ? frame.result.nest.unplaced.reduce((n, p) => n + p.count, 0) : frame.requested,
    result: frame,
  }));
  const detail: NestingRunDetail = {
    ...base.detail,
    company_id: 2,
    input_sha256: digest,
    solver_version: 'werco-contour-v7',
    evaluated_count: stages.length,
    completed_count: summary!.complete_option_count,
    settings: {
      ...base.detail.settings,
      protocol: 2,
      solver_version: 'werco-contour-v7',
      geometry_profile: CURRENT_GEOMETRY_PROFILE,
      remnant_domain_profile: REMNANT_DOMAIN_PROFILE,
      runtime: { ...(base.detail.settings.runtime as Record<string, unknown>), solver_version: 'werco-contour-v7' },
    },
    checkpoints: checkpoints.map(({ result: _result, schema_version: _schema, ...metadata }) => metadata),
    summary: summary!,
  };
  return {
    raw,
    digest,
    stages,
    summary: summary!,
    checkpoints,
    detail,
    source: { ...base.source, company_id: 2, content_sha256: digest, payload_schema_version: 18, estimate: raw },
  };
}

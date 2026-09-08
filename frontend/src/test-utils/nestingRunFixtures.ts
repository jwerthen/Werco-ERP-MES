import type { NestingDraftRevision } from '../types/nestingDraft';
import type { NestingRunDetail, NestingRunPage, NestingRuntime } from '../types/nestingRun';
import { rect } from '../features/nesting/lib/nesting';
import { createBlankProject, projectToFile } from '../features/nesting/lib/quote-project';
import { createBlankQuote } from '../features/nesting/lib/quoting';
import { calculateSavedProject } from '../features/nesting/lib/server-run';

export function savedRunFixture() {
  const estimate = projectToFile(
    createBlankProject({
      ...createBlankQuote(),
      name: 'Synthetic sheet estimate',
      margin: 2,
      gap: 1,
      parts: [
        {
          id: 'ring',
          name: 'Synthetic ring',
          quantity: 1,
          rotate: true,
          color: 0,
          loops: [
            { type: 'circle', cx: 15, cy: 15, r: 15 },
            { type: 'circle', cx: 15, cy: 15, r: 5 },
          ],
        },
        { id: 'plate', name: 'Synthetic plate', quantity: 1, rotate: false, color: 1, loops: [rect(12, 22)] },
      ],
      options: [{ id: 'sheet', enabled: true, price: null, width: 100, height: 100 }],
    })
  );
  const source: NestingDraftRevision = {
    schema_version: 1,
    draft_id: 41,
    company_id: 2,
    revision_number: 3,
    draft_version: 3,
    name: 'Synthetic sheet estimate',
    status: 'DRAFT',
    content_sha256: 'a'.repeat(64),
    payload_schema_version: estimate.version,
    payload_bytes: 2000,
    created_by: 7,
    created_at: '2026-09-08T18:00:00Z',
    review_issues: [],
    estimate,
  };
  const output = calculateSavedProject(estimate, source.content_sha256).next().value;
  if (!output || output.type !== 'option') throw new Error('Missing synthetic option');
  const detail: NestingRunDetail = {
    schema_version: 1,
    id: 11,
    company_id: 2,
    draft_id: 41,
    revision_id: 51,
    revision_number: 3,
    input_sha256: source.content_sha256,
    created_by: 7,
    status: 'COMPLETED',
    version: 5,
    cancel_requested: false,
    created_at: source.created_at,
    updated_at: source.created_at,
    started_at: source.created_at,
    finished_at: source.created_at,
    release_identity: 'synthetic-release',
    solver_version: 'werco-contour-v4',
    bundle_sha256: 'b'.repeat(64),
    node_version: 'v22.20.0',
    evaluated_count: 1,
    completed_count: 1,
    checkpoint_bytes: 2000,
    error_code: null,
    error_message: null,
    settings: { approved: false, remnant_credit_usd: 0 },
    summary: null,
    warnings: [{ code: 'unapproved', message: 'Server-calculated draft evidence only.' }],
    checkpoints: [
      {
        sequence: 1,
        group_id: output.group_id,
        option_id: output.option_id,
        content_sha256: 'c'.repeat(64),
        payload_bytes: 2000,
        created_at: source.created_at,
        complete: true,
        sheets: output.result.nest!.sheets,
        placed: 2,
        unplaced: 0,
      },
    ],
  };
  const checkpoint = { ...detail.checkpoints[0], result: output };
  return { source, detail, checkpoint };
}

export function runPage(items: NestingRunDetail[] = []): NestingRunPage {
  return { schema_version: 1, items, total: items.length, page: 1, per_page: 10 };
}

export const readyRuntime: NestingRuntime = {
  schema_version: 1,
  available: true,
  reason: 'ready',
  identity: {
    release: 'synthetic-release',
    protocol: 1,
    solver_version: 'werco-contour-v4',
    bundle_sha256: 'b'.repeat(64),
    node_version: 'v22.20.0',
    instance_id: '12345678-1234-4234-8234-123456789abc',
    observed_at: '2026-09-08T18:00:00Z',
  },
};

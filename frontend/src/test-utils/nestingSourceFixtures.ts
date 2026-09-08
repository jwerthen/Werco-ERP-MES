import type { NestingDraftRevision } from '../types/nestingDraft';
import type { NestingSourceIntent, NestingSourcePage, NestingSourceRequest } from '../types/nestingSource';
import { canonicalJSON, sha256, type PartProvenance } from '../features/nesting/lib/provenance';
import {
  savedBindingEvidence,
  savedSourceParts,
  sourceTargetKey,
  type SavedSourcePart,
} from '../features/nesting/cadSourceEvidence';

export function originalFile(
  text = '\uFEFF0\r\nSECTION\r\n2\r\nENTITIES\r\n999\r\nCafé\r\n0\r\nENDSEC\r\n0\r\nEOF\r\n',
  name = 'original.dxf'
) {
  const bytes = new TextEncoder().encode(text);
  const file = new File([bytes], name, { type: 'application/dxf' });
  Object.defineProperty(file, 'arrayBuffer', { value: jest.fn(async () => bytes.buffer.slice(0)) });
  return file;
}

export async function sourceFixture() {
  const file = originalFile();
  const sourceSha256 = await sha256(await file.arrayBuffer());
  const provenance: PartProvenance = {
    version: 1,
    sourceName: file.name,
    sourceSha256,
    sourceHashBasis: 'original-bytes',
    geometrySha256: 'b'.repeat(64),
    geometryVersion: 'werco-geometry-v1',
    sourceUnits: 'in',
    resolvedUnits: 'in',
    unitDecision: 'declared',
    importerVersion: 'werco-dxf-v2',
    warnings: [],
  };
  const revision: NestingDraftRevision = {
    schema_version: 1,
    company_id: 2,
    draft_id: 41,
    revision_number: 1,
    draft_version: 1,
    name: 'Saved nest',
    status: 'DRAFT',
    content_sha256: 'a'.repeat(64),
    payload_schema_version: 15,
    payload_bytes: 2000,
    created_by: 7,
    created_at: '2026-09-08T18:00:00Z',
    review_issues: [],
    estimate: {
      version: 15,
      groups: [
        {
          id: 'g1',
          quote: {
            name: 'Plate batch',
            material: 'Carbon steel',
            parts: [{ id: 'p1', name: 'Outer plate', revision: 'A', provenance }],
          },
        },
        {
          id: 'g2',
          quote: {
            name: 'Flange batch',
            material: 'Stainless steel',
            parts: [
              { id: 'p2', name: 'Second profile', provenance: { ...provenance, geometrySha256: 'c'.repeat(64) } },
            ],
          },
        },
      ],
    },
  };
  const parts = savedSourceParts(revision, revision);
  const request: NestingSourceRequest = {
    expected_company_id: 2,
    expected_input_sha256: revision.content_sha256,
    request_key: '11111111-1111-4111-8111-111111111111',
    source_sha256: sourceSha256,
    byte_count: file.size,
    source_name: file.name,
    mime_type: 'application/dxf',
    targets: parts.map(part => ({ group_id: part.group_id, part_id: part.part_id })),
  };
  return { file, revision, parts, request };
}

export async function sourceIntent(
  request: NestingSourceRequest,
  parts: SavedSourcePart[],
  attached = false
): Promise<NestingSourceIntent> {
  const targets = request.targets.map(target => ({
    ...target,
    provenance: savedBindingEvidence(parts.find(part => sourceTargetKey(part) === sourceTargetKey(target))!),
  }));
  return {
    id: 51,
    company_id: 2,
    draft_id: 41,
    revision_id: 61,
    revision_number: 1,
    input_sha256: request.expected_input_sha256,
    source_sha256: request.source_sha256,
    byte_count: request.byte_count,
    source_name: request.source_name,
    mime_type: request.mime_type,
    targets,
    targets_sha256: await sha256(canonicalJSON(targets)),
    target_count: targets.length,
    request_key: request.request_key,
    created_by: 7,
    submitted_api_token_id: null,
    created_at: '2026-09-08T18:05:00Z',
    state: attached ? 'ATTACHED' : 'PENDING',
    attempt_count: attached ? 1 : 0,
    can_resume: !attached,
    receipt: attached
      ? {
          id: 71,
          source_sha256: request.source_sha256,
          byte_count: request.byte_count,
          verified_at: '2026-09-08T18:06:00Z',
          created_by: 7,
          submitted_api_token_id: null,
          claim: 'server_hash_verified_unapproved',
        }
      : null,
  };
}

export const sourcePage = (items: NestingSourceIntent[] = [], canAttach = true): NestingSourcePage => ({
  company_id: 2,
  draft_id: 41,
  revision_number: 1,
  input_sha256: 'a'.repeat(64),
  can_attach: canAttach,
  items,
  total: items.length,
  page: 1,
  per_page: 10,
});

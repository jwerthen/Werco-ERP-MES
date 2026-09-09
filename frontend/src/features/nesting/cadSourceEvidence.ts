import { z } from 'zod';
import type { NestingDraftRevision, NestingDraftSummary } from '../../types/nestingDraft';
import type {
  NestingSourceIntent,
  NestingSourcePage,
  NestingSourceRequest,
  NestingSourceTarget,
} from '../../types/nestingSource';
import { canonicalJSON, sha256, validateProvenance, type PartProvenance } from './lib/provenance';

const id = z.number().int().positive().max(2147483647);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const timestamp = z.string().refine(value => Number.isFinite(Date.parse(value)), 'Invalid date');
const targetSchema = z
  .object({
    group_id: z.string().min(1).max(200),
    part_id: z.string().min(1).max(200),
    provenance: z.record(z.string(), z.unknown()),
  })
  .strict();
const receiptSchema = z
  .object({
    id,
    source_sha256: hash,
    byte_count: z.number().int().positive().max(4_999_999),
    verified_at: timestamp,
    created_by: id,
    submitted_api_token_id: id.nullable(),
    claim: z.literal('server_hash_verified_unapproved'),
  })
  .strict();
const intentSchema = z
  .object({
    id,
    company_id: id,
    draft_id: id,
    revision_id: id,
    revision_number: id,
    input_sha256: hash,
    source_sha256: hash,
    byte_count: z.number().int().positive().max(4_999_999),
    source_name: z.string().min(1).max(1024),
    mime_type: z.string().min(1).max(120),
    targets: z.array(targetSchema).min(1).max(1000),
    targets_sha256: hash,
    target_count: z.number().int().positive().max(1000),
    request_key: z.string().uuid(),
    created_by: id,
    submitted_api_token_id: id.nullable(),
    created_at: timestamp,
    state: z.enum(['PENDING', 'ATTACHED']),
    attempt_count: z.number().int().min(0).max(8),
    can_resume: z.boolean(),
    receipt: receiptSchema.nullable(),
  })
  .strict();
const pageSchema = z
  .object({
    company_id: id,
    draft_id: id,
    revision_number: id,
    input_sha256: hash,
    can_attach: z.boolean(),
    items: z.array(intentSchema).max(10),
    total: z.number().int().nonnegative(),
    page: id,
    per_page: id.max(10),
  })
  .strict();

export type SavedSourcePart = NestingSourceTarget & {
  name: string;
  groupName: string;
  revision?: string | null;
  provenance?: PartProvenance;
};
export const sourceTargetKey = (target: NestingSourceTarget) => JSON.stringify([target.group_id, target.part_id]);

/** Read the saved revision independently; never replace or serialize the working estimate. */
export function savedSourceParts(value: NestingDraftRevision, target: NestingDraftSummary): SavedSourcePart[] {
  if (
    value.schema_version !== 1 ||
    value.status !== 'DRAFT' ||
    value.company_id !== target.company_id ||
    value.draft_id !== target.draft_id ||
    value.revision_number !== target.revision_number ||
    value.content_sha256 !== target.content_sha256 ||
    value.draft_version !== target.revision_number
  )
    throw new Error('The saved source does not match this company and revision.');
  // Only inspect retained metadata. A source attachment neither recalculates
  // historical geometry nor rewrites its numbers through today's file reader.
  const project = z
    .object({
      groups: z
        .array(
          z
            .object({
              id: z.string().min(1),
              quote: z
                .object({
                  name: z.string(),
                  material: z.string(),
                  parts: z
                    .array(
                      z
                        .object({
                          id: z.string().min(1),
                          name: z.string(),
                          revision: z.string().nullable().optional(),
                          provenance: z.unknown().optional(),
                        })
                        .passthrough()
                    )
                    .max(1000),
                })
                .passthrough(),
            })
            .passthrough()
        )
        .min(1)
        .max(1000),
    })
    .passthrough()
    .parse(value.estimate);
  const parts = project.groups.flatMap(group =>
    group.quote.parts.map(part => {
      if (part.provenance !== undefined) validateProvenance(part.provenance);
      return {
        group_id: group.id,
        part_id: part.id,
        name: part.name,
        groupName: `${group.quote.material} · ${group.quote.name}`,
        ...(Object.prototype.hasOwnProperty.call(part, 'revision') ? { revision: part.revision } : {}),
        ...(part.provenance ? { provenance: part.provenance as PartProvenance } : {}),
      };
    })
  );
  if (parts.length > 1000 || new Set(parts.map(sourceTargetKey)).size !== parts.length)
    throw new Error('The saved profile identities are duplicated or exceed the attachment limit.');
  return parts;
}

export const savedBindingEvidence = (part: SavedSourcePart) => ({
  ...part.provenance,
  ...(Object.prototype.hasOwnProperty.call(part, 'revision') ? { reportedRevision: part.revision } : {}),
});

/** Bind every server target to the exact immutable input, and every receipt to its frozen intent. */
export async function checkSourceIntent(
  value: unknown,
  target: NestingDraftSummary,
  parts: SavedSourcePart[],
  request?: NestingSourceRequest,
  previous?: NestingSourceIntent
): Promise<NestingSourceIntent> {
  const intent = intentSchema.parse(value);
  if (
    intent.company_id !== target.company_id ||
    intent.draft_id !== target.draft_id ||
    intent.revision_number !== target.revision_number ||
    intent.input_sha256 !== target.content_sha256 ||
    intent.target_count !== intent.targets.length ||
    new Set(intent.targets.map(sourceTargetKey)).size !== intent.target_count
  )
    throw new Error('The attachment does not match this saved revision.');
  for (const evidence of intent.targets) {
    const part = parts.find(item => sourceTargetKey(item) === sourceTargetKey(evidence));
    if (
      !part?.provenance ||
      part.provenance.sourceHashBasis !== 'original-bytes' ||
      part.provenance.sourceSha256 !== intent.source_sha256 ||
      canonicalJSON(evidence.provenance) !== canonicalJSON(savedBindingEvidence(part))
    )
      throw new Error('Attachment profile evidence does not match the saved original-byte provenance.');
  }
  if ((await sha256(canonicalJSON(intent.targets))) !== intent.targets_sha256)
    throw new Error('The attachment target fingerprint does not match.');
  if (
    request &&
    (intent.request_key !== request.request_key ||
      intent.source_sha256 !== request.source_sha256 ||
      intent.byte_count !== request.byte_count ||
      intent.source_name !== request.source_name ||
      intent.mime_type !== request.mime_type ||
      canonicalJSON(intent.targets.map(sourceTargetKey).sort()) !==
        canonicalJSON(request.targets.map(sourceTargetKey).sort()))
  )
    throw new Error('The attachment receipt differs from the submitted request.');
  if (previous) {
    const immutable = (item: NestingSourceIntent) => {
      const mutable = new Set(['state', 'receipt', 'attempt_count', 'can_resume']);
      return Object.fromEntries(Object.entries(item).filter(([key]) => !mutable.has(key)));
    };
    if (
      canonicalJSON(immutable(intent)) !== canonicalJSON(immutable(previous)) ||
      intent.attempt_count < previous.attempt_count ||
      (previous.receipt && canonicalJSON(intent.receipt) !== canonicalJSON(previous.receipt))
    )
      throw new Error('The server changed an immutable attachment command or receipt.');
  }
  if (
    (intent.state === 'ATTACHED') !== (intent.receipt !== null) ||
    (intent.receipt &&
      (intent.receipt.source_sha256 !== intent.source_sha256 ||
        intent.receipt.byte_count !== intent.byte_count ||
        intent.receipt.created_by !== intent.created_by ||
        intent.receipt.submitted_api_token_id !== intent.submitted_api_token_id ||
        intent.attempt_count < 1))
  )
    throw new Error('The retained-byte receipt is inconsistent with this attachment.');
  return intent;
}

export async function checkSourcePage(
  value: unknown,
  target: NestingDraftSummary,
  parts: SavedSourcePart[]
): Promise<NestingSourcePage> {
  const page = pageSchema.parse(value);
  if (
    page.company_id !== target.company_id ||
    page.draft_id !== target.draft_id ||
    page.revision_number !== target.revision_number ||
    page.input_sha256 !== target.content_sha256 ||
    page.items.length > page.per_page ||
    page.total < page.items.length ||
    new Set(page.items.map(item => item.id)).size !== page.items.length
  )
    throw new Error('The attachment list belongs to a different saved revision or is malformed.');
  const items: NestingSourceIntent[] = [];
  for (const item of page.items) items.push(await checkSourceIntent(item, target, parts));
  return { ...page, items };
}

export async function fingerprintOriginal(file: File): Promise<string> {
  if (
    !/\.dxf$/i.test(file.name) ||
    file.name !== file.name.trim() ||
    /[\\/\x00-\x1f]/.test(file.name) ||
    file.name.length > 1024
  )
    throw new Error('Choose an original .dxf file with a filename and no path.');
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size >= 5_000_000)
    throw new Error('Each original must contain bytes and be smaller than 5 MB.');
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength !== file.size) throw new Error('The original file length changed. Select it again.');
  return sha256(bytes);
}

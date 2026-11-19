/**
 * Restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2).
 *
 * Every controlled document carries an `access_level`, and the server applies ONE rule to
 * every read path (`backend/app/services/document_access.py`):
 *   - `standard` — anyone who can read documents.
 *   - `restricted` — Admin, Manager, Quality and Supervisor (plus superuser / Platform Admin).
 *   - `export_controlled` — ONLY a person an Admin has marked export-authorized. No role
 *     bypass: not Admin, not superuser, not Platform Admin.
 *
 * A document the caller may not view is a 404 on every route (list, get, revisions, download,
 * kiosk viewer, nest PDF, QMS link, ...), never a 403, so the UI cannot tell "removed" from
 * "not yours to see" and must not try to: it says "not available".
 *
 * The UI never decides who can see what. These labels and the rank below are display aids;
 * the server is the enforcement.
 */

export type DocumentAccessLevel = 'standard' | 'restricted' | 'export_controlled';

/** In ascending order of restriction. */
export const DOCUMENT_ACCESS_LEVELS: readonly DocumentAccessLevel[] = ['standard', 'restricted', 'export_controlled'];

export const DOCUMENT_ACCESS_LABELS: Readonly<Record<DocumentAccessLevel, string>> = {
  standard: 'Standard',
  restricted: 'Restricted',
  export_controlled: 'Export-controlled',
};

/** Who can open a document at each level, worded for the people choosing one. */
export const DOCUMENT_ACCESS_DESCRIPTIONS: Readonly<Record<DocumentAccessLevel, string>> = {
  standard: 'Anyone who can read documents.',
  restricted: 'Admin, Manager, Quality and Supervisor only.',
  export_controlled: 'Only people an Admin has marked export-authorized. No role overrides this, Admin included.',
};

export function isDocumentAccessLevel(value: unknown): value is DocumentAccessLevel {
  return value === 'standard' || value === 'restricted' || value === 'export_controlled';
}

/**
 * The level a document record carries. A record without one (an API that predates the column:
 * the SPA and the API deploy separately) is `standard`, which is what every row was before
 * migration 131.
 */
export function documentAccessLevel(record: { access_level?: unknown } | null | undefined): DocumentAccessLevel {
  const level = record?.access_level;
  return isDocumentAccessLevel(level) ? level : 'standard';
}

export function documentAccessLabel(level: unknown): string {
  return isDocumentAccessLevel(level) ? DOCUMENT_ACCESS_LABELS[level] : DOCUMENT_ACCESS_LABELS.standard;
}

function rank(level: DocumentAccessLevel): number {
  return DOCUMENT_ACCESS_LEVELS.indexOf(level);
}

/**
 * Did the upload come back at a higher level than the one asked for? The server raises (never
 * lowers) the level when the linked part's customer is export-controlled, and a revision keeps
 * at least its predecessor's level. Read from the explicit `access_level_raised` flag when the
 * response carries one, and from the saved level otherwise, so the notice never depends on the
 * flag's presence.
 */
export function serverRaisedAccessLevel(requested: DocumentAccessLevel, saved: DocumentUploadResult): boolean {
  if (saved.access_level_raised === true) return true;
  return rank(documentAccessLevel(saved)) > rank(requested);
}

/** The `DocumentResponse` fields the access UI reads. */
export interface DocumentAccessFields {
  id: number;
  document_number: string;
  revision: string;
  title: string;
  /** Optional only so a response from an API without migration 131 still reads as `standard`. */
  access_level?: DocumentAccessLevel;
}

/** `POST /documents/upload` response (`DocumentUploadResponse`): the saved document, plus what was asked. */
export interface DocumentUploadResult extends DocumentAccessFields {
  /** The level the form asked for. */
  access_level_requested?: DocumentAccessLevel;
  /** True when the server saved the document above `access_level_requested`. */
  access_level_raised?: boolean;
  /** Why: `export_controlled_customer` and / or `predecessor_revision`. */
  access_level_raised_because?: string[];
  [key: string]: unknown;
}

const RAISED_BECAUSE_TEXT: Readonly<Record<string, string>> = {
  export_controlled_customer: "the linked part's customer is export-controlled",
  predecessor_revision: 'the previous revision is already at that level',
};

/**
 * The upload notice, e.g. "Document uploaded as Export-controlled, not Standard: the linked part's
 * customer is export-controlled, so the server raised it." Reasons come from `access_level_raised_because`; an unknown or missing one
 * reads as the server having raised it, never as a guess at why.
 */
export function accessLevelRaisedNotice(subject: string, requested: DocumentAccessLevel, saved: DocumentUploadResult) {
  const reasons = (Array.isArray(saved.access_level_raised_because) ? saved.access_level_raised_because : [])
    .map(reason => RAISED_BECAUSE_TEXT[reason])
    .filter((text): text is string => !!text);
  const why = reasons.length
    ? `${reasons.join(', and ')}, so the server raised it`
    : 'the server raised the access level';
  return `${subject} as ${DOCUMENT_ACCESS_LABELS[documentAccessLevel(saved)]}, not ${DOCUMENT_ACCESS_LABELS[requested]}: ${why}.`;
}

/** `POST /documents/{id}/access-level` body. Admin / Quality; never an API token. */
export interface DocumentAccessChangeRequest {
  access_level: DocumentAccessLevel;
  /** Required. Recorded on the audit row with the old and new level. */
  reason: string;
}

/**
 * `POST /documents/{id}/access-level` response: the document after the change. The change applies
 * to the document's WHOLE revision chain, so a level never differs between revisions; lowering is
 * refused 409 when some revision is not visible to the caller.
 */
export interface DocumentAccessChangeResult extends DocumentAccessFields {
  previous_access_level?: DocumentAccessLevel;
  /**
   * Active export-authorized people in the company. 0 means nobody can open an
   * export-controlled document yet.
   */
  authorized_user_count: number;
  /** Every revision whose level this change moved (this one included). */
  changed_document_ids?: number[];
  /** Revisions in the chain. */
  chain_size?: number;
  [key: string]: unknown;
}

/** Ids of the revisions a change moved: the server's list, or just the document itself. */
export function changedRevisionIds(result: DocumentAccessChangeResult): number[] {
  const ids = Array.isArray(result.changed_document_ids)
    ? result.changed_document_ids.filter((id): id is number => typeof id === 'number' && Number.isFinite(id))
    : [];
  return ids.length ? ids : [result.id];
}

const revisions = (count: number) => `${count} revision${count === 1 ? '' : 's'}`;

/**
 * What a change did, in words: "DWG-12 is now Restricted on all 3 revisions.", or "…: 2 of 3
 * revisions changed." when some were already there. Counts come only from the server's
 * `changed_document_ids` / `chain_size`; a response without them names just the revision.
 */
export function accessChangeSummary(document: DocumentAccessFields, result: DocumentAccessChangeResult): string {
  const level = DOCUMENT_ACCESS_LABELS[documentAccessLevel(result)];
  const changed = Array.isArray(result.changed_document_ids) ? changedRevisionIds(result).length : null;
  const chain =
    typeof result.chain_size === 'number' && Number.isFinite(result.chain_size) && result.chain_size > 0
      ? result.chain_size
      : null;
  if (changed === null && chain === null) {
    return `${document.document_number} Rev ${document.revision} is now ${level}.`;
  }
  const total = chain ?? changed ?? 1;
  if (changed !== null && changed < total) {
    return `${document.document_number} is now ${level}: ${changed} of ${revisions(total)} changed.`;
  }
  return total === 1
    ? `${document.document_number} is now ${level} (1 revision).`
    : `${document.document_number} is now ${level} on all ${revisions(total)}.`;
}

/**
 * One row of `GET /documents/access-review` (Admin / Quality): a document whose linked part's
 * customer is export-controlled, but whose own level is not `export_controlled`. `id` is the
 * wire's `document_id`.
 */
export interface DocumentAccessReviewRow {
  id: number;
  document_number: string;
  revision?: string | null;
  title: string;
  part_id?: number | null;
  part_number?: string | null;
  customer_id?: number | null;
  customer_name?: string | null;
  /** The customer's control, e.g. `itar_export_controlled` or `cui`. */
  classification?: string | null;
  access_level: DocumentAccessLevel;
}

export interface DocumentAccessReview {
  documents: DocumentAccessReviewRow[];
  /** The wire's `unlinked_document_count`: part-less documents the review cannot judge. */
  unlinked_count: number;
}

type Loose = Record<string, unknown>;
const asRecord = (value: unknown): Loose => (value && typeof value === 'object' ? (value as Loose) : {});
const asText = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);
const asId = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);

const asCount = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : null;

/**
 * Read a `GET /documents/access-review` body (`DocumentAccessReviewResponse`:
 * `{ documents: [{ document_id, ... }], document_count, unlinked_document_count }`) into
 * `DocumentAccessReview`. An `id` / `unlinked_count` spelling and a nested `part` / `customer`
 * object are read too, and a missing count is 0. Rows without a numeric id are dropped: every
 * row must open its document.
 */
export function normalizeDocumentAccessReview(body: unknown): DocumentAccessReview {
  const root = asRecord(body);
  const rawRows: unknown[] = Array.isArray(body) ? body : Array.isArray(root.documents) ? root.documents : [];
  const documents: DocumentAccessReviewRow[] = [];
  for (const raw of rawRows) {
    const row = asRecord(raw);
    const id = asId(row.document_id) ?? asId(row.id);
    if (id === null) continue;
    const part = asRecord(row.part);
    const customer = asRecord(row.customer);
    documents.push({
      id,
      document_number: asText(row.document_number) ?? `#${id}`,
      revision: asText(row.revision),
      title: asText(row.title) ?? '',
      part_id: asId(row.part_id) ?? asId(part.id),
      part_number: asText(row.part_number) ?? asText(part.part_number),
      customer_id: asId(row.customer_id) ?? asId(customer.id),
      customer_name: asText(row.customer_name) ?? asText(customer.name),
      classification: asText(row.classification),
      access_level: documentAccessLevel(row),
    });
  }
  return {
    documents,
    unlinked_count: asCount(root.unlinked_document_count) ?? asCount(root.unlinked_count) ?? 0,
  };
}

/** `PUT /users/{id}/export-authorization` body. Admin only; never an API token. */
export interface UserExportAuthorizationRequest {
  export_authorized: boolean;
  /** Required. Recorded on the audit row. */
  reason: string;
}

export type NestingDraftIssue = { code: string; message: string; group_id?: string | null };

/** A saved input snapshot. This status never represents geometry or quote approval. */
export type NestingDraftSummary = {
  draft_id: number;
  company_id: number;
  revision_number: number;
  draft_version: number;
  name: string;
  status: 'DRAFT';
  content_sha256: string;
  payload_schema_version: number;
  payload_bytes: number;
  created_by: number;
  created_at: string;
  review_issues: NestingDraftIssue[];
};

export type NestingDraftRevision = NestingDraftSummary & {
  schema_version: 1;
  estimate: Record<string, unknown>;
};

export type NestingDraftPage = {
  schema_version: 1;
  items: NestingDraftSummary[];
  total: number;
  page: number;
  per_page: number;
};

export type NestingDraftSave = {
  companyId: number;
  requestKey: string;
  estimateJson: string;
  target?: { draftId: number; expectedVersion: number };
};

export type NestingSourceTarget = { group_id: string; part_id: string };
export type NestingSourceEvidence = NestingSourceTarget & { provenance: Record<string, unknown> };

export type NestingSourceRequest = {
  expected_company_id: number;
  expected_input_sha256: string;
  request_key: string;
  source_sha256: string;
  byte_count: number;
  source_name: string;
  mime_type: string;
  targets: NestingSourceTarget[];
};

export type NestingSourceReceipt = {
  id: number;
  source_sha256: string;
  byte_count: number;
  verified_at: string;
  created_by: number;
  submitted_api_token_id: number | null;
  claim: 'server_hash_verified_unapproved';
};

/** ATTACHED records verification at completion, not current storage availability or CAD approval. */
export type NestingSourceIntent = {
  id: number;
  company_id: number;
  draft_id: number;
  revision_id: number;
  revision_number: number;
  input_sha256: string;
  source_sha256: string;
  byte_count: number;
  source_name: string;
  mime_type: string;
  targets: NestingSourceEvidence[];
  targets_sha256: string;
  target_count: number;
  request_key: string;
  created_by: number;
  submitted_api_token_id: number | null;
  created_at: string;
  state: 'PENDING' | 'ATTACHED';
  attempt_count: number;
  can_resume: boolean;
  receipt: NestingSourceReceipt | null;
};

export type NestingSourcePage = {
  company_id: number;
  draft_id: number;
  revision_number: number;
  input_sha256: string;
  can_attach: boolean;
  items: NestingSourceIntent[];
  total: number;
  page: number;
  per_page: number;
};

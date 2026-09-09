import type {
  SpacingPolicyContent,
  SpacingPolicySnapshot,
  PolicyMaterial,
} from '../features/nesting/lib/spacing-policy';

export type NestingPolicyRevision = {
  id: number;
  company_id: number;
  policy_id: number;
  revision_number: number;
  name: string;
  content_sha256: string;
  payload_schema_version: number;
  payload_bytes: number;
  created_by: number;
  created_at: string;
};
export type NestingPolicyPublication = {
  id: number;
  company_id: number;
  policy_id: number;
  policy_version: number;
  revision_id: number;
  revision_number: number;
  content_sha256: string;
  effective_at: string;
  created_by: number;
  created_at: string;
  reason: string;
  status: 'scheduled' | 'current' | 'superseded' | 'withdrawn';
  withdrawal: null | { id: number; created_by: number; created_at: string; reason: string };
};
export type NestingPolicyState = {
  schema_version: 1;
  policy: null | {
    id: number;
    company_id: number;
    version: number;
    latest_revision_number: number;
    created_by: number;
    created_at: string;
    updated_at: string;
  };
  current_publication: NestingPolicyPublication | null;
  revisions: NestingPolicyRevision[];
  publications: NestingPolicyPublication[];
  total_revisions: number;
  total_publications: number;
  page: number;
  per_page: number;
};
export type NestingPolicyCommandBase = {
  expected_company_id: number;
  expected_version: number;
  request_key: string;
  reason: string;
};
export type NestingPolicyRevisionRequest = NestingPolicyCommandBase & { content: SpacingPolicyContent };
export type NestingPolicyPublishRequest = NestingPolicyCommandBase & {
  revision_number: number;
  content_sha256: string;
  effective_at: string | null;
};
export type NestingPolicyReceipt = {
  schema_version: 1;
  policy_version: number;
  event_id: number;
  revision: NestingPolicyRevision;
  publication: NestingPolicyPublication | null;
};
export type NestingPolicyResolution = {
  schema_version: 1;
  status: 'resolved' | 'unmatched' | 'unavailable';
  policy: SpacingPolicySnapshot | null;
  explanation: string;
};
export type NestingPolicyResolveRequest = { material: PolicyMaterial; thickness_in: string };

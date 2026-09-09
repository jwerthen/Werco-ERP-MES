import type { NestingPolicyReceipt, NestingPolicyState } from '../types/nestingPolicy';
import type { SpacingPolicyContent, SpacingPolicySnapshot } from '../features/nesting/lib/spacing-policy';

export const policyContent: SpacingPolicyContent = {
  schema_version: 1,
  units: 'in',
  name: 'Synthetic reviewed allowances',
  bands: [
    {
      id: 'carbon-thin',
      material: 'Carbon steel',
      thickness_min_in: '0',
      thickness_max_in: '0.25',
      minimum_gap_in: '0.125',
      gap_thickness_multiplier: '1',
      minimum_margin_in: '0.375',
      margin_thickness_multiplier: '2',
    },
  ],
};
export const policyState: NestingPolicyState = {
  schema_version: 1,
  policy: {
    id: 7,
    company_id: 2,
    version: 1,
    latest_revision_number: 1,
    created_by: 9,
    created_at: '2026-09-08T12:00:00Z',
    updated_at: '2026-09-08T12:00:00Z',
  },
  current_publication: null,
  publications: [],
  revisions: [
    {
      id: 31,
      company_id: 2,
      policy_id: 7,
      revision_number: 1,
      name: policyContent.name,
      content_sha256: '72355108fb529e3d95c83b4ae4095c5f6ff08b1c41ef4d1888c7f5c6fb305b2f',
      payload_schema_version: 1,
      payload_bytes: 300,
      created_by: 9,
      created_at: '2026-09-08T12:00:00Z',
    },
  ],
  total_revisions: 1,
  total_publications: 0,
  page: 1,
  per_page: 20,
};
export const policyReceipt: NestingPolicyReceipt = {
  schema_version: 1,
  policy_version: 2,
  event_id: 62,
  revision: policyState.revisions[0],
  publication: {
    id: 62,
    company_id: 2,
    policy_id: 7,
    policy_version: 2,
    revision_id: 31,
    revision_number: 1,
    content_sha256: '72355108fb529e3d95c83b4ae4095c5f6ff08b1c41ef4d1888c7f5c6fb305b2f',
    effective_at: '2026-09-08T12:10:00Z',
    created_by: 9,
    created_at: '2026-09-08T12:10:00Z',
    reason: 'Synthetic reviewed allowances',
    status: 'current',
    withdrawal: null,
  },
};
export const policySnapshot: SpacingPolicySnapshot = {
  schema_version: 1,
  company_id: 2,
  policy_id: 7,
  publication_id: 62,
  revision_id: 31,
  revision_number: 1,
  content_sha256: '72355108fb529e3d95c83b4ae4095c5f6ff08b1c41ef4d1888c7f5c6fb305b2f',
  band: policyContent.bands[0],
  thickness_in: '0.125',
  gap_in: '0.125',
  margin_in: '0.375',
  resolved_at: '2026-09-08T13:00:00Z',
};

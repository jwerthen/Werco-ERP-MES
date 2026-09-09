import type { StockPieceEvidence } from './stockPiece';
import type { GeometryProfileRef } from '../features/nesting/lib/geometry-profile';

export const REMNANT_PLANNING_ADVISORY = 'Recorded piece — availability and eligibility unverified.';
export type RemnantFamily = 'Carbon steel' | 'Stainless steel' | 'Aluminum';
export type RemnantSourceEvidence = {
  version: 1;
  item: {
    id: number;
    company_id: number;
    part_id: number;
    location: string;
    warehouse: string | null;
    lot_number: string | null;
    serial_number: string | null;
    received_date: string | null;
    supplier_id: number | null;
    po_number: string | null;
    cert_number: string | null;
    heat_lot: string | null;
    expiration_date: string | null;
    status: 'available';
    is_active: true;
    updated_at: string | null;
  };
  part: {
    id: number;
    company_id: number;
    part_number: string;
    revision: string | null;
    name: string;
    part_type: string | null;
    unit_of_measure: string | null;
    is_active: true;
    is_deleted: false;
    updated_at: string | null;
  };
  movement_watermark: {
    coverage: 'direct_item_and_unattributed_same_part';
    count: number;
    max_id: number | null;
    max_created_at: string | null;
  };
};
export type RemnantSnapshot = {
  version: 1;
  companyId: number;
  pieceId: number;
  label: string;
  observationNumber: number;
  state: 'RECORDED';
  observedAt: string;
  observerName: string;
  reason: string;
  createdAt: string;
  createdBy: number;
  submittedApiTokenId: number | null;
  payloadSchemaVersion: 1;
  payloadSha256: string;
  payloadBytes: number;
  evidence: StockPieceEvidence;
  sourceInventoryItemId: number;
  sourcePartId: number;
  sourceSha256: string;
  sourceEvidence: RemnantSourceEvidence;
};
export type RemnantSnapshotRequest = {
  expected_company_id: number;
  expected_payload_sha256: string;
  expected_source_sha256: string;
};
export type RemnantResolution = {
  company_id: number;
  snapshot: RemnantSnapshot;
  snapshot_sha256: string;
  latest_observation_number: number;
  source_status: 'unchanged';
  current_source_sha256: string;
  checked_at: string;
  review_issues: string[];
  advisory: typeof REMNANT_PLANNING_ADVISORY;
};
export type RemnantAssignment = {
  version: 1;
  basis: 'planner_declared_unverified';
  family: RemnantFamily;
  requiredGrade: string;
  thicknessIn: string;
  reason: string;
  targetGroupSha256: string;
};
export type RemnantPlan = {
  version: 1;
  groupId: string;
  snapshot: RemnantSnapshot;
  snapshotSha256: string;
  assignment: RemnantAssignment;
  geometryProfile: GeometryProfileRef;
  zoneClearanceIn: string;
  capacity: 1;
  planningOnly: true;
  eligibilityVerified: false;
  availabilityVerified: false;
};

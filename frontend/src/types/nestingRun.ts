import type { RemnantStageMessage } from '../features/nesting/lib/remnant-planning';
import type { ServerOptionMessage } from '../features/nesting/lib/server-run';
export type NestingRuntime = {
  schema_version: 1;
  available: boolean;
  reason: 'ready' | 'missing' | 'stale' | 'release_mismatch' | 'queue_unavailable' | 'invalid_identity';
  identity: null | {
    release: string;
    protocol: number;
    solver_version: string;
    bundle_sha256: string;
    node_version: string;
    instance_id: string;
    deployment_id?: string | null;
    observed_at: string;
  };
};
export type NestingRunStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'PARTIAL' | 'CANCELLED' | 'FAILED';
export type NestingRunSummary = {
  id: number;
  company_id: number;
  draft_id: number;
  revision_id: number;
  revision_number: number;
  input_sha256: string;
  created_by: number;
  status: NestingRunStatus;
  version: number;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  release_identity: string | null;
  solver_version: string | null;
  bundle_sha256: string | null;
  node_version: string | null;
  evaluated_count: number;
  completed_count: number;
  checkpoint_bytes: number;
  error_code: string | null;
  error_message: string | null;
};
export type NestingRunCheckpointSummary = {
  sequence: number;
  group_id: string;
  option_id: string;
  content_sha256: string;
  payload_bytes: number;
  created_at: string;
  stage_kind?: 'baseline' | 'recorded_piece' | 'residual';
  source_option_id?: string | null;
  depends_on?: string | null;
  complete: boolean;
  sheets: number | null;
  placed: number;
  unplaced: number;
};
export type NestingRunDetail = NestingRunSummary & {
  schema_version: 1;
  settings: Record<string, unknown>;
  summary: Record<string, unknown> | null;
  warnings: { code: string; message: string }[];
  checkpoints: NestingRunCheckpointSummary[];
};
export type NestingRunPage = {
  schema_version: 1;
  items: NestingRunSummary[];
  page: number;
  per_page: number;
  total: number;
};
export type NestingRunRequest = {
  draft_id: number;
  revision_number: number;
  input_sha256: string;
  expected_company_id: number;
  request_key: string;
};
export type NestingRunCheckpoint = NestingRunCheckpointSummary & {
  schema_version?: 1;
  result: ServerOptionMessage | RemnantStageMessage;
};
export type NestingRunReport = {
  schema_version: 1;
  status: 'UNAPPROVED';
  run: NestingRunDetail;
  estimate: Record<string, unknown>;
  checkpoints: NestingRunCheckpoint[];
  content_sha256: string;
};

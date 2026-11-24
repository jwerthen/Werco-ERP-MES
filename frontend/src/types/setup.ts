/**
 * Types for `GET /setup/health` (the Setup Wizard, the Action Inbox and the
 * dashboard setup nudge). Mirrors `SetupHealthResponse` in
 * `backend/app/api/endpoints/setup.py` and `StorageStatus` in
 * `backend/app/schemas/storage.py`.
 */

export interface SetupStep {
  key: string;
  label: string;
  status: 'complete' | 'missing';
  count: number;
  required_count: number;
  href: string;
  reason?: string | null;
}

export interface MasterDataIssue {
  key: string;
  severity: 'high' | 'medium' | 'low';
  title: string;
  detail: string;
  count: number;
  href: string;
}

/** `local` = API process disk; `s3` = S3-compatible object storage. */
export type StorageBackend = 'local' | 's3';

/**
 * `durable` = object storage; `volume` = local disk on what looks like a mounted
 * volume; `ephemeral` = local disk with no mount found (lost on redeploy);
 * `unknown` = local disk whose mount state could not be read.
 */
export type StorageDurability = 'durable' | 'volume' | 'ephemeral' | 'unknown';

/**
 * Where the API process writes NEW uploaded document bytes. Configuration only:
 * no credentials, no full URL, no filesystem path, and no reachability probe.
 */
export interface StorageStatus {
  backend: StorageBackend;
  durability: StorageDurability;
  /** s3 only. */
  bucket: string | null;
  /** s3 only. Hostname of S3_ENDPOINT_URL; null = the AWS default endpoint for `region`. */
  endpoint_host: string | null;
  /** s3 only. */
  region: string | null;
  /** local only. True when UPLOAD_DIR was set explicitly. */
  upload_dir_configured: boolean | null;
  /** local only. */
  upload_dir_exists: boolean | null;
  /** local only. Heuristic; null when the mount state could not be read. */
  upload_dir_on_mounted_volume: boolean | null;
  /** Fixed server text when stored documents may not survive a redeploy, else null. */
  warning: string | null;
}

export interface SetupHealthResponse {
  progress: number;
  counts: Record<string, number>;
  steps: SetupStep[];
  issues: MasterDataIssue[];
  /**
   * Absent from an API that predates the field (the SPA and API deploy separately);
   * null when the server could not read the storage configuration.
   */
  storage?: StorageStatus | null;
}

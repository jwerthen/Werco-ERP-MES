import React from 'react';
import { CircleStackIcon } from '@heroicons/react/24/outline';
import { StatusBadge } from '../ui';
import { variantClass } from '../../utils/statusColors';
import type { StorageDurability, StorageStatus } from '../../types/setup';

/** Durability → badge color: object storage and a mounted volume are fine, no volume is not. */
const DURABILITY_COLORS: Record<StorageDurability, string> = {
  durable: variantClass.green,
  volume: variantClass.blue,
  ephemeral: variantClass.red,
  unknown: variantClass.amber,
};

interface StorageStatusPanelProps {
  /**
   * `undefined` = the API predates the field; `null` = the server could not read its
   * storage configuration; otherwise the configuration summary.
   */
  storage: StorageStatus | null | undefined;
  /** The last refresh failed, so what is shown may be out of date. */
  stale?: boolean;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-baseline sm:gap-3 py-1.5 border-b border-slate-800 last:border-b-0">
      <dt className="text-xs uppercase tracking-wide text-slate-500 sm:w-40 shrink-0">{label}</dt>
      <dd className="text-sm text-slate-200 break-all">{value}</dd>
    </div>
  );
}

function yesNo(value: boolean | null, yes: string, no: string, unknown = 'Could not be read'): string {
  if (value === null) return unknown;
  return value ? yes : no;
}

function objectStorageTarget(storage: StorageStatus): string {
  const where = storage.endpoint_host ?? `AWS S3${storage.region ? ` (${storage.region})` : ''}`;
  return storage.bucket ? `${storage.bucket} @ ${where}` : where;
}

/**
 * Read-only summary of where the API writes uploaded document bytes (drawings, certs,
 * process-sheet attachments). Comes from `GET /setup/health` → `storage`. A database
 * restore never brings these files back, so the owner needs to see which backend holds them.
 */
export function StorageStatusPanel({ storage, stale = false }: StorageStatusPanelProps) {
  let body: React.ReactNode;

  if (stale) {
    body = <p className="text-amber-200">Current storage configuration is unknown. Retry to verify it.</p>;
  } else if (storage === undefined) {
    body = <p className="text-slate-400">This API version does not report its storage configuration.</p>;
  } else if (storage === null) {
    body = <p className="text-amber-200">Storage configuration unavailable.</p>;
  } else {
    const isS3 = storage.backend === 's3';
    body = (
      <>
        <div className="flex items-center gap-3 mb-3">
          <span className="font-semibold text-white">{isS3 ? 'Object storage' : 'Local disk'}</span>
          <StatusBadge status={storage.durability} colorMap={DURABILITY_COLORS} />
        </div>
        <dl>
          {isS3 ? (
            <>
              <Row label="Bucket" value={objectStorageTarget(storage)} />
              {storage.endpoint_host && storage.region && <Row label="Region" value={storage.region} />}
            </>
          ) : (
            <>
              <Row
                label="Volume"
                value={yesNo(storage.upload_dir_on_mounted_volume, 'Mounted volume found', 'No mounted volume found')}
              />
              <Row
                label="Upload folder"
                value={`${yesNo(storage.upload_dir_configured, 'Set explicitly', 'Default location')}, ${yesNo(
                  storage.upload_dir_exists,
                  'exists',
                  'does not exist yet'
                ).toLowerCase()}`}
              />
            </>
          )}
        </dl>
        {storage.warning && (
          <p
            className={`mt-3 border px-3 py-2 text-sm ${
              storage.durability === 'ephemeral'
                ? 'border-red-500/50 bg-red-500/10 text-red-200'
                : 'border-amber-500/50 bg-amber-500/10 text-amber-200'
            }`}
          >
            {storage.warning}
          </p>
        )}
        <p className="mt-3 text-xs text-slate-500">
          Describes the API service only; the background worker reads its own settings. A database restore does not
          bring uploaded files back.
        </p>
      </>
    );
  }

  return (
    <section aria-labelledby="storage-status-heading" className="bg-fd-panel border border-slate-700 rounded-lg p-5">
      <div className="flex items-center gap-2 mb-4">
        <CircleStackIcon className="h-5 w-5 text-cyan-300" aria-hidden="true" />
        <h2 id="storage-status-heading" className="text-lg font-semibold text-white">
          Document Storage
        </h2>
      </div>
      {body}
    </section>
  );
}

export default StorageStatusPanel;

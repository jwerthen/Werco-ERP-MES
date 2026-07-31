import React, { useState } from 'react';
import { DocumentMagnifyingGlassIcon, FireIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { LaserNestInfo } from '../../types';
import LaserNestPdfPreview from './LaserNestPdfPreview';

interface LaserNestOperatorPanelProps {
  nest: LaserNestInfo;
  /** Larger type + taller preview for the touch-first kiosk surfaces. */
  size?: 'compact' | 'kiosk';
  /** Allow expanding the inline PDF preview (off inside the small queue card). */
  allowPreview?: boolean;
  /**
   * Optional transport override for the nest PDF, threaded to
   * LaserNestPdfPreview's `fetchBlob`. Kiosk surfaces (crew station) inject a
   * fence-safe fetcher here (badge-token shop-floor inline route) so the
   * preview never drives the global axios client. Omit on desktop callers —
   * behavior is unchanged (api.fetchLaserNestDocument).
   */
  fetchNestPdf?: () => Promise<string>;
}

/**
 * Operator-facing nest summary: CNC number prominent, runs progress, material —
 * plus an optional inline PDF preview the operator can expand to visually
 * confirm the nest layout before cutting. Used on the kiosk queue card, the
 * clock-in confirm screen, the active-job banner, and the browser shop floor.
 */
export default function LaserNestOperatorPanel({
  nest,
  size = 'compact',
  allowPreview = true,
  fetchNestPdf,
}: LaserNestOperatorPanelProps) {
  const [showPreview, setShowPreview] = useState(false);
  const kiosk = size === 'kiosk';
  const canPreview = allowPreview && Boolean(nest.has_document);

  return (
    <div
      data-testid="laser-nest-operator-panel"
      className="rounded border border-fd-red/40 bg-fd-red/5 p-3 text-left"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <FireIcon className={`shrink-0 text-fd-red ${kiosk ? 'h-6 w-6' : 'h-5 w-5'}`} />
          <div className="min-w-0">
            <div className={`font-mono font-bold text-fd-ink ${kiosk ? 'text-2xl' : 'text-lg'}`}>
              {nest.cnc_number ? `CNC# ${nest.cnc_number}` : nest.nest_name}
            </div>
            {nest.cnc_number && nest.nest_name && nest.nest_name !== nest.cnc_number && (
              <div className={`truncate text-fd-mute ${kiosk ? 'text-base' : 'text-sm'}`}>{nest.nest_name}</div>
            )}
          </div>
        </div>
        <div className={`font-mono font-bold text-fd-ink ${kiosk ? 'text-xl' : 'text-base'}`}>
          {Number(nest.completed_runs)}
          <span className="text-fd-faint"> / </span>
          {Number(nest.planned_runs)}
          <span className={`ml-1 uppercase tracking-widest text-fd-faint ${kiosk ? 'text-sm' : 'text-xs'}`}>runs</span>
        </div>
      </div>

      {(nest.material || nest.thickness || nest.sheet_size) && (
        <div className={`mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-fd-mute ${kiosk ? 'text-base' : 'text-xs'}`}>
          {(nest.material || nest.thickness) && (
            <span>{[nest.material, nest.thickness].filter(Boolean).join(' • ')}</span>
          )}
          {nest.sheet_size && <span>Sheet: {nest.sheet_size}</span>}
        </div>
      )}

      {canPreview && (
        <button
          type="button"
          onClick={() => setShowPreview((v) => !v)}
          className={`mt-2 inline-flex items-center gap-2 rounded border border-fd-blue bg-fd-blue/15 font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25 ${
            kiosk ? 'px-4 py-2 text-lg' : 'px-3 py-1.5 text-sm'
          }`}
        >
          {showPreview ? (
            <>
              <XMarkIcon className={kiosk ? 'h-6 w-6' : 'h-4 w-4'} />
              Hide nest PDF
            </>
          ) : (
            <>
              <DocumentMagnifyingGlassIcon className={kiosk ? 'h-6 w-6' : 'h-4 w-4'} />
              Preview nest
            </>
          )}
        </button>
      )}

      {canPreview && showPreview && (
        <div className="mt-3">
          <LaserNestPdfPreview
            laserNestId={nest.id}
            fileName={nest.document_file_name}
            heightClassName={kiosk ? 'h-[520px]' : 'h-[360px]'}
            fetchBlob={fetchNestPdf}
          />
        </div>
      )}
    </div>
  );
}

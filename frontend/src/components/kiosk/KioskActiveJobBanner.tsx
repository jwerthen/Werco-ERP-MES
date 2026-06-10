import React from 'react';
import { PlusCircleIcon, CheckCircleIcon, PauseCircleIcon } from '@heroicons/react/24/solid';
import { ActiveJob } from '../../types';

interface KioskActiveJobBannerProps {
  job: ActiveJob;
  nowMs: number;
  busy: boolean;
  onReportProduction: () => void;
  onComplete: () => void;
  onHold: () => void;
}

function formatElapsed(clockInIso: string, nowMs: number): string {
  const startMs = Date.parse(clockInIso);
  if (!Number.isFinite(startMs)) return '--:--:--';
  const totalSeconds = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return [h, m, s].map((n) => String(n).padStart(2, '0')).join(':');
}

/**
 * Pinned banner for the operator's active entry: running timer plus the three
 * big actions. Deliberately no supervisor verbs (no resume-others, no edits).
 */
export default function KioskActiveJobBanner({ job, nowMs, busy, onReportProduction, onComplete, onHold }: KioskActiveJobBannerProps) {
  return (
    <section
      aria-label="Active job"
      className="rounded border border-fd-green/50 bg-fd-panel p-5 shadow-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <span className="relative flex h-3 w-3">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-fd-green opacity-60" />
              <span className="relative inline-flex h-3 w-3 rounded-full bg-fd-green" />
            </span>
            <span className="font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-green">Running</span>
          </div>
          <div className="mt-2 font-mono text-3xl font-bold text-fd-ink">{job.work_order_number || '—'}</div>
          <div className="mt-1 truncate text-lg text-fd-body">
            <span className="font-mono font-semibold text-fd-ink">{job.part_number || '—'}</span>
            {job.part_name ? <span className="text-fd-mute"> · {job.part_name}</span> : null}
          </div>
          <div className="truncate text-base text-fd-mute">
            Op {job.operation_number ?? '—'} · {job.operation_name || 'Operation'} · {Number(job.quantity_complete || 0)} /{' '}
            {Number(job.quantity_ordered || 0)} pcs
          </div>
        </div>
        <div
          data-testid="kiosk-active-timer"
          className="rounded border border-fd-line bg-fd-sunken px-5 py-3 font-mono text-4xl font-bold tabular-nums text-fd-green"
        >
          {formatElapsed(job.clock_in, nowMs)}
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <button
          type="button"
          disabled={busy}
          onClick={onReportProduction}
          className="flex min-h-20 items-center justify-center gap-3 rounded border border-fd-blue bg-fd-blue/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25 disabled:opacity-40"
        >
          <PlusCircleIcon className="h-8 w-8 shrink-0" />
          Report production
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onComplete}
          className="flex min-h-20 items-center justify-center gap-3 rounded border border-fd-green bg-fd-green/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:opacity-40"
        >
          <CheckCircleIcon className="h-8 w-8 shrink-0" />
          Complete
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onHold}
          className="flex min-h-20 items-center justify-center gap-3 rounded border border-fd-amber bg-fd-amber/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/25 disabled:opacity-40"
        >
          <PauseCircleIcon className="h-8 w-8 shrink-0" />
          Hold
        </button>
      </div>
    </section>
  );
}

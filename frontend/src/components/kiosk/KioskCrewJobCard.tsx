import React from 'react';
import { FireIcon, PaperClipIcon, UserIcon } from '@heroicons/react/24/outline';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../../utils/centralTime';
import { KioskCrewQueueItem, UNKNOWN_OPERATOR_LABEL, formatCrewTally, formatElapsed } from './kioskConstants';
import { KioskRunOrderChip, KioskStepsChip } from './KioskQueueCard';

interface KioskCrewJobCardProps {
  item: KioskCrewQueueItem;
  /** Skew-corrected "now" (local nowMs + serverSkewMs) for honest timers. */
  nowMs: number;
  onSelect: (item: KioskCrewQueueItem) => void;
  disabled?: boolean;
}

/**
 * One queued operation on the crew board: a giant tap target carrying the
 * operation-level tally ("37 of 50 · 2 scrap" — the double-count guard) and a
 * live roster of everyone clocked in, each with their own running timer.
 * The chip strip is deliberately NON-interactive (a labeled list, not buttons):
 * joining/leaving goes through the job screen's badge scan.
 */
export default function KioskCrewJobCard({ item, nowMs, onSelect, disabled = false }: KioskCrewJobCardProps) {
  const pastDue = item.due_date ? isDateBeforeTodayInCentral(item.due_date) : false;
  const dueToday = item.due_date ? isDateTodayInCentral(item.due_date) : false;
  const inProgress = String(item.status).toLowerCase() === 'in_progress';
  const roster = item.roster || [];

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onSelect(item)}
      aria-label={`Work order ${item.work_order_number}, operation ${item.operation_name || item.operation_number || ''}, ${roster.length} clocked in`}
      className={`grid w-full grid-cols-[1fr_auto] items-center gap-4 rounded border px-5 py-5 text-left transition-colors active:translate-y-px disabled:opacity-40 ${
        pastDue
          ? 'border-fd-red/60 bg-fd-red/5 hover:border-fd-red'
          : roster.length > 0
            ? 'border-fd-green/50 bg-fd-panel hover:border-fd-green'
            : 'border-fd-line bg-fd-panel hover:border-fd-line-bright'
      }`}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <KioskRunOrderChip item={item} />
          <span className="font-mono text-3xl font-bold tracking-tight text-fd-ink">{item.work_order_number}</span>
          <span
            className={`rounded border px-2 py-1 font-mono text-xs font-semibold uppercase tracking-widest ${
              inProgress ? 'border-fd-amber/50 text-fd-amber' : 'border-fd-blue/50 text-fd-blue'
            }`}
          >
            {inProgress ? 'In progress' : 'Ready'}
          </span>
          <KioskStepsChip item={item} />
        </div>
        <div className="mt-2 truncate text-xl text-fd-body">
          <span className="font-mono font-semibold text-fd-ink">{item.part_number || '—'}</span>
          {item.part_name ? <span className="text-fd-mute"> · {item.part_name}</span> : null}
        </div>
        <div className="mt-1 truncate text-lg text-fd-mute">
          Op {item.operation_number ?? '—'} · {item.operation_name || 'Operation'}
        </div>

        {/* Live crew roster — one chip per open TimeEntry, with a running timer. */}
        {roster.length > 0 && (
          <ul aria-label="Crew clocked in" className="mt-3 flex flex-wrap gap-2">
            {roster.map((entry) => (
              <li
                key={entry.time_entry_id}
                className="flex items-center gap-2 rounded border border-fd-green/50 bg-fd-green/10 px-3 py-1.5"
              >
                <UserIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-fd-green" />
                <span className="text-lg font-semibold text-fd-ink">
                  {entry.operator_name ?? UNKNOWN_OPERATOR_LABEL}
                </span>
                {entry.entry_type === 'setup' && (
                  <span className="rounded border border-fd-amber/60 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-widest text-fd-amber">
                    Setup
                  </span>
                )}
                <span className="font-mono text-lg font-bold tabular-nums text-fd-green">
                  {formatElapsed(entry.clock_in, nowMs)}
                </span>
              </li>
            ))}
          </ul>
        )}

        {item.laser_nest && (
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-fd-red/40 bg-fd-red/5 px-3 py-2">
            <span className="flex items-center gap-1.5 font-mono text-xl font-bold text-fd-ink">
              <FireIcon className="h-5 w-5 text-fd-red" />
              {item.laser_nest.cnc_number ? `CNC# ${item.laser_nest.cnc_number}` : item.laser_nest.nest_name}
            </span>
            <span className="font-mono text-base text-fd-body">
              {Number(item.laser_nest.completed_runs)} / {Number(item.laser_nest.planned_runs)} runs
            </span>
            {(item.laser_nest.material || item.laser_nest.thickness) && (
              <span className="text-base text-fd-mute">
                {[item.laser_nest.material, item.laser_nest.thickness].filter(Boolean).join(' • ')}
              </span>
            )}
            {item.laser_nest.has_document && (
              <span className="inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-fd-blue">
                <PaperClipIcon className="h-4 w-4" />
                PDF
              </span>
            )}
          </div>
        )}
      </div>

      <div className="text-right">
        {/* Operation-level crew tally — the double-count guard. */}
        <div data-testid="kiosk-crew-tally" className="font-mono text-2xl font-bold text-fd-ink">
          {formatCrewTally(item)}
        </div>
        <div className="mt-1 text-sm uppercase tracking-widest text-fd-faint">crew total</div>
        {item.due_date && (
          <div
            className={`mt-2 inline-block rounded border px-2 py-1 font-mono text-sm font-bold uppercase tracking-wider ${
              pastDue
                ? 'border-fd-red bg-fd-red/15 text-fd-red'
                : dueToday
                  ? 'border-fd-amber bg-fd-amber/15 text-fd-amber'
                  : 'border-fd-line text-fd-mute'
            }`}
          >
            {pastDue ? 'Past due ' : dueToday ? 'Due today' : `Due ${formatCentralDate(item.due_date)}`}
            {pastDue ? formatCentralDate(item.due_date) : ''}
          </div>
        )}
      </div>
    </button>
  );
}

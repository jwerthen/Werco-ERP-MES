import React from 'react';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../../utils/centralTime';
import { KioskQueueItem } from './kioskConstants';

interface KioskQueueCardProps {
  item: KioskQueueItem;
  onSelect: (item: KioskQueueItem) => void;
  disabled?: boolean;
}

/**
 * One queued operation as a single giant tap target (full-width, ~7rem tall).
 * Tap → confirm screen → CLOCK IN: two taps for the 90% path.
 */
export default function KioskQueueCard({ item, onSelect, disabled = false }: KioskQueueCardProps) {
  const pastDue = item.due_date ? isDateBeforeTodayInCentral(item.due_date) : false;
  const dueToday = item.due_date ? isDateTodayInCentral(item.due_date) : false;
  const inProgress = String(item.status).toLowerCase() === 'in_progress';
  const done = Number(item.quantity_complete || 0);
  const ordered = Number(item.quantity_ordered || 0);

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onSelect(item)}
      aria-label={`Work order ${item.work_order_number}, operation ${item.operation_name || item.operation_number || ''}`}
      className={`grid w-full grid-cols-[1fr_auto] items-center gap-4 rounded border px-5 py-5 text-left transition-colors active:translate-y-px disabled:opacity-40 ${
        pastDue
          ? 'border-fd-red/60 bg-fd-red/5 hover:border-fd-red'
          : 'border-fd-line bg-fd-panel hover:border-fd-line-bright'
      }`}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-3xl font-bold tracking-tight text-fd-ink">{item.work_order_number}</span>
          <span
            className={`rounded border px-2 py-1 font-mono text-xs font-semibold uppercase tracking-widest ${
              inProgress ? 'border-fd-amber/50 text-fd-amber' : 'border-fd-blue/50 text-fd-blue'
            }`}
          >
            {inProgress ? 'In progress' : 'Ready'}
          </span>
        </div>
        <div className="mt-2 truncate text-xl text-fd-body">
          <span className="font-mono font-semibold text-fd-ink">{item.part_number || '—'}</span>
          {item.part_name ? <span className="text-fd-mute"> · {item.part_name}</span> : null}
        </div>
        <div className="mt-1 truncate text-lg text-fd-mute">
          Op {item.operation_number ?? '—'} · {item.operation_name || 'Operation'}
        </div>
      </div>

      <div className="text-right">
        <div className="font-mono text-2xl font-bold text-fd-ink">
          {done}
          <span className="text-fd-faint"> / </span>
          {ordered}
        </div>
        <div className="mt-1 text-sm uppercase tracking-widest text-fd-faint">pcs</div>
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

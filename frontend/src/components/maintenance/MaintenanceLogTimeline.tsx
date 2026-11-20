import React from 'react';
import { ClipboardDocumentListIcon } from '@heroicons/react/24/outline';
import { EmptyState } from '../ui/EmptyState';
import { ErrorState } from '../ui/ErrorState';
import { formatCentralDateTime } from '../../utils/centralTime';
import { formatCurrency } from '../../utils/numberFormat';
import { logEventTypeLabel } from '../../utils/maintenance';
import type { MaintenanceLogEntry } from '../../types/maintenance';

// Event types are a log vocabulary, not record statuses, so they keep their own colors here
// rather than the shared status map. A legacy free-text type falls back to slate.
const EVENT_TYPE_CLASSES: Record<string, string> = {
  inspection: 'border-blue-500/40 bg-blue-500/10 text-blue-300',
  repair: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  part_replacement: 'border-orange-500/40 bg-orange-500/10 text-orange-300',
  observation: 'border-slate-500/40 bg-slate-500/10 text-slate-300',
  note: 'border-slate-500/40 bg-slate-500/10 text-slate-300',
  started: 'border-blue-500/40 bg-blue-500/10 text-blue-300',
  completed: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
};
const FALLBACK_EVENT_CLASS = 'border-slate-600 bg-slate-800/50 text-slate-300';

interface Props {
  logs: MaintenanceLogEntry[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /** Names the list for assistive technology ("Maintenance log for MWO-…"). */
  label: string;
  /** Shown under "No log entries yet". */
  emptyDescription?: string;
}

/**
 * A maintenance log, newest first as the server orders it: each entry's type, when it happened
 * (Central), who filed it, what it says, and its cost when there was one. Loading, error and empty
 * states go through the shared primitives. Read-only: entries are added by
 * `AddMaintenanceLogForm` and never edited.
 */
export function MaintenanceLogTimeline({ logs, loading, error, onRetry, label, emptyDescription }: Props) {
  if (error) {
    return <ErrorState title="Couldn't load the maintenance log" message={error} onRetry={onRetry} />;
  }
  if (loading && logs.length === 0) {
    return (
      <p role="status" className="text-sm text-slate-400">
        Loading the maintenance log…
      </p>
    );
  }
  if (logs.length === 0) {
    return (
      <EmptyState
        icon={ClipboardDocumentListIcon}
        title="No log entries yet"
        description={emptyDescription}
        className="py-6"
      />
    );
  }
  return (
    <ol aria-label={label} aria-busy={loading || undefined} className="ml-1 border-l border-slate-700">
      {logs.map(entry => (
        <li key={entry.id} className="relative pb-3 pl-4 last:pb-0">
          <span aria-hidden="true" className="absolute -left-[4px] top-1.5 h-[7px] w-[7px] bg-slate-500" />
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
            <span
              className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 font-medium ${
                EVENT_TYPE_CLASSES[entry.event_type] ?? FALLBACK_EVENT_CLASS
              }`}
            >
              {logEventTypeLabel(entry.event_type)}
            </span>
            {entry.event_date ? (
              <time dateTime={entry.event_date} className="text-slate-300">
                {formatCentralDateTime(entry.event_date)}
              </time>
            ) : (
              <span className="text-slate-500">Date not recorded</span>
            )}
            <span className="text-slate-400">by {entry.performed_by_name || 'an unknown user'}</span>
            {entry.cost != null && entry.cost > 0 && (
              <span className="font-mono text-slate-300">{formatCurrency(entry.cost)}</span>
            )}
          </div>
          {entry.description && (
            <p className="mt-1 whitespace-pre-wrap break-words text-sm text-slate-200">{entry.description}</p>
          )}
        </li>
      ))}
    </ol>
  );
}

export default MaintenanceLogTimeline;

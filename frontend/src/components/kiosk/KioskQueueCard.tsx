import React from 'react';
import { FireIcon, PaperClipIcon } from '@heroicons/react/24/outline';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../../utils/centralTime';
import { KioskQueueItem, formatStepsChip } from './kioskConstants';

/**
 * "Steps 2/6" — required process-step progress for the operation. Hidden when
 * the snapshot has no gating steps (0/0). Green once every required step has
 * a satisfying record, cyan while work remains.
 */
export function KioskStepsChip({ item }: { item: Pick<KioskQueueItem, 'steps_total' | 'steps_recorded'> }) {
  const total = Number(item.steps_total || 0);
  if (total <= 0) return null;
  const recorded = Number(item.steps_recorded || 0);
  return (
    <span
      data-testid="kiosk-steps-chip"
      className={`rounded border px-2 py-1 font-mono text-xs font-semibold uppercase tracking-widest ${
        recorded >= total ? 'border-fd-green/50 text-fd-green' : 'border-fd-cyan/50 text-fd-cyan'
      }`}
    >
      {formatStepsChip(item)}
    </span>
  );
}

/**
 * "RUN 1" — the manager-dictated run order for this operation (Dispatch Board).
 *
 * Advisory only: the server already sorts the queue by it and ANY job can still
 * be started, so the chip only DISPLAYS the rank — it never reorders client-side.
 * Renders nothing when the operation is unranked (`run_order` null/absent).
 *
 * The ONE run-chip implementation for every surface that lists work as a queue:
 * the kiosk/crew cards AND the desktop shop-floor pages (ShopFloor,
 * ShopFloorSimple). The default `kiosk` size is deliberately oversized /
 * high-contrast — read at arm's length on a shop tablet; `sm` is the same chip
 * scaled for dense desktop rows. Do not fork a second implementation.
 */
export function KioskRunOrderChip({
  item,
  size = 'kiosk',
}: {
  item: Pick<KioskQueueItem, 'run_order'>;
  size?: 'kiosk' | 'sm';
}) {
  const rank = item.run_order;
  if (rank === null || rank === undefined) return null;
  const numeric = Number(rank);
  if (!Number.isFinite(numeric)) return null;
  const compact = size === 'sm';
  return (
    <span
      data-testid="kiosk-run-order-chip"
      aria-label={`Run order ${numeric}`}
      className={`inline-flex items-center rounded border-fd-amber bg-fd-amber/15 font-mono font-bold uppercase tracking-widest text-fd-amber ${
        compact ? 'gap-1 border px-1.5 py-0.5' : 'gap-2 border-2 px-3 py-1 text-xl'
      }`}
    >
      <span className={compact ? 'text-[10px] tracking-widest' : 'text-sm tracking-widest'}>Run</span>
      <span className={`leading-none tabular-nums ${compact ? 'text-sm' : 'text-2xl'}`}>{numeric}</span>
    </span>
  );
}

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
        {item.laser_nest && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-fd-red/40 bg-fd-red/5 px-3 py-2">
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

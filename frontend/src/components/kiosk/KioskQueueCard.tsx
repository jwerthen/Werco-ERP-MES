import React from 'react';
import { ArrowUpRightIcon } from '@heroicons/react/24/outline';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../../utils/centralTime';
import { KioskQueueItem, formatStepsChip } from './kioskConstants';

/**
 * "Steps 2/6" — required process-step progress for the operation. Hidden when
 * the snapshot has no gating steps (0/0). Green once every required step has
 * a satisfying record, quiet gray while work remains (Foundry 1b).
 */
export function KioskStepsChip({ item }: { item: Pick<KioskQueueItem, 'steps_total' | 'steps_recorded'> }) {
  const total = Number(item.steps_total || 0);
  if (total <= 0) return null;
  const recorded = Number(item.steps_recorded || 0);
  return (
    <span
      data-testid="kiosk-steps-chip"
      className={`rounded-[3px] border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] ${
        recorded >= total ? 'border-fd-green/40 text-fd-green' : 'border-fd-line text-fd-mute'
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
 * `active` = this op is the operator's running job → solid green (Foundry 1b).
 */
export function KioskRunOrderChip({
  item,
  active = false,
}: {
  item: Pick<KioskQueueItem, 'run_order'>;
  active?: boolean;
}) {
  const rank = item.run_order;
  if (rank === null || rank === undefined) return null;
  const numeric = Number(rank);
  if (!Number.isFinite(numeric)) return null;
  return (
    <span
      data-testid="kiosk-run-order-chip"
      aria-label={`Run order ${numeric}`}
      className={`inline-flex items-center gap-1 rounded-[3px] px-2 py-1 font-mono text-[11px] font-bold uppercase tracking-[0.06em] ${
        active
          ? 'bg-fd-green text-[#04101f]'
          : 'border border-fd-line-bright bg-fd-raised text-fd-ink'
      }`}
    >
      <span>Run</span>
      <span className="tabular-nums">{numeric}</span>
    </span>
  );
}

interface KioskQueueCardProps {
  item: KioskQueueItem;
  onSelect: (item: KioskQueueItem) => void;
  /** True when this op is the operator's active (clocked-in) job — 1b active card chrome. */
  active?: boolean;
  disabled?: boolean;
  /** Foundry doc-viewer entry: renders the CNC-strip PDF chip as a real button. */
  onOpenPdf?: (item: KioskQueueItem) => void;
}

/**
 * One queued operation as a Foundry 1b queue card. The whole card is one tap
 * target (→ the existing confirm→clock-in flow — ANY job stays startable, per
 * the advisory run-order convention); the CNC strip's PDF chip is a nested
 * doc-viewer entry, so the card is a role="button" div (the sanctioned wrapper
 * pattern) and the chip stops propagation.
 */
export default function KioskQueueCard({ item, onSelect, active = false, disabled = false, onOpenPdf }: KioskQueueCardProps) {
  const pastDue = item.due_date ? isDateBeforeTodayInCentral(item.due_date) : false;
  const dueToday = item.due_date ? isDateTodayInCentral(item.due_date) : false;
  const inProgress = String(item.status).toLowerCase() === 'in_progress';
  const done = Number(item.quantity_complete || 0);
  const ordered = Number(item.quantity_ordered || 0);
  const nest = item.laser_nest;

  const select = () => {
    if (!disabled) onSelect(item);
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      onClick={select}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          select();
        }
      }}
      aria-label={`Work order ${item.work_order_number}, operation ${item.operation_name || item.operation_number || ''}`}
      className={`w-full rounded-[4px] border bg-fd-panel px-4 py-3.5 text-left transition-transform duration-150 ease-out active:scale-[0.99] ${
        active
          ? 'border-fd-line-bright border-l-2 border-l-fd-green'
          : pastDue
            ? 'border-fd-line border-l-2 border-l-fd-red'
            : 'border-fd-line'
      } ${disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer'}`}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <KioskRunOrderChip item={item} active={active} />
        <span className="font-mono text-lg font-bold text-fd-ink">{item.work_order_number}</span>
        <span
          className={`rounded-[3px] border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] ${
            active
              ? 'border-fd-green/40 text-fd-green'
              : inProgress
                ? 'border-fd-amber/50 text-fd-amber'
                : 'border-fd-blue/40 text-fd-blue'
          }`}
        >
          {active ? 'On machine' : inProgress ? 'In progress' : 'Ready'}
        </span>
        <KioskStepsChip item={item} />
        <div className="flex-1" />
        <span className="font-mono text-[15px] font-bold tabular-nums text-fd-ink">
          {done}
          <span className="font-normal text-fd-mute">/{ordered}</span>
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px] text-fd-body">
        <span className="min-w-0 truncate">
          <span className="font-mono font-semibold text-fd-body-2">{item.part_number || '—'}</span>
          {item.part_name ? ` ${item.part_name}` : ''} · Op {item.operation_number ?? '—'}
        </span>
        <div className="flex-1" />
        {item.due_date && (
          <span
            className={`shrink-0 rounded-[3px] border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] ${
              pastDue
                ? 'border-fd-red/50 bg-fd-red/8 font-bold text-fd-red'
                : dueToday
                  ? 'border-fd-amber/45 bg-fd-amber/8 text-fd-amber'
                  : 'border-fd-line text-fd-mute'
            }`}
          >
            {pastDue
              ? `Past due · ${formatCentralDate(item.due_date, { year: undefined })}`
              : dueToday
                ? 'Due today'
                : `Due ${formatCentralDate(item.due_date, { year: undefined })}`}
          </span>
        )}
      </div>

      {nest && (
        <div className="mt-2.5 flex items-center gap-2.5 rounded-[3px] border border-fd-line bg-fd-sunken px-2.5 py-2 font-mono text-[11px] text-fd-body">
          <span className="min-w-0 truncate uppercase">
            {nest.cnc_number ? `CNC# ${nest.cnc_number}` : nest.nest_name}
            {` · ${Number(nest.completed_runs)}/${Number(nest.planned_runs)} runs`}
            {nest.material ? ` · ${nest.material}` : ''}
            {nest.thickness ? ` · ${nest.thickness}` : ''}
          </span>
          <div className="flex-1" />
          {nest.has_document &&
            (onOpenPdf ? (
              <button
                type="button"
                aria-label={`Open nest PDF for ${item.work_order_number}`}
                disabled={disabled}
                onClick={(e) => {
                  // The card's own tap target starts the clock-in confirm —
                  // opening the viewer must not also fire it.
                  e.stopPropagation();
                  onOpenPdf(item);
                }}
                className="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-[3px] px-2 font-mono text-[11px] font-semibold uppercase tracking-[0.06em] text-fd-blue transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
              >
                PDF
                <ArrowUpRightIcon className="h-3 w-3" aria-hidden="true" />
              </button>
            ) : (
              <span className="inline-flex shrink-0 items-center gap-1.5 font-semibold uppercase tracking-[0.06em] text-fd-blue">
                PDF
                <ArrowUpRightIcon className="h-3 w-3" aria-hidden="true" />
              </span>
            ))}
        </div>
      )}
    </div>
  );
}

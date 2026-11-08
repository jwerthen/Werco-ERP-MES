import React from 'react';
import { ArrowUpIcon, ArrowUpRightIcon, PauseIcon } from '@heroicons/react/24/outline';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../../utils/centralTime';
import { startBlockedLabel } from '../../utils/startBlock';
import { UnitBadge } from '../ui';
import { KioskQueueItem, formatOperationLabel, formatStepsChip, operationNumberText } from './kioskConstants';

/**
 * "NOT RELEASED" / "WO ON HOLD" — the WORK ORDER refuses every start on this job
 * (owner ruling 2026-11-05: the floor never starts work a supervisor has not
 * released, or has put on hold). Renders nothing unless the row carries the
 * server's `start_blocked_reason`; the chip names the state in two words and
 * its `title` carries the server's full text, verbatim.
 *
 * Shared by the kiosk queue card and the crew job card so the two read alike.
 * The card itself stays tappable: the confirm / job screen is where the reason
 * is spelled out and the start control is disabled.
 */
export function KioskStartBlockedChip({
  item,
  size = 'kiosk',
}: {
  item: Pick<KioskQueueItem, 'start_blocked_reason' | 'work_order_status'>;
  size?: 'kiosk' | 'crew';
}) {
  if (!item.start_blocked_reason) return null;
  return (
    <span
      data-testid="kiosk-start-blocked-chip"
      title={item.start_blocked_reason}
      className={`inline-flex items-center gap-1 rounded-[3px] border border-fd-amber/60 bg-fd-amber/10 font-mono font-bold uppercase text-fd-amber ${
        size === 'crew' ? 'px-2 py-1 text-xs tracking-widest' : 'px-1.5 py-0.5 text-[10px] tracking-[0.08em]'
      }`}
    >
      <PauseIcon aria-hidden="true" className={size === 'crew' ? 'h-4 w-4' : 'h-3 w-3'} />
      {startBlockedLabel(item.work_order_status)}
    </span>
  );
}

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
 * "MOVED UP" — the planner moved this operation ahead of work it used to follow
 * (its id is in an unacknowledged dispatch notice's `moved_up`). Shown until the
 * screen's "Got it" acknowledges the notice; purely informational — it changes
 * nothing about what may be started (run order is advisory).
 *
 * Shared by the kiosk queue card and the crew job card so the two read alike.
 */
export function KioskMovedUpChip({ size = 'kiosk' }: { size?: 'kiosk' | 'crew' }) {
  return (
    <span
      data-testid="kiosk-moved-up-chip"
      className={`inline-flex items-center gap-1 rounded-[3px] border border-fd-amber/60 bg-fd-amber/15 font-mono font-bold uppercase text-fd-amber ${
        size === 'crew' ? 'px-2 py-1 text-xs tracking-widest' : 'px-2 py-1 text-[11px] tracking-[0.06em]'
      }`}
    >
      <ArrowUpIcon aria-hidden="true" className={size === 'crew' ? 'h-4 w-4' : 'h-3 w-3'} />
      Moved up
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
 *
 * The ONE run-chip implementation for every surface that lists work as a queue:
 * the kiosk/crew cards AND the desktop shop-floor pages (ShopFloor,
 * ShopFloorSimple, Scheduling). The default `kiosk` size is sized for the
 * Foundry kiosk card; `sm` is the same chip tightened for dense desktop rows.
 * Do not fork a second implementation.
 */
export function KioskRunOrderChip({
  item,
  active = false,
  size = 'kiosk',
}: {
  item: Pick<KioskQueueItem, 'run_order'>;
  active?: boolean;
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
      className={`inline-flex items-center gap-1 rounded-[3px] font-mono font-bold uppercase tracking-[0.06em] ${
        compact ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-1 text-[11px]'
      } ${
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
  /** The planner moved this job up and the operator has not said "Got it" yet. */
  movedUp?: boolean;
}

/**
 * One queued operation as a Foundry 1b queue card. The whole card is one tap
 * target (→ the existing confirm→clock-in flow — ANY job stays startable, per
 * the advisory run-order convention); the CNC strip's PDF chip is a nested
 * doc-viewer entry, so the card is a role="button" div (the sanctioned wrapper
 * pattern) and the chip stops propagation.
 */
export default function KioskQueueCard({
  item,
  onSelect,
  active = false,
  disabled = false,
  onOpenPdf,
  movedUp = false,
}: KioskQueueCardProps) {
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
      // `operationNumberText`, not the raw column: the visible line below runs the
      // same value through `formatOperationLabel`, so interpolating it raw made a
      // screen reader announce "operation Op 10" for a legacy row and "operation 10"
      // for a new one -- the same operation, named two ways, in the same card.
      aria-label={`Work order ${item.work_order_number}, operation ${
        item.operation_name || operationNumberText(item.operation_number) || ''
      }${movedUp ? ', moved up' : ''}${
        item.start_blocked_reason ? `, ${startBlockedLabel(item.work_order_status).toLowerCase()}` : ''
      }`}
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
        {movedUp && <KioskMovedUpChip />}
        <span className="font-mono text-lg font-bold text-fd-ink">{item.work_order_number}</span>
        <UnitBadge unitNumber={item.unit_number} size="md" />
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
        <KioskStartBlockedChip item={item} />
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
          {item.part_name ? ` ${item.part_name}` : ''} · {formatOperationLabel(item.operation_number)}
        </span>
        {/* The COMPONENT this operation builds, when it is not the work order's own
            part — on a BOM-exploded assembly the number above is the ASSEMBLY's.
            Read LIVE, unlike the copy baked into operation_name when the work order
            was raised, which a renumber deliberately leaves alone. */}
        {item.component_part_number ? (
          <span className="min-w-0 truncate">
            <span className="text-fd-mute">Component </span>
            <span className="font-mono font-semibold text-fd-body-2">{item.component_part_number}</span>
          </span>
        ) : null}
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

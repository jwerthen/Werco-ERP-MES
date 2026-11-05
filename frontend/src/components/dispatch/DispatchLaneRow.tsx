/**
 * One work order on the work-order-first board: a compact one-line lane, with
 * an on-demand detail panel (its operations, drawn by mode).
 *
 * The line carries what a planner ranks by — plan position, the work order
 * (router link), unit, part, customer, quantity, the promise date, status, mode,
 * progress, blockers, material shortages and who is on it right now — and the
 * keyboard equivalents of every drag: Move up / Move down / Move to top /
 * Set position… / Remove from plan (planned), or Add to plan / Add at position…
 * (not in plan). Drag is a pointer-only enhancement over those buttons.
 *
 * In-flight gating is `aria-disabled` (the control stays focusable and inert),
 * never `disabled`, so a keyboard user standing on a control is not blurred to
 * <body> by their own action. Structural boundaries (already first / last) are
 * real `disabled`.
 */

import React from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowDownIcon,
  ArrowUpIcon,
  BarsArrowUpIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  HashtagIcon,
  PlusIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { Button, StatusBadge, UnitBadge } from '../ui';
import { formatCentralDate } from '../../utils/centralTime';
import { countShortTies } from '../../utils/materialTie';
import {
  laneCrewNames,
  laneIsRunning,
  operationOrderBlockedReason,
  promiseTone,
  ROUTING_ORDER_REASON,
} from '../../utils/dispatchPlan';
import type { DispatchLane, DispatchLaneMode, DispatchLaneOp, DispatchWorkCenterOption } from '../../types';
import { MATERIAL_SHORT_ROLLUP_TITLE, ROLE_PIN_REASON, SAVING_REASON } from './DispatchMachineView';
import { DispatchPoolOperations, DispatchRoutingStrip } from './DispatchLaneOperations';

const MODE_LABEL: Record<DispatchLaneMode, string> = { routing: 'Routing', pool: 'Pool', laser: 'Laser' };

const MODE_TITLE: Record<DispatchLaneMode, string> = {
  routing: 'Sequenced routing — operations run in routing order (read-only here).',
  pool: 'Dispatch pool — operations sharing a machine can run in any order; set it when expanded.',
  laser: 'Laser nest package — nests run in any order; set it when expanded.',
};

/** Whole numbers stay whole; fractional quantities keep two decimals at most. */
const qty = (value: number | null | undefined) => String(Number(Number(value || 0).toFixed(2)));

export interface DispatchLaneRowProps {
  lane: DispatchLane;
  /** In the Plan section (has a `dispatch_position`). */
  planned: boolean;
  canEdit: boolean;
  /** A dispatch write is in flight anywhere on the board. */
  busy: boolean;
  /** Plan section: a visible planned lane exists above / below this one. */
  canMoveUp: boolean;
  canMoveDown: boolean;
  /** Plan section: already position 1. */
  atTop: boolean;
  expanded: boolean;
  dragging: boolean;
  /** Draw the drop slot BEFORE / AFTER this lane while dragging. */
  dropBefore: boolean;
  dropAfter: boolean;
  workCenters: DispatchWorkCenterOption[];
  movingOpIds: readonly number[];
  onToggleExpanded: (lane: DispatchLane) => void;
  onMoveUp: (lane: DispatchLane) => void;
  onMoveDown: (lane: DispatchLane) => void;
  onMoveToTop: (lane: DispatchLane) => void;
  onSetPosition: (lane: DispatchLane) => void;
  onRemove: (lane: DispatchLane) => void;
  onAdd: (lane: DispatchLane) => void;
  onDragStart: (event: React.DragEvent, lane: DispatchLane) => void;
  onDragEnd: () => void;
  onDragOver?: (event: React.DragEvent, lane: DispatchLane) => void;
  onDrop?: (event: React.DragEvent, lane: DispatchLane) => void;
  onReorderOperation: (lane: DispatchLane, operationId: number, toIndex: number, direction?: 'up' | 'down') => void;
  onMoveOperations: (lane: DispatchLane, ops: DispatchLaneOp[], targetWorkCenterId: number) => void;
  onOperationDragActiveChange: (active: boolean) => void;
}

export default function DispatchLaneRow({
  lane,
  planned,
  canEdit,
  busy,
  canMoveUp,
  canMoveDown,
  atTop,
  expanded,
  dragging,
  dropBefore,
  dropAfter,
  workCenters,
  movingOpIds,
  onToggleExpanded,
  onMoveUp,
  onMoveDown,
  onMoveToTop,
  onSetPosition,
  onRemove,
  onAdd,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDrop,
  onReorderOperation,
  onMoveOperations,
  onOperationDragActiveChange,
}: DispatchLaneRowProps) {
  const wo = lane.work_order_number;
  const detailId = `dispatch-lane-detail-${lane.work_order_id}`;
  const held = String(lane.status).toLowerCase() === 'on_hold';
  const tone = promiseTone(lane.promise_date);
  const running = laneIsRunning(lane);
  const crew = laneCrewNames(lane);
  const shortCount = countShortTies(lane.operations);
  const blockers = Number(lane.open_blocker_count || 0);
  const opsTotal = Number(lane.ops_total ?? lane.operations.length);
  const opsComplete = Number(lane.ops_complete ?? 0);
  const inertReason = !canEdit ? ROLE_PIN_REASON : busy ? SAVING_REASON : undefined;
  const mode: DispatchLaneMode = lane.mode === 'laser' || lane.mode === 'pool' ? lane.mode : 'routing';

  const inert = (handler: (lane: DispatchLane) => void) => () => {
    if (busy || !canEdit) return;
    handler(lane);
  };

  const promiseTitle = lane.must_ship_by
    ? `Must ship by ${formatCentralDate(lane.must_ship_by)}${lane.due_date ? ` (due ${formatCentralDate(lane.due_date)})` : ''}`
    : lane.due_date
      ? `Due ${formatCentralDate(lane.due_date)}`
      : undefined;

  return (
    <li
      data-lane-row={lane.work_order_id}
      data-testid={`dispatch-lane-${lane.work_order_id}`}
      // Focus fallback when every control the user stood on is disabled by
      // their own move (e.g. Move to top disables Move up AND Move to top).
      tabIndex={-1}
      className={`rounded-sm border bg-fd-panel outline-none focus-visible:ring-1 focus-visible:ring-blue-400 ${
        held ? 'border-amber-500/40' : running ? 'border-blue-400/50' : 'border-fd-line'
      } ${dragging ? 'opacity-50' : ''} ${dropBefore ? 'shadow-[inset_0_2px_0_0_#60a5fa]' : ''} ${
        dropAfter ? 'shadow-[inset_0_-2px_0_0_#60a5fa]' : ''
      }`}
    >
      {/* Drag surface (the lane's one-line header). Presentational: drag is a
          pointer-only enhancement and the accessible equivalents are the
          Move / Set position / Add buttons. */}
      <div
        role="presentation"
        data-testid={`dispatch-lane-drag-${lane.work_order_id}`}
        draggable={canEdit && !busy}
        onDragStart={event => onDragStart(event, lane)}
        onDragEnd={onDragEnd}
        onDragOver={onDragOver ? event => onDragOver(event, lane) : undefined}
        onDrop={onDrop ? event => onDrop(event, lane) : undefined}
      >
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1.5">
          <span
            data-testid={`dispatch-lane-position-${lane.work_order_id}`}
            className={`w-7 shrink-0 text-center font-mono text-lg font-bold leading-none tabular-nums ${
              planned ? (held ? 'text-amber-300' : 'text-white') : 'text-slate-600'
            }`}
            title={
              planned
                ? held
                  ? `Plan position ${lane.dispatch_position}. On hold: its operations drop below the plan at every machine until the hold clears.`
                  : `Plan position ${lane.dispatch_position}`
                : 'Not in the plan — runs after every planned work order'
            }
          >
            {planned ? lane.dispatch_position : '–'}
          </span>
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailId}
            aria-label={`${expanded ? 'Hide' : 'Show'} details for ${wo}`}
            onClick={() => onToggleExpanded(lane)}
            className="shrink-0 rounded-sm p-0.5 text-slate-400 hover:bg-slate-700/60 hover:text-slate-100"
          >
            {expanded ? (
              <ChevronDownIcon className="h-4 w-4" aria-hidden="true" />
            ) : (
              <ChevronRightIcon className="h-4 w-4" aria-hidden="true" />
            )}
          </button>
          <Link
            to={`/work-orders/${lane.work_order_id}`}
            className="shrink-0 font-mono text-sm font-semibold text-slate-100 underline decoration-slate-600 underline-offset-2 hover:text-white"
          >
            {wo}
          </Link>
          <UnitBadge unitNumber={lane.unit_number} size="sm" />
          <span className="min-w-[6rem] flex-1 truncate font-mono text-[11px] text-slate-300">
            {lane.part_number || (mode === 'laser' ? 'Laser nests' : '—')}
            {lane.part_name ? <span className="font-sans text-slate-400"> · {lane.part_name}</span> : null}
          </span>
          {lane.customer_name && (
            <span
              className="hidden max-w-[10rem] truncate text-[11px] text-slate-400 lg:inline"
              title={lane.customer_name}
            >
              {lane.customer_name}
            </span>
          )}
          <span className="font-mono text-[11px] tabular-nums text-slate-400" title="Complete / ordered">
            {qty(lane.quantity_complete)}/{qty(lane.quantity_ordered)}
          </span>
          {lane.promise_date && (
            <span
              data-testid={`dispatch-lane-promise-${lane.work_order_id}`}
              title={promiseTitle}
              className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                tone === 'late'
                  ? 'border-red-500/60 text-red-300'
                  : tone === 'today'
                    ? 'border-amber-500/60 text-amber-300'
                    : 'border-fd-line text-slate-400'
              }`}
            >
              {tone === 'late'
                ? `Late · ${formatCentralDate(lane.promise_date, { year: undefined })}`
                : tone === 'today'
                  ? 'Due today'
                  : `Due ${formatCentralDate(lane.promise_date, { year: undefined })}`}
            </span>
          )}
          <StatusBadge status={String(lane.status)} />
          <span
            title={MODE_TITLE[mode]}
            className="shrink-0 rounded border border-fd-line px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-slate-400"
          >
            {MODE_LABEL[mode]}
          </span>
          <span
            className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-slate-400"
            title="Operations complete"
          >
            {opsComplete}/{opsTotal} ops
          </span>
          {blockers > 0 && (
            <span
              title="Unresolved blockers on this work order"
              className="shrink-0 rounded border border-red-500/60 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-red-300"
            >
              {blockers} blocker{blockers === 1 ? '' : 's'}
            </span>
          )}
          {(shortCount > 0 || lane.any_material_short) && (
            <span
              data-testid={`dispatch-lane-short-${lane.work_order_id}`}
              title={MATERIAL_SHORT_ROLLUP_TITLE}
              className="shrink-0 rounded border border-red-500/60 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-red-300"
            >
              {shortCount > 0 ? `${shortCount} short on material` : 'Short on material'}
            </span>
          )}
          {running && (
            <span
              data-testid={`dispatch-lane-running-${lane.work_order_id}`}
              className="shrink-0 font-mono text-[10px] font-semibold uppercase tracking-wider text-blue-300"
            >
              <span aria-hidden="true">▶ </span>
              {crew.length > 0 ? (
                <span className="normal-case tracking-normal">
                  Running · {crew.slice(0, 2).join(', ')}
                  {crew.length > 2 ? ` +${crew.length - 2}` : ''}
                </span>
              ) : (
                'Running'
              )}
            </span>
          )}

          {canEdit && (
            <div className="ml-auto flex shrink-0 items-center gap-0.5">
              {planned ? (
                <>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="up"
                    aria-label={`Move ${wo} up`}
                    title={inertReason || 'Move up one position'}
                    disabled={!canMoveUp}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onMoveUp)}
                  >
                    <ArrowUpIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="down"
                    aria-label={`Move ${wo} down`}
                    title={inertReason || 'Move down one position'}
                    disabled={!canMoveDown}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onMoveDown)}
                  >
                    <ArrowDownIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="top"
                    aria-label={`Move ${wo} to top`}
                    title={inertReason || 'Move to plan position 1'}
                    disabled={atTop}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onMoveToTop)}
                  >
                    <BarsArrowUpIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="position"
                    aria-label={`Set plan position for ${wo}`}
                    title={inertReason || 'Set position…'}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onSetPosition)}
                  >
                    <HashtagIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="remove"
                    aria-label={`Remove ${wo} from plan`}
                    title={inertReason || 'Remove from plan — its operations fall back to machine order'}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onRemove)}
                  >
                    <XMarkIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    variant="secondary"
                    size="sm"
                    data-lane-action="add"
                    aria-label={`Add ${wo} to plan`}
                    title={inertReason || 'Add to the end of the plan'}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onAdd)}
                  >
                    <PlusIcon className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
                    Add to plan
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-lane-action="position"
                    aria-label={`Add ${wo} to plan at a position`}
                    title={inertReason || 'Add at position…'}
                    aria-disabled={busy || undefined}
                    className={busy ? 'opacity-40' : undefined}
                    onClick={inert(onSetPosition)}
                  >
                    <HashtagIcon className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {/* The detail sits OUTSIDE the lane's drag surface: its operation rows are
          drag sources of their own, and a nested draggable would bubble its
          dragstart up into a lane drag. */}
      {expanded && (
        <div id={detailId} className="space-y-2 border-t border-fd-line px-3 py-2">
          <LaneMeta lane={lane} />
          {mode === 'routing' ? (
            <DispatchRoutingStrip lane={lane} />
          ) : (
            <DispatchPoolOperations
              lane={lane}
              canEdit={canEdit}
              busy={busy}
              blockedReason={
                operationOrderBlockedReason(lane) === ROUTING_ORDER_REASON ? null : operationOrderBlockedReason(lane)
              }
              workCenters={workCenters}
              movingOpIds={movingOpIds}
              onReorder={onReorderOperation}
              onMoveOperations={onMoveOperations}
              onDragActiveChange={onOperationDragActiveChange}
            />
          )}
        </div>
      )}
    </li>
  );
}

function LaneMeta({ lane }: { lane: DispatchLane }) {
  const parts: Array<{ key: string; label: string; value: string }> = [];
  if (lane.priority != null) parts.push({ key: 'priority', label: 'Priority', value: `P${lane.priority}` });
  if (lane.due_date) parts.push({ key: 'due', label: 'Due', value: formatCentralDate(lane.due_date) });
  if (lane.must_ship_by)
    parts.push({ key: 'ship', label: 'Must ship by', value: formatCentralDate(lane.must_ship_by) });
  if (lane.customer_name) parts.push({ key: 'customer', label: 'Customer', value: lane.customer_name });
  parts.push({ key: 'qty', label: 'Qty', value: `${qty(lane.quantity_complete)} of ${qty(lane.quantity_ordered)}` });
  return (
    <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">
      {parts.map(part => (
        <div key={part.key} className="flex gap-1">
          <dt className="text-slate-500">{part.label}</dt>
          <dd className="font-mono text-slate-300">{part.value}</dd>
        </div>
      ))}
    </dl>
  );
}

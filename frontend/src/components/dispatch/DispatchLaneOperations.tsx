/**
 * The operations of one EXPANDED work-order lane, drawn by the lane's mode.
 *
 * - `routing` (a sequenced routing): a horizontal strip in sequence order. The
 *   routing IS the order — read-only here, because re-sequencing a released
 *   routing is the migration-081 hazard and the operation-order PUT refuses it.
 * - `pool` / `laser`: operations grouped by machine in a compact vertical list.
 *   Operations sharing a machine are mutually startable, so the planner may order
 *   them (drag, or Move up / Move down) — only ever WITHIN one machine, because
 *   only that relative order means anything to the server. Each operation can be
 *   sent to another machine ("Move to machine…", non-optimistic: the server
 *   refuses running / completed work), and several at once ("Move selected to…").
 *
 * Everything here is presentational plus local drag/selection state; the view
 * (`DispatchWorkOrderView`) owns the writes, the in-flight gate and the
 * announcements.
 */

import React, { useMemo, useState } from 'react';
import { ArrowDownIcon, ArrowUpIcon } from '@heroicons/react/24/outline';
import { Button } from '../ui';
import { KioskRunOrderChip } from '../kiosk/KioskQueueCard';
import { changeoverLabel, nestChangeover, nestDetailSegments, nestQueueSummary } from '../../utils/nestChangeover';
import { materialTieChip } from '../../utils/materialTie';
import { formatOperationLabel } from '../../utils/operationLabel';
import {
  groupOperationsByMachine,
  isQueuedStatus,
  isRunningStatus,
  laneOperationLabel,
  ROUTING_ORDER_REASON,
  waitingOnOperation,
} from '../../utils/dispatchPlan';
import type { DispatchLane, DispatchLaneOp, DispatchWorkCenterOption } from '../../types';
import {
  ROLE_PIN_REASON,
  RUNNING_MOVE_REASON,
  RUNNING_PIN_REASON,
  SAVING_REASON,
  TIE_TONE_CLASS,
} from './DispatchMachineView';

const opStatus = (op: DispatchLaneOp) => String(op.status || '').toLowerCase();

/** Someone is on it: an in-progress status or an open labor entry. Pinned to its machine. */
const opIsRunning = (op: DispatchLaneOp) => isRunningStatus(op.status) || (op.crew || []).length > 0;

const crewText = (op: DispatchLaneOp) =>
  (op.crew || [])
    .map(member => (member.display_name || '').trim())
    .filter(Boolean)
    .join(', ');

/**
 * The one-line state of an operation, in the board's vocabulary: complete,
 * running (+ crew), ready with its RUN position at its machine, waiting on an
 * earlier operation, held, or simply pending (not yet released).
 */
export function OperationState({ lane, op }: { lane: DispatchLane; op: DispatchLaneOp }) {
  const status = opStatus(op);
  if (status === 'complete') {
    return (
      <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-green-300">
        <span aria-hidden="true">✓ </span>Complete
      </span>
    );
  }
  if (op.on_hold || status === 'on_hold') {
    return (
      <span className="rounded-sm border border-amber-500/60 px-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-amber-300">
        On hold
      </span>
    );
  }
  if (status === 'in_progress') {
    const crew = crewText(op);
    return (
      <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-blue-300">
        <span aria-hidden="true">▶ </span>Running
        {crew ? <span className="normal-case tracking-normal"> · {crew}</span> : null}
      </span>
    );
  }
  if (status === 'ready') {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-slate-300">
        <span className="font-semibold">Ready</span>
        <KioskRunOrderChip item={{ run_order: op.run_position }} size="sm" />
        {op.work_center_code ? (
          <span className="normal-case tracking-normal text-slate-400">at {op.work_center_code}</span>
        ) : null}
      </span>
    );
  }
  if (op.blocked_by_predecessor) {
    const waitingOn = waitingOnOperation(lane, op);
    return (
      <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
        {waitingOn
          ? `Waiting on ${formatOperationLabel(waitingOn.operation_number, waitingOn.sequence)}`
          : 'Waiting on an earlier operation'}
      </span>
    );
  }
  return (
    <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
      {String(lane.status).toLowerCase() === 'draft' ? 'Not released' : 'Pending'}
    </span>
  );
}

/** A routing lane's operations: a read-only horizontal strip in sequence. */
export function DispatchRoutingStrip({ lane }: { lane: DispatchLane }) {
  return (
    <div className="space-y-1">
      <p className="text-[11px] text-slate-500" data-testid={`lane-routing-note-${lane.work_order_id}`}>
        {ROUTING_ORDER_REASON} — operation order is read-only here.
      </p>
      <ol
        aria-label={`Routing for ${lane.work_order_number}`}
        title={ROUTING_ORDER_REASON}
        className="flex gap-1.5 overflow-x-auto pb-1"
      >
        {lane.operations.map(op => {
          const status = opStatus(op);
          const tone =
            status === 'complete'
              ? 'border-green-500/40'
              : status === 'in_progress'
                ? 'border-blue-400/60 bg-blue-500/5'
                : op.on_hold || status === 'on_hold'
                  ? 'border-amber-500/60'
                  : 'border-fd-line';
          return (
            <li
              key={op.operation_id}
              data-testid={`lane-op-${op.operation_id}`}
              title={ROUTING_ORDER_REASON}
              className={`w-44 shrink-0 rounded-sm border bg-fd-sunken px-2 py-1 ${tone}`}
            >
              <div className="flex items-center gap-1.5">
                <span className="font-mono text-xs font-semibold text-slate-100">
                  {formatOperationLabel(op.operation_number, op.sequence)}
                </span>
                {op.work_center_code && (
                  <span className="truncate font-mono text-[10px] uppercase text-slate-500">{op.work_center_code}</span>
                )}
              </div>
              <p className="truncate text-[11px] text-slate-300">{op.operation_name}</p>
              <OperationState lane={lane} op={op} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export interface DispatchPoolOperationsProps {
  lane: DispatchLane;
  canEdit: boolean;
  /** A dispatch write is in flight: controls stay focusable but inert (`aria-disabled`). */
  busy: boolean;
  /** Why this lane's operation order is read-only right now (not planned / on hold), or null. */
  blockedReason: string | null;
  workCenters: DispatchWorkCenterOption[];
  movingOpIds: readonly number[];
  onReorder: (lane: DispatchLane, operationId: number, toIndex: number, direction?: 'up' | 'down') => void;
  onMoveOperations: (lane: DispatchLane, ops: DispatchLaneOp[], targetWorkCenterId: number) => void;
  onDragActiveChange: (active: boolean) => void;
}

interface OpDrag {
  operationId: number;
  workCenterId: number | null;
}

interface OpDropSlot {
  workCenterId: number | null;
  /** Insertion index among that machine's QUEUED operations. */
  index: number;
}

/** A pool / laser lane's operations, grouped by machine, reorderable within a machine. */
export function DispatchPoolOperations({
  lane,
  canEdit,
  busy,
  blockedReason,
  workCenters,
  movingOpIds,
  onReorder,
  onMoveOperations,
  onDragActiveChange,
}: DispatchPoolOperationsProps) {
  const groups = useMemo(() => groupOperationsByMachine(lane.operations), [lane.operations]);
  const [selected, setSelected] = useState<number[]>([]);
  const [drag, setDrag] = useState<OpDrag | null>(null);
  const [dropSlot, setDropSlot] = useState<OpDropSlot | null>(null);
  const workCenterName = (id: number | null) => workCenters.find(center => center.id === id)?.name ?? null;
  const activeTargets = workCenters.filter(center => center.is_active !== false);
  const selectable = (op: DispatchLaneOp) => canEdit && !opIsRunning(op) && opStatus(op) !== 'complete';
  // A selection only ever names operations that are still movable (a poll can
  // start one under the planner), so the batch never carries a pinned job.
  const selectedOps = lane.operations.filter(op => selected.includes(op.operation_id) && selectable(op));

  const endDrag = () => {
    setDrag(null);
    setDropSlot(null);
    onDragActiveChange(false);
  };

  const queuedAt = (workCenterId: number | null) =>
    lane.operations.filter(op => isQueuedStatus(op.status) && op.work_center_id === workCenterId);

  return (
    <div className="space-y-2">
      {blockedReason ? (
        <p data-testid={`lane-op-order-blocked-${lane.work_order_id}`} className="text-[11px] text-amber-300">
          Operation order is read-only: {blockedReason}
        </p>
      ) : canEdit ? (
        <p className="text-[11px] text-slate-500">
          Order this work order&apos;s operations at each machine — drag, or use Move up / Move down.
        </p>
      ) : null}

      {canEdit && selectedOps.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-sm border border-blue-400/40 bg-blue-500/5 px-2 py-1"
          data-testid={`lane-selection-${lane.work_order_id}`}
        >
          <span className="font-mono text-[11px] text-blue-200">{selectedOps.length} selected</span>
          <select
            aria-label={`Move selected operations of ${lane.work_order_number} to another machine`}
            aria-disabled={busy || undefined}
            value=""
            onChange={event => {
              const targetId = Number(event.target.value);
              event.target.value = '';
              if (busy || !Number.isFinite(targetId) || targetId <= 0) return;
              onMoveOperations(lane, selectedOps, targetId);
              setSelected([]);
            }}
            className="rounded-sm border border-fd-line bg-transparent px-1.5 py-0.5 text-xs text-slate-300 aria-disabled:opacity-40"
          >
            <option value="">Move selected to…</option>
            {activeTargets.map(center => (
              <option key={center.id} value={center.id}>
                {center.code} · {center.name}
              </option>
            ))}
          </select>
          <Button variant="ghost" size="sm" onClick={() => setSelected([])}>
            Clear selection
          </Button>
        </div>
      )}

      {groups.map(group => {
        const queued = queuedAt(group.workCenterId);
        const summary = nestQueueSummary(group.ops);
        const name = workCenterName(group.workCenterId);
        const groupLabel = group.code || name || 'No machine';
        return (
          <section
            key={group.workCenterId ?? 'none'}
            aria-label={`${groupLabel} operations of ${lane.work_order_number}`}
            className="rounded-sm border border-fd-line"
          >
            <header className="flex flex-wrap items-center gap-2 border-b border-fd-line px-2 py-1">
              <span className="font-mono text-xs font-semibold uppercase tracking-wider text-slate-200">
                {groupLabel}
              </span>
              {name && name !== groupLabel && <span className="truncate text-[11px] text-slate-500">{name}</span>}
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
                {queued.length} queued
              </span>
              {summary.nests > 0 && (
                <span
                  className="font-mono text-[10px] uppercase tracking-wider text-slate-400"
                  data-testid={`lane-changeovers-${lane.work_order_id}-${group.workCenterId ?? 'none'}`}
                >
                  {summary.nests} nest{summary.nests === 1 ? '' : 's'} · {summary.changeovers} changeover
                  {summary.changeovers === 1 ? '' : 's'}
                </span>
              )}
            </header>
            <ul className="space-y-1 p-1">
              {group.ops.map((op, index) => {
                const queuedIndex = queued.findIndex(candidate => candidate.operation_id === op.operation_id);
                return (
                  <PoolOperationRow
                    key={op.operation_id}
                    lane={lane}
                    op={op}
                    changeover={index === 0 ? null : nestChangeover(group.ops[index - 1].laser_nest, op.laser_nest)}
                    queuedIndex={queuedIndex}
                    queuedCount={queued.length}
                    canEdit={canEdit}
                    busy={busy}
                    blockedReason={blockedReason}
                    moving={movingOpIds.includes(op.operation_id)}
                    targets={activeTargets.filter(center => center.id !== op.work_center_id)}
                    selectable={selectable(op)}
                    selected={selected.includes(op.operation_id)}
                    onToggleSelected={() =>
                      setSelected(previous =>
                        previous.includes(op.operation_id)
                          ? previous.filter(id => id !== op.operation_id)
                          : [...previous, op.operation_id]
                      )
                    }
                    dropBefore={
                      !!drag && dropSlot?.workCenterId === group.workCenterId && dropSlot.index === queuedIndex
                    }
                    dropAfter={
                      !!drag &&
                      queuedIndex === queued.length - 1 &&
                      dropSlot?.workCenterId === group.workCenterId &&
                      dropSlot.index === queued.length
                    }
                    dragging={drag?.operationId === op.operation_id}
                    onReorder={onReorder}
                    onMove={target => onMoveOperations(lane, [op], target)}
                    onDragStart={event => {
                      // An operation drag is not a lane drag: keep it from
                      // reaching any lane-level handler above.
                      event.stopPropagation();
                      event.dataTransfer.effectAllowed = 'move';
                      event.dataTransfer.setData('text/plain', String(op.operation_id));
                      setDrag({ operationId: op.operation_id, workCenterId: op.work_center_id });
                      onDragActiveChange(true);
                    }}
                    onDragEnd={endDrag}
                    onDragOver={event => {
                      // Only a queued operation at the SAME machine is a slot:
                      // order across machines means nothing to the server.
                      if (!drag || drag.workCenterId !== op.work_center_id || queuedIndex < 0) return;
                      event.preventDefault();
                      event.stopPropagation();
                      event.dataTransfer.dropEffect = 'move';
                      const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
                      const slot = event.clientY < (rect.top + rect.bottom) / 2 ? queuedIndex : queuedIndex + 1;
                      if (!dropSlot || dropSlot.workCenterId !== op.work_center_id || dropSlot.index !== slot) {
                        setDropSlot({ workCenterId: op.work_center_id, index: slot });
                      }
                    }}
                    onDrop={event => {
                      const source = drag;
                      // Not an operation drag (a lane being dragged over this
                      // row): let it reach the plan's own drop surface.
                      if (!source) return;
                      event.preventDefault();
                      event.stopPropagation();
                      const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
                      const slot = event.clientY < (rect.top + rect.bottom) / 2 ? queuedIndex : queuedIndex + 1;
                      endDrag();
                      if (!source || source.workCenterId !== op.work_center_id || queuedIndex < 0) return;
                      const from = queued.findIndex(candidate => candidate.operation_id === source.operationId);
                      if (from < 0) return;
                      onReorder(lane, source.operationId, slot > from ? slot - 1 : slot);
                    }}
                  />
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

interface PoolOperationRowProps {
  lane: DispatchLane;
  op: DispatchLaneOp;
  changeover: ReturnType<typeof nestChangeover>;
  /** Index among this machine's queued operations; -1 when off-queue. */
  queuedIndex: number;
  queuedCount: number;
  canEdit: boolean;
  busy: boolean;
  blockedReason: string | null;
  moving: boolean;
  targets: DispatchWorkCenterOption[];
  selectable: boolean;
  selected: boolean;
  onToggleSelected: () => void;
  dropBefore: boolean;
  dropAfter: boolean;
  dragging: boolean;
  onReorder: DispatchPoolOperationsProps['onReorder'];
  onMove: (targetWorkCenterId: number) => void;
  onDragStart: (event: React.DragEvent) => void;
  onDragEnd: () => void;
  onDragOver: (event: React.DragEvent) => void;
  onDrop: (event: React.DragEvent) => void;
}

function PoolOperationRow({
  lane,
  op,
  changeover,
  queuedIndex,
  queuedCount,
  canEdit,
  busy,
  blockedReason,
  moving,
  targets,
  selectable,
  selected,
  onToggleSelected,
  dropBefore,
  dropAfter,
  dragging,
  onReorder,
  onMove,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDrop,
}: PoolOperationRowProps) {
  const label = laneOperationLabel(op);
  const queued = queuedIndex >= 0;
  const running = opIsRunning(op);
  const complete = opStatus(op) === 'complete';
  const reorderReason = !canEdit
    ? ROLE_PIN_REASON
    : blockedReason
      ? blockedReason
      : running
        ? RUNNING_PIN_REASON
        : busy
          ? SAVING_REASON
          : undefined;
  // Structural reasons really are `disabled`; the in-flight gate is
  // `aria-disabled` + a no-op so a keyboard user is never blurred to <body>.
  const reorderStructural = !canEdit || !!blockedReason || running;
  const moveReason = !canEdit
    ? ROLE_PIN_REASON
    : running
      ? RUNNING_MOVE_REASON
      : complete
        ? 'Completed operations cannot be moved to another work center.'
        : busy
          ? SAVING_REASON
          : undefined;
  const nestSegments = nestDetailSegments(op.laser_nest);
  const tieChip = materialTieChip(op);
  const crew = crewText(op);

  return (
    <li
      data-lane-op={op.operation_id}
      data-testid={`lane-op-${op.operation_id}`}
      tabIndex={-1}
      className={`rounded-sm border outline-none focus-visible:ring-1 focus-visible:ring-blue-400 ${
        running ? 'border-blue-400/60 bg-blue-500/5' : 'border-fd-line bg-fd-sunken'
      } ${dragging || moving ? 'opacity-50' : ''} ${dropBefore ? 'shadow-[inset_0_2px_0_0_#60a5fa]' : ''} ${
        dropAfter ? 'shadow-[inset_0_-2px_0_0_#60a5fa]' : ''
      }`}
    >
      {/* Drag surface (pointer-only; Move up / Move down are the accessible path). */}
      <div
        role="presentation"
        data-testid={`lane-op-drag-${op.operation_id}`}
        className="px-2 py-1"
        draggable={queued && !reorderStructural && !busy}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        onDragOver={queued ? onDragOver : undefined}
        onDrop={queued ? onDrop : undefined}
      >
        {changeover && (
          <div data-testid={`lane-changeover-marker-${op.operation_id}`} className="mb-0.5 flex items-center gap-1.5">
            <span aria-hidden="true" className="font-mono text-[10px] leading-none text-amber-400/80">
              ▸
            </span>
            <span className="whitespace-nowrap font-mono text-[10px] uppercase tracking-wider text-amber-300/90">
              {changeoverLabel(changeover)}
            </span>
            <span aria-hidden="true" className="h-px flex-1 bg-amber-400/30" />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {canEdit && (
            <input
              type="checkbox"
              aria-label={`Select ${label}`}
              checked={selected && selectable}
              disabled={!selectable}
              title={selectable ? undefined : moveReason}
              onChange={onToggleSelected}
              className="h-3.5 w-3.5 accent-blue-500"
            />
          )}
          <span className="font-mono text-xs font-semibold text-slate-100">
            {formatOperationLabel(op.operation_number, op.sequence)}
          </span>
          <span className="min-w-0 max-w-[16rem] truncate text-xs text-slate-300">{op.operation_name}</span>
          {op.component_part_number && (
            <span className="truncate font-mono text-[11px] text-slate-400">
              <span className="font-sans text-slate-500">Component </span>
              {op.component_part_number}
            </span>
          )}
          <OperationState lane={lane} op={op} />
          {crew && !isRunningStatus(op.status) && (
            <span className="font-mono text-[10px] text-blue-300">
              <span aria-hidden="true">▶ </span>
              {crew}
            </span>
          )}
          <span className="font-mono text-[10px] text-slate-500">
            {Number(op.quantity_complete || 0)}/{Number(op.quantity_target || 0)}
          </span>
          {tieChip && (
            <span
              data-testid={`lane-op-tie-${op.operation_id}`}
              title={tieChip.title}
              className={`max-w-[14rem] truncate rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${TIE_TONE_CLASS[tieChip.tone]}`}
            >
              {tieChip.text}
            </span>
          )}
          {moving && <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">Moving…</span>}
          <div className="ml-auto flex items-center gap-1">
            {queued && (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  data-op-action="up"
                  aria-label={`Move ${label} up`}
                  title={reorderReason || `Run earlier at ${op.work_center_code || 'this machine'}`}
                  disabled={reorderStructural || queuedIndex === 0}
                  aria-disabled={busy || undefined}
                  className={busy ? 'opacity-40' : undefined}
                  onClick={() => {
                    if (busy) return;
                    onReorder(lane, op.operation_id, queuedIndex - 1, 'up');
                  }}
                >
                  <ArrowUpIcon className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  data-op-action="down"
                  aria-label={`Move ${label} down`}
                  title={reorderReason || `Run later at ${op.work_center_code || 'this machine'}`}
                  disabled={reorderStructural || queuedIndex === queuedCount - 1}
                  aria-disabled={busy || undefined}
                  className={busy ? 'opacity-40' : undefined}
                  onClick={() => {
                    if (busy) return;
                    onReorder(lane, op.operation_id, queuedIndex + 1, 'down');
                  }}
                >
                  <ArrowDownIcon className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </>
            )}
            {canEdit && (
              <select
                aria-label={`Move ${label} to another machine`}
                title={moveReason || 'Move this operation to another machine'}
                disabled={running || complete || targets.length === 0}
                aria-disabled={busy || undefined}
                value=""
                onChange={event => {
                  const targetId = Number(event.target.value);
                  event.target.value = '';
                  if (busy) return;
                  if (Number.isFinite(targetId) && targetId > 0) onMove(targetId);
                }}
                className="max-w-[10rem] rounded-sm border border-fd-line bg-transparent px-1.5 py-0.5 text-[11px] text-slate-300 disabled:opacity-40 aria-disabled:opacity-40"
              >
                <option value="">Move to machine…</option>
                {targets.map(center => (
                  <option key={center.id} value={center.id}>
                    {center.code} · {center.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
        {nestSegments.length > 0 && (
          <p data-testid={`lane-op-nest-${op.operation_id}`} className="mt-0.5 truncate font-mono text-[11px]">
            {op.laser_nest?.cnc_number ? (
              <span className="text-slate-400">CNC# {op.laser_nest.cnc_number} · </span>
            ) : null}
            {nestSegments.map((segment, segmentIndex) => (
              <React.Fragment key={segment.key}>
                {segmentIndex > 0 && <span className="text-slate-600"> · </span>}
                <span
                  className={
                    segment.key === 'material' || segment.key === 'thickness' ? 'text-slate-200' : 'text-slate-400'
                  }
                >
                  {segment.text}
                </span>
              </React.Fragment>
            ))}
          </p>
        )}
      </div>
    </li>
  );
}

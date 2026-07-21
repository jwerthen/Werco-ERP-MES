/**
 * Dispatch Board — manager-controlled run order.
 *
 * One column per work center; each card is a queued operation. Managers dictate
 * the order operators should run work in by dragging a card up/down within a
 * column (run order) or across columns (move to another machine).
 *
 * Product posture: the run order is ADVISORY. The kiosks sort by it and show the
 * rank, but any job can still be started — the board never blocks the floor.
 *
 * Laser nests: a nest card carries the data an order is actually chosen by —
 * material, thickness, sheet size, sheets left — and each boundary where the
 * material or thickness changes is marked between the cards, with the count
 * summarised in the column header. That makes the COST of an ordering visible
 * (six changeovers vs two) without the board ever reordering anything itself:
 * auto-batching is deliberately out of scope.
 *
 * Optimistic vs server-gated (CLAUDE.md convention):
 *  - Reordering WITHIN a column is rarely rejected -> optimistic via
 *    `useOptimisticMutation`, rolling back with the server's verbatim `detail`.
 *  - Moving ACROSS machines exists precisely because the server may refuse it
 *    (409 while the operation is running/complete, 404 on an inactive work
 *    center) -> NON-optimistic: loading state on the card, and the board only
 *    changes to reflect what the server actually returned.
 *
 * Accessibility: drag is a pointer-only enhancement. Every card also carries real
 * <button> Move up / Move down controls and a labeled machine <select> — the
 * accessible equivalent of both drag axes — and every resulting position is
 * announced through the board's aria-live status line. Because that IS the
 * accessible path, it also has to survive its own success: a move that disables
 * the pressed button re-homes focus inside the moved card rather than letting the
 * browser drop it on <body> (see the focus layout effect), and in-flight gating
 * uses `aria-disabled` instead of `disabled` so no control the user is standing
 * on ever vanishes underneath them.
 */

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDownIcon,
  ArrowPathIcon,
  ArrowUpIcon,
  QueueListIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { usePermissions } from '../hooks/usePermissions';
import { useOptimisticMutation } from '../hooks/useOptimisticMutation';
import { Button, EmptyState, ErrorState, StatusBadge, useToast } from '../components/ui';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../utils/centralTime';
import {
  changeoverLabel,
  nestChangeover,
  nestDetailSegments,
  nestQueueSummary,
  type NestChangeover,
} from '../utils/nestChangeover';
import type { DispatchBoardColumn, DispatchBoardRow, RunOrderUpdateResponse } from '../types';

/** Surface the backend's message verbatim (a refusal must not be reworded). */
function serverDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

/**
 * The run-order PUT answers with that work center's refreshed queue. Accept the
 * bare row array or a wrapped {queue}/column envelope so a shape change on the
 * server can't blank the column we just reordered.
 */
export function extractDispatchQueue(response: RunOrderUpdateResponse | undefined | null): DispatchBoardRow[] | null {
  if (Array.isArray(response)) return response;
  const queue = (response as { queue?: unknown } | null | undefined)?.queue;
  return Array.isArray(queue) ? (queue as DispatchBoardRow[]) : null;
}

/** Pure list move: take the row at `from` and insert it at `to`. */
export function reorderRows(rows: DispatchBoardRow[], from: number, to: number): DispatchBoardRow[] {
  const next = [...rows];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

/** Every row in a column we just ordered becomes ranked 1..N (what the PUT does). */
const withRanks = (rows: DispatchBoardRow[]): DispatchBoardRow[] =>
  rows.map((row, index) => ({ ...row, run_order: index + 1 }));

const replaceQueue = (
  columns: DispatchBoardColumn[],
  workCenterId: number,
  queue: DispatchBoardRow[]
): DispatchBoardColumn[] => columns.map((column) => (column.id === workCenterId ? { ...column, queue } : column));

const isRunning = (row: DispatchBoardRow) => String(row.status).toLowerCase() === 'in_progress';

/** "WO-20260720-001 Nest 1" — how a job is named in labels and announcements. */
export function jobLabel(row: DispatchBoardRow): string {
  const operation = row.operation_name || (row.operation_number != null ? `Op ${row.operation_number}` : 'Operation');
  return `${row.work_order_number} ${operation}`.trim();
}

/**
 * What is actually true of a running job (a tooltip must not claim more than is
 * enforced):
 *  - the SERVER refuses to move an in-progress operation to another work center
 *    (409 "Clock out before moving the operation to another work center");
 *  - the server does NOT refuse to re-rank it — the run-order rewrite accepts a
 *    running operation, and a neighbour's Move up/down really does change a
 *    running job's displayed position;
 *  - so the board's own rule is the weaker one: it won't let YOU pick a running
 *    card up, but the card still shifts as the work around it is reordered.
 */
const RUNNING_PIN_REASON =
  "This job is running, so the board won't pick it up. Its position still shifts as the jobs around it are reordered.";

const RUNNING_MOVE_REASON =
  'This job is running. The server refuses to move an in-progress operation — clock out first.';

interface DragSource {
  operationId: number;
  fromWorkCenterId: number;
}

interface DropSlot {
  workCenterId: number;
  /** Insertion index: the card this drop would land BEFORE (queue.length = end). */
  index: number;
}

/**
 * Insertion index from pointer geometry: the first card whose vertical midpoint
 * is below the pointer, else the tail. Pure so it is unit-testable — jsdom
 * reports zero-size rects, so the geometry can only be exercised directly.
 *
 * This is what stops the gap between two cards from meaning "append to the end
 * of the column": the pointer's position decides the slot, and the tail is only
 * chosen when the pointer really is past the last card's midpoint.
 */
export function insertionIndexFromPointer(rects: Array<{ top: number; bottom: number }>, clientY: number): number {
  for (let index = 0; index < rects.length; index += 1) {
    const { top, bottom } = rects[index];
    if (clientY < (top + bottom) / 2) return index;
  }
  return rects.length;
}

/** Insertion index for a drag event over a column's card list. */
function columnInsertionIndex(container: HTMLElement, clientY: number): number {
  const rects = Array.from(container.querySelectorAll<HTMLElement>('[data-dispatch-card]')).map((card) =>
    card.getBoundingClientRect()
  );
  return insertionIndexFromPointer(rects, clientY);
}

/** Insertion index for a drag event over one card: before it, or after it. */
function cardInsertionIndex(card: HTMLElement, index: number, clientY: number): number {
  const rect = card.getBoundingClientRect();
  return insertionIndexFromPointer([rect], clientY) === 0 ? index : index + 1;
}

interface ReorderCtx {
  /**
   * Monotonic id WITHIN one work center: a late response from a superseded
   * reorder of THIS column must not win. Deliberately per column — a board-wide
   * counter let a reorder of column B discard column A's authoritative
   * reconcile, leaving A on client-side ranks and stale `version`s.
   */
  seq: number;
  workCenterId: number;
  prevQueue: DispatchBoardRow[];
  nextQueue: DispatchBoardRow[];
  label: string;
  fromPosition: number;
  toPosition: number;
  workCenterName: string;
}

export default function DispatchBoard() {
  const { can } = usePermissions();
  const { showToast } = useToast();
  const canEdit = can('work_orders:edit');

  const [columns, setColumns] = useState<DispatchBoardColumn[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A re-read that did not land: the board on screen may not be what the server
  // (and the kiosks) have, so say so instead of looking authoritative.
  const [staleNotice, setStaleNotice] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState('');
  const [dragSource, setDragSource] = useState<DragSource | null>(null);
  const [dropSlot, setDropSlot] = useState<DropSlot | null>(null);
  // Cross-machine moves are server-gated -> the card shows in-flight state and
  // the board waits for the server before changing.
  const [movingOperationId, setMovingOperationId] = useState<number | null>(null);
  // Work centers with a reorder in flight: a second reorder of the same column
  // can't stack on an unresolved one.
  const [reorderingColumnIds, setReorderingColumnIds] = useState<readonly number[]>([]);
  // PER WORK CENTER monotonic counter — guards reconcile AND rollback against an
  // out-of-order response, without one column's reorder invalidating another's.
  const reorderSeqRef = useRef<Map<number, number>>(new Map());
  // Where focus must land once a keyboard reorder commits (the pressed button
  // can be disabled by the move itself — see focusAfterReorder below).
  const pendingFocusRef = useRef<{ operationId: number; direction: 'up' | 'down' } | null>(null);
  const boardRef = useRef<HTMLDivElement | null>(null);

  const nextReorderSeq = useCallback((workCenterId: number) => {
    const seq = (reorderSeqRef.current.get(workCenterId) || 0) + 1;
    reorderSeqRef.current.set(workCenterId, seq);
    return seq;
  }, []);

  const isCurrentReorder = useCallback(
    (ctx: ReorderCtx) => reorderSeqRef.current.get(ctx.workCenterId) === ctx.seq,
    []
  );

  /** Resolves false when the re-read failed — callers must not report success then. */
  const load = useCallback(async (options?: { silent?: boolean }): Promise<boolean> => {
    if (options?.silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const data = await api.getDispatchBoard();
      setColumns(data?.work_centers || []);
      setError(null);
      setStaleNotice(null);
      return true;
    } catch (err) {
      const detail = serverDetail(err, 'Could not load the dispatch board.');
      setError(detail);
      setStaleNotice(detail);
      return false;
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const totals = useMemo(() => {
    const all = (columns || []).flatMap((column) => column.queue);
    return {
      machines: (columns || []).length,
      jobs: all.length,
      ranked: all.filter((row) => row.run_order != null).length,
    };
  }, [columns]);

  // --- Reorder within a column (OPTIMISTIC) ---------------------------------
  const { run: runReorder } = useOptimisticMutation<RunOrderUpdateResponse, ReorderCtx>({
    applyOptimistic: (ctx) => {
      setColumns((prev) => (prev ? replaceQueue(prev, ctx.workCenterId, ctx.nextQueue) : prev));
      setStatusMessage(
        `${ctx.label} moved to position ${ctx.toPosition} of ${ctx.nextQueue.length} on ${ctx.workCenterName}`
      );
    },
    rollback: (ctx) => {
      // Guarded like reconcile: a newer reorder of THIS column owns the state now.
      if (isCurrentReorder(ctx)) {
        setColumns((prev) => (prev ? replaceQueue(prev, ctx.workCenterId, ctx.prevQueue) : prev));
        setStatusMessage(
          `Run order change refused. ${ctx.label} is back at position ${ctx.fromPosition} on ${ctx.workCenterName}. Re-reading the board.`
        );
      }
      // The snapshot we just restored is a CLIENT guess and may itself be stale
      // (the refusal often means the board no longer matches the server). Re-read
      // rather than sit on it; a failed re-read raises the stale banner.
      void load({ silent: true });
    },
    mutate: (ctx) =>
      api.setWorkCenterRunOrder(
        ctx.workCenterId,
        ctx.nextQueue.map((row) => row.operation_id)
      ),
    reconcile: (result, ctx) => {
      if (!isCurrentReorder(ctx)) return; // superseded by a newer reorder OF THIS COLUMN
      const queue = extractDispatchQueue(result);
      if (queue) setColumns((prev) => (prev ? replaceQueue(prev, ctx.workCenterId, queue) : prev));
    },
    errorFallback: 'Failed to save the run order',
  });

  const reorderingColumn = useCallback(
    (workCenterId: number) => reorderingColumnIds.includes(workCenterId),
    [reorderingColumnIds]
  );

  /**
   * Move a card to `toIndex` (a final position, not an insertion point).
   *
   * `direction` is set only by the keyboard controls — it tells the focus effect
   * which button the user pressed, so focus can be placed deliberately once the
   * move commits instead of being dropped on `document.body`.
   */
  const moveWithinColumn = useCallback(
    (workCenterId: number, operationId: number, toIndex: number, direction?: 'up' | 'down') => {
      if (!canEdit || !columns) return;
      // One unresolved reorder per column: stacking a second one would race the
      // first's reconcile and rollback against each other.
      if (reorderingColumn(workCenterId) || movingOperationId != null) return;
      const column = columns.find((candidate) => candidate.id === workCenterId);
      if (!column) return;
      const fromIndex = column.queue.findIndex((row) => row.operation_id === operationId);
      if (fromIndex < 0) return;
      const row = column.queue[fromIndex];
      if (isRunning(row)) {
        showToast('error', RUNNING_PIN_REASON);
        return;
      }
      const clamped = Math.max(0, Math.min(column.queue.length - 1, toIndex));
      if (clamped === fromIndex) return;

      if (direction) pendingFocusRef.current = { operationId, direction };
      setReorderingColumnIds((prev) => (prev.includes(workCenterId) ? prev : [...prev, workCenterId]));
      void runReorder({
        seq: nextReorderSeq(workCenterId),
        workCenterId,
        workCenterName: column.name,
        prevQueue: column.queue,
        nextQueue: withRanks(reorderRows(column.queue, fromIndex, clamped)),
        label: jobLabel(row),
        fromPosition: fromIndex + 1,
        toPosition: clamped + 1,
      }).finally(() => {
        setReorderingColumnIds((prev) => prev.filter((id) => id !== workCenterId));
      });
    },
    [canEdit, columns, movingOperationId, nextReorderSeq, reorderingColumn, runReorder, showToast]
  );

  /**
   * Keep focus inside the card the user just moved.
   *
   * Moving a card to the first/last position disables the very button that was
   * pressed, and a browser blurs a control the moment it becomes disabled — the
   * keyboard user would land on `<body>`, at the top of the page, mid-task. So
   * after every committed keyboard reorder focus is placed explicitly: the same
   * control if it survived, otherwise its sibling, otherwise the card itself
   * (which carries `tabIndex={-1}` for exactly this).
   */
  useLayoutEffect(() => {
    const pending = pendingFocusRef.current;
    if (!pending) return;
    pendingFocusRef.current = null;
    const card = boardRef.current?.querySelector<HTMLElement>(`[data-dispatch-card="${pending.operationId}"]`);
    if (!card) return;
    const button = (which: 'up' | 'down') => card.querySelector<HTMLButtonElement>(`[data-move="${which}"]`);
    const pressed = button(pending.direction);
    const sibling = button(pending.direction === 'up' ? 'down' : 'up');
    const target = pressed && !pressed.disabled ? pressed : sibling && !sibling.disabled ? sibling : card;
    target.focus();
  }, [columns]);

  // --- Move across machines (NON-optimistic: the server may refuse) ---------
  const moveToWorkCenter = useCallback(
    async (row: DispatchBoardRow, targetWorkCenterId: number) => {
      if (!canEdit || movingOperationId != null) return;
      const target = (columns || []).find((column) => column.id === targetWorkCenterId);
      if (!target) return;
      const label = jobLabel(row);

      setMovingOperationId(row.operation_id);
      try {
        await api.updateOperation(row.operation_id, {
          work_center_id: targetWorkCenterId,
          version: row.version,
        });
        // Only now does the board change — re-read the authoritative board. The
        // move succeeded server-side, but until the re-read lands the card is
        // still drawn in its old column: reporting success then would describe a
        // board the user is not looking at.
        const refreshed = await load({ silent: true });
        if (!refreshed) {
          showToast(
            'error',
            `${label} moved to ${target.name} on the server, but the board could not be re-read. Refresh to see where it is.`
          );
          setStatusMessage(
            `${label} moved to ${target.name}, but the board could not be re-read and may be out of date.`
          );
          return;
        }
        showToast('success', `${label} moved to ${target.name}.`);
        setStatusMessage(`${label} moved to ${target.name}. It is unranked at the end of that queue.`);
      } catch (err) {
        const detail = serverDetail(err, 'Failed to move this job to another machine');
        showToast('error', detail);
        setStatusMessage(`${label} was not moved. ${detail}`);
      } finally {
        setMovingOperationId(null);
      }
    },
    [canEdit, columns, load, movingOperationId, showToast]
  );

  // --- Drag (pointer-only enhancement) -------------------------------------
  const handleDragStart = (e: React.DragEvent, row: DispatchBoardRow, workCenterId: number) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(row.operation_id));
    setDragSource({ operationId: row.operation_id, fromWorkCenterId: workCenterId });
  };

  const handleDragEnd = () => {
    setDragSource(null);
    setDropSlot(null);
  };

  const markDropSlot = (workCenterId: number, index: number) => {
    if (!dropSlot || dropSlot.workCenterId !== workCenterId || dropSlot.index !== index) {
      setDropSlot({ workCenterId, index });
    }
  };

  /**
   * Dragging over a CARD: the slot is decided by which half of the card the
   * pointer is in — above the midpoint inserts before it, below inserts after.
   */
  const handleCardDragOver = (e: React.DragEvent, workCenterId: number, index: number) => {
    if (!dragSource) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    markDropSlot(workCenterId, cardInsertionIndex(e.currentTarget as HTMLElement, index, e.clientY));
  };

  /**
   * Dragging over the COLUMN's surface — the 8px gaps between cards and the
   * padding around them. This used to hard-code "append to the end", so a
   * manager aiming between card 1 and card 2 and releasing in the gap silently
   * sent the job to the bottom. The slot now comes from pointer geometry, so the
   * tail is only chosen when the pointer really is past the last card.
   */
  const handleColumnDragOver = (e: React.DragEvent, workCenterId: number) => {
    if (!dragSource) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    markDropSlot(workCenterId, columnInsertionIndex(e.currentTarget as HTMLElement, e.clientY));
  };

  // Only clear when the pointer leaves the zone entirely — entering a child
  // would otherwise flicker the highlight off (Scheduling.tsx precedent).
  const handleDragLeave = (e: React.DragEvent) => {
    const related = e.relatedTarget as Node | null;
    if (!related || !e.currentTarget.contains(related)) {
      setDropSlot(null);
    }
  };

  const handleDrop = (e: React.DragEvent, workCenterId: number, insertIndex: number) => {
    e.preventDefault();
    e.stopPropagation();
    const source = dragSource;
    setDragSource(null);
    setDropSlot(null);
    if (!source) return;

    if (source.fromWorkCenterId === workCenterId) {
      const column = (columns || []).find((candidate) => candidate.id === workCenterId);
      if (!column) return;
      const fromIndex = column.queue.findIndex((row) => row.operation_id === source.operationId);
      if (fromIndex < 0) return;
      // Insertion index -> final index: removing the row first shifts later slots.
      moveWithinColumn(workCenterId, source.operationId, insertIndex > fromIndex ? insertIndex - 1 : insertIndex);
      return;
    }

    const sourceColumn = (columns || []).find((candidate) => candidate.id === source.fromWorkCenterId);
    const row = sourceColumn?.queue.find((candidate) => candidate.operation_id === source.operationId);
    if (row) moveToWorkCenter(row, workCenterId);
  };

  // Both drops recompute the slot from the release point rather than trusting the
  // last `dragover` state, so what lands is what the drop line showed.
  const handleCardDrop = (e: React.DragEvent, workCenterId: number, index: number) =>
    handleDrop(e, workCenterId, cardInsertionIndex(e.currentTarget as HTMLElement, index, e.clientY));

  const handleColumnDrop = (e: React.DragEvent, workCenterId: number) =>
    handleDrop(e, workCenterId, columnInsertionIndex(e.currentTarget as HTMLElement, e.clientY));

  if (loading && !columns) {
    return (
      <div className="space-y-3" data-testid="dispatch-loading">
        <div className="skeleton-title w-56" />
        <div className="flex gap-3">
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="skeleton h-80 w-80" />
          ))}
        </div>
      </div>
    );
  }

  if (error && !columns) {
    return (
      <div className="space-y-3">
        <BoardHeader
          totals={totals}
          statusMessage={statusMessage}
          onRefresh={() => load({ silent: true })}
          refreshing={refreshing}
          canEdit={canEdit}
          // The full ErrorState below already owns the failure here.
          staleNotice={null}
        />
        <ErrorState title="Couldn't load the dispatch board" message={error} onRetry={() => load()} />
      </div>
    );
  }

  const boardColumns = columns || [];

  return (
    <div className="space-y-3">
      <BoardHeader
        totals={totals}
        statusMessage={statusMessage}
        onRefresh={() => load({ silent: true })}
        refreshing={refreshing}
        canEdit={canEdit}
        staleNotice={staleNotice}
      />

      {boardColumns.length === 0 ? (
        <EmptyState
          icon={QueueListIcon}
          title="No machines to dispatch"
          description="Released work assigned to an active work center shows up here as a run-order column."
        />
      ) : (
        // The BOARD scrolls sideways, never the page body.
        <div className="overflow-x-auto pb-2" data-testid="dispatch-board-scroll" ref={boardRef}>
          <div className="flex min-w-max items-start gap-3">
            {boardColumns.map((column) => {
              const empty = column.queue.length === 0;
              const columnTargeted = dropSlot?.workCenterId === column.id;
              // Recomputed from the queue on every render, so an optimistic
              // reorder updates the cost of the order at the same moment the
              // cards move — that immediacy is the whole feedback loop.
              const nestSummary = nestQueueSummary(column.queue);
              return (
                <section
                  key={column.id}
                  aria-label={`${column.name} run order`}
                  className={`flex w-80 shrink-0 flex-col rounded-sm border ${
                    empty ? 'border-slate-800/80 bg-fd-panel/40' : 'border-fd-line bg-fd-panel'
                  } ${columnTargeted ? 'ring-1 ring-blue-400/70' : ''}`}
                >
                  <header className="flex items-center justify-between gap-2 border-b border-fd-line px-3 py-2">
                    <div className="min-w-0">
                      <p className={`truncate text-sm font-semibold ${empty ? 'text-slate-400' : 'text-slate-100'}`}>
                        {column.name}
                      </p>
                      <p className="truncate font-mono text-[11px] uppercase tracking-widest text-slate-500">
                        {column.code}
                        {column.work_center_type ? ` · ${String(column.work_center_type).replace(/_/g, ' ')}` : ''}
                      </p>
                      {nestSummary.nests > 0 && (
                        <p
                          data-testid={`dispatch-changeovers-${column.id}`}
                          className="truncate font-mono text-[11px] text-slate-400"
                        >
                          {nestSummary.nests} nest{nestSummary.nests === 1 ? '' : 's'} · {nestSummary.changeovers}{' '}
                          changeover{nestSummary.changeovers === 1 ? '' : 's'}
                        </p>
                      )}
                    </div>
                    <span
                      className={`shrink-0 rounded border px-2 py-0.5 font-mono text-xs ${
                        empty ? 'border-slate-800 text-slate-500' : 'border-fd-line text-slate-300'
                      }`}
                    >
                      {column.queue.length}
                    </span>
                  </header>

                  {/* Drop surface. Plain div: the accessible equivalent of dropping
                      here is each card's Move up/down buttons + machine select, so
                      the surface itself is presentational (POUpload.tsx precedent). */}
                  <div
                    role="presentation"
                    data-testid={`dispatch-column-${column.id}`}
                    className="flex min-h-[7rem] flex-1 flex-col gap-2 p-2"
                    onDragOver={(e) => handleColumnDragOver(e, column.id)}
                    onDragLeave={handleDragLeave}
                    onDrop={(e) => handleColumnDrop(e, column.id)}
                  >
                    {empty ? (
                      <p className="px-1 py-6 text-center text-xs text-slate-500">
                        Idle — no queued work. Drag a job here (or use a card&apos;s machine select) to move it to
                        this machine.
                      </p>
                    ) : (
                      <>
                        {column.queue.map((row, index) => (
                          <React.Fragment key={row.operation_id}>
                            <DropLine columnId={column.id} index={index} dropSlot={dropSlot} />
                            <ChangeoverMarker
                              operationId={row.operation_id}
                              // Only a nest-to-nest boundary is a changeover; the
                              // helper returns null for the first card and for any
                              // pair where either side isn't a nest.
                              kind={
                                index === 0 ? null : nestChangeover(column.queue[index - 1].laser_nest, row.laser_nest)
                              }
                            />
                            <DispatchCard
                              row={row}
                              index={index}
                              column={column}
                              columns={boardColumns}
                              canEdit={canEdit}
                              moving={movingOperationId === row.operation_id}
                              moveDisabled={movingOperationId != null}
                              reorderBusy={reorderingColumn(column.id)}
                              dragging={dragSource?.operationId === row.operation_id}
                              onDragStart={handleDragStart}
                              onDragEnd={handleDragEnd}
                              onDragOver={handleCardDragOver}
                              onDragLeave={handleDragLeave}
                              onDrop={handleCardDrop}
                              onMove={moveWithinColumn}
                              onMoveToWorkCenter={moveToWorkCenter}
                            />
                          </React.Fragment>
                        ))}
                        {/* The tail slot gets a line of its own — "goes to the
                            bottom" must be something the manager SEES before
                            releasing, never a silent consequence of the gap. */}
                        <DropLine columnId={column.id} index={column.queue.length} dropSlot={dropSlot} />
                      </>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The visible landing slot. Rendered between cards (and after the last one) so
 * the drop target — including the tail — is always shown before release.
 */
function DropLine({ columnId, index, dropSlot }: { columnId: number; index: number; dropSlot: DropSlot | null }) {
  const active = dropSlot?.workCenterId === columnId && dropSlot.index === index;
  return (
    <div
      aria-hidden="true"
      data-testid={active ? `dispatch-drop-line-${columnId}-${index}` : undefined}
      className={`-my-1 h-0.5 rounded-sm ${active ? 'bg-blue-400' : 'bg-transparent'}`}
    />
  );
}

/**
 * The setup a boundary costs, drawn between the two cards that cause it.
 *
 * Purely presentational: the glyph and the rule are `aria-hidden`, but the words
 * stay in the accessible name of the column's content so a screen-reader user
 * hears "material change" in position between the two jobs. It is deliberately
 * not focusable and carries no interactive role — there is nothing to activate;
 * the fix for a changeover is to reorder the cards around it.
 */
function ChangeoverMarker({ operationId, kind }: { operationId: number; kind: NestChangeover | null }) {
  if (!kind) return null;
  return (
    <div data-testid={`dispatch-changeover-marker-${operationId}`} className="-my-0.5 flex items-center gap-1.5 px-1">
      <span aria-hidden="true" className="font-mono text-[10px] leading-none text-amber-400/80">
        ▸
      </span>
      <span className="whitespace-nowrap font-mono text-[10px] uppercase tracking-wider text-amber-300/90">
        {changeoverLabel(kind)}
      </span>
      <span aria-hidden="true" className="h-px flex-1 bg-amber-400/30" />
    </div>
  );
}

interface BoardHeaderProps {
  totals: { machines: number; jobs: number; ranked: number };
  statusMessage: string;
  onRefresh: () => void;
  refreshing: boolean;
  canEdit: boolean;
  /** Set when a re-read failed: what's on screen may not be what the server has. */
  staleNotice: string | null;
}

function BoardHeader({ totals, statusMessage, onRefresh, refreshing, canEdit, staleNotice }: BoardHeaderProps) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold text-white">Dispatch Board</h1>
          <p className="mt-0.5 text-sm text-slate-400">
            {totals.jobs} queued job{totals.jobs === 1 ? '' : 's'} across {totals.machines} machine
            {totals.machines === 1 ? '' : 's'} · {totals.ranked} ranked ·{' '}
            <span className="text-slate-500">
              run order is advisory — operators see the rank but can still start any job
            </span>
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={onRefresh} disabled={refreshing}>
          <ArrowPathIcon className={`mr-1.5 inline h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
      {!canEdit && (
        <p className="rounded-sm border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
          You can view the run order but not change it.
        </p>
      )}
      {staleNotice && (
        <div
          role="alert"
          data-testid="dispatch-stale-notice"
          className="flex flex-wrap items-center gap-2 rounded-sm border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-300"
        >
          <span>This board may be out of date — the last refresh didn&apos;t land. {staleNotice}</span>
          <Button variant="secondary" size="sm" onClick={onRefresh} disabled={refreshing}>
            Retry refresh
          </Button>
        </div>
      )}
      <p
        role="status"
        aria-live="polite"
        data-testid="dispatch-status"
        className="min-h-[1.25rem] font-mono text-xs text-slate-400"
      >
        {statusMessage}
      </p>
    </div>
  );
}

interface DispatchCardProps {
  row: DispatchBoardRow;
  index: number;
  column: DispatchBoardColumn;
  columns: DispatchBoardColumn[];
  canEdit: boolean;
  moving: boolean;
  moveDisabled: boolean;
  /** A reorder of THIS column is unresolved — don't let another one stack on it. */
  reorderBusy: boolean;
  dragging: boolean;
  onDragStart: (e: React.DragEvent, row: DispatchBoardRow, workCenterId: number) => void;
  onDragEnd: () => void;
  onDragOver: (e: React.DragEvent, workCenterId: number, index: number) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent, workCenterId: number, index: number) => void;
  onMove: (workCenterId: number, operationId: number, toIndex: number, direction?: 'up' | 'down') => void;
  onMoveToWorkCenter: (row: DispatchBoardRow, workCenterId: number) => void;
}

function DispatchCard({
  row,
  index,
  column,
  columns,
  canEdit,
  moving,
  moveDisabled,
  reorderBusy,
  dragging,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDragLeave,
  onDrop,
  onMove,
  onMoveToWorkCenter,
}: DispatchCardProps) {
  const running = isRunning(row);
  const label = jobLabel(row);
  const pinned = running || !canEdit;
  // In-flight gating is `aria-disabled`, NOT `disabled`: a control that becomes
  // `disabled` under the user's finger is blurred by the browser, which is how a
  // keyboard reorder used to throw focus to <body>. The click handler no-ops
  // instead, so a second reorder still can't stack on an unresolved one.
  const busy = reorderBusy || moveDisabled;
  const disabledReason = running
    ? RUNNING_PIN_REASON
    : !canEdit
      ? 'Your role cannot change the run order.'
      : busy
        ? 'Waiting for the last change to save…'
        : undefined;
  const pastDue = row.due_date ? isDateBeforeTodayInCentral(row.due_date) : false;
  const dueToday = row.due_date ? isDateTodayInCentral(row.due_date) : false;
  const otherMachines = columns.filter((candidate) => candidate.id !== column.id);
  const nestSegments = nestDetailSegments(row.laser_nest);

  return (
    <div
      role="presentation"
      data-testid={`dispatch-card-${row.operation_id}`}
      data-dispatch-card={row.operation_id}
      // Focus fallback for the reorder focus effect: if BOTH move buttons end up
      // disabled by the move, focus lands on the card rather than on <body>.
      tabIndex={-1}
      draggable={!pinned && !busy}
      onDragStart={(e) => onDragStart(e, row, column.id)}
      onDragEnd={onDragEnd}
      onDragOver={(e) => onDragOver(e, column.id, index)}
      onDragLeave={onDragLeave}
      onDrop={(e) => onDrop(e, column.id, index)}
      className={`rounded-sm border px-2.5 py-2 outline-none transition-colors focus-visible:ring-1 focus-visible:ring-blue-400 ${
        running ? 'border-blue-400/60 bg-blue-500/5' : 'border-fd-line bg-fd-sunken'
      } ${dragging ? 'opacity-50' : ''} ${moving ? 'opacity-60' : ''}`}
    >
      <div className="flex items-start gap-2">
        <span
          data-testid={`dispatch-rank-${row.operation_id}`}
          className={`mt-0.5 w-7 shrink-0 text-center font-mono text-xl font-bold leading-none tabular-nums ${
            row.run_order == null ? 'text-slate-600' : 'text-white'
          }`}
          title={row.run_order == null ? 'Unranked — runs after every ranked job' : `Run order ${row.run_order}`}
        >
          {row.run_order == null ? '–' : row.run_order}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-sm font-semibold text-slate-100">{row.work_order_number}</span>
            {running ? (
              <span className="rounded border border-blue-400/60 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-blue-300">
                Running
              </span>
            ) : (
              <StatusBadge status={row.status} />
            )}
          </div>
          <p className="truncate text-xs text-slate-300">
            {row.operation_number != null ? `Op ${row.operation_number} · ` : ''}
            {row.operation_name || 'Operation'}
          </p>
          {row.part_number && (
            <p className="truncate font-mono text-[11px] text-slate-400">
              {row.part_number}
              {row.part_name ? <span className="font-sans"> · {row.part_name}</span> : null}
            </p>
          )}
          {/* One dense line — material and thickness carry the sequencing weight,
              so they read brighter than the sheet size and the sheets left. The
              card must stay short enough that a column still shows a queue. */}
          {nestSegments.length > 0 && (
            <p data-testid={`dispatch-nest-${row.operation_id}`} className="truncate font-mono text-[11px]">
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
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {row.due_date && (
              <span
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                  pastDue
                    ? 'border-red-500/60 text-red-300'
                    : dueToday
                      ? 'border-amber-500/60 text-amber-300'
                      : 'border-fd-line text-slate-400'
                }`}
              >
                {pastDue ? 'Past due ' : dueToday ? 'Due today' : 'Due '}
                {dueToday ? '' : formatCentralDate(row.due_date)}
              </span>
            )}
            <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
              {Number(row.quantity_complete || 0)}/{Number(row.quantity_ordered || 0)} pcs
            </span>
          </div>
        </div>
      </div>

      <div className="mt-2 flex items-center gap-1 border-t border-fd-line pt-2">
        <Button
          variant="ghost"
          size="sm"
          data-move="up"
          aria-label={`Move ${label} up`}
          title={disabledReason || 'Move up one position'}
          disabled={pinned || index === 0}
          aria-disabled={busy || undefined}
          className={busy ? 'opacity-40' : undefined}
          onClick={() => {
            if (busy) return;
            onMove(column.id, row.operation_id, index - 1, 'up');
          }}
        >
          <ArrowUpIcon className="h-4 w-4" aria-hidden="true" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          data-move="down"
          aria-label={`Move ${label} down`}
          title={disabledReason || 'Move down one position'}
          disabled={pinned || index === column.queue.length - 1}
          aria-disabled={busy || undefined}
          className={busy ? 'opacity-40' : undefined}
          onClick={() => {
            if (busy) return;
            onMove(column.id, row.operation_id, index + 1, 'down');
          }}
        >
          <ArrowDownIcon className="h-4 w-4" aria-hidden="true" />
        </Button>
        <select
          aria-label={`Move ${label} to another machine`}
          title={(running ? RUNNING_MOVE_REASON : disabledReason) || 'Move this job to another machine'}
          // Structural reasons (running job, nowhere to move it) really are
          // `disabled`; the in-flight gate is `aria-disabled` + a no-op, so a
          // keyboard user holding this control isn't blurred to <body> the
          // instant their own move starts -- same rule as the Move buttons.
          disabled={pinned || otherMachines.length === 0}
          aria-disabled={busy || undefined}
          value=""
          onChange={(e) => {
            const targetId = Number(e.target.value);
            e.target.value = '';
            if (busy) return;
            if (Number.isFinite(targetId) && targetId > 0) onMoveToWorkCenter(row, targetId);
          }}
          className="ml-auto min-w-0 flex-1 rounded-sm border border-fd-line bg-transparent px-1.5 py-1 text-xs text-slate-300 disabled:opacity-40 aria-disabled:opacity-40"
        >
          <option value="">Move to machine…</option>
          {otherMachines.map((machine) => (
            <option key={machine.id} value={machine.id}>
              {machine.name}
            </option>
          ))}
        </select>
      </div>

      {(running || moving) && (
        <p className="mt-1 text-[10px] uppercase tracking-wider text-slate-500">
          {moving ? 'Moving…' : 'Held in place while running'}
        </p>
      )}
    </div>
  );
}

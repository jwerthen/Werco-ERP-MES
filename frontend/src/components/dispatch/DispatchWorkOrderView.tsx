/**
 * Dispatch Board — the WORK ORDERS view (`/dispatch`, the default).
 *
 * The shop-wide PLAN: one compact lane per live work order (DRAFT included —
 * planning a draft never releases it; Release is still the authorization step).
 * Every machine queue follows this order, and inside a planned pool / laser work
 * order the planner orders that work order's operations. Two sections:
 *
 *  - **Plan** — planned lanes in plan order (`dispatch_position` 1..N). Reorder
 *    by native drag or by keyboard: Move up / Move down / Move to top /
 *    Set position… / Remove from plan.
 *  - **Not in plan** — every other live work order, in the server's order
 *    (priority, promise date, number). "Add to plan" appends one; "Add all by
 *    promise date" appends the shown ones, earliest promise first.
 *
 * Writes, and which are optimistic (CLAUDE.md convention):
 *  - The PLAN write (`PUT …/work-order-order`) is rarely refused, so it is
 *    OPTIMISTIC via `useOptimisticMutation`: the lanes move at once; the PUT
 *    carries the full desired plan plus the plan exactly as rendered (the CAS
 *    list); its response is the whole board and replaces the guess. A refusal
 *    (409 the board changed, 400 an unknown id) rolls back, shows the server's
 *    `detail` verbatim, and re-reads.
 *  - The OPERATION-order write (`PUT …/work-orders/{id}/operation-order`) is the
 *    same shape, one lane wide. It is never sent for a routing lane, a lane not
 *    in the plan, or a held lane: the control is disabled with the server's own
 *    reason instead.
 *  - A MACHINE MOVE (`updateOperation` with `version`) exists precisely because
 *    the server may refuse it (running / completed work, a stale version), so it
 *    is NON-optimistic: nothing moves on screen until the re-read lands.
 * Only one write is in flight at a time — each later CAS list would otherwise be
 * stale by construction — and every control is `aria-disabled` while it is.
 *
 * Freshness: polls every 10 s while the tab is visible, and on focus; a poll is
 * deferred while a drag, a write or a plan dialog is under way and runs once it
 * ends. A write also drops every read already in flight (a manual Refresh
 * included), so nothing answered before the write can land after it. Polls are
 * silent. The aria-live status line speaks only for the user's own actions,
 * refusals and failures.
 *
 * Deploy window: merging to main auto-deploys, so for a minute this SPA can talk
 * to the previous API. A 404 (or a body without lanes) renders the retryable
 * ErrorState and points at the Machines view; it never crashes the page.
 */

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowPathIcon, QueueListIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';
import { useOptimisticMutation } from '../../hooks/useOptimisticMutation';
import { Button, ConfirmDialog, EmptyState, ErrorState, InputDialog, Skeleton, useToast } from '../ui';
import { formatCentralTime } from '../../utils/centralTime';
import {
  applyPlanToBoard,
  DISPATCH_POLL_MS,
  dispatchErrorDetail,
  dispatchErrorStatus,
  hasAnyFilter,
  isLanePayload,
  isLanesPayload,
  isRunningStatus,
  laneIsRunning,
  laneMatchesFilters,
  laneOperationLabel,
  moveIdAfter,
  moveIdBefore,
  moveIdToIndex,
  NO_DISPATCH_FILTERS,
  operationOrderBlockedReason,
  placeAtVisibleSlot,
  planIdsOf,
  promiseTone,
  reorderLaneOperation,
  replaceLane,
  sameIds,
  sortLanesByPromiseDate,
  type DispatchFilters,
} from '../../utils/dispatchPlan';
import type { DispatchLane, DispatchLaneOp, DispatchWorkOrderBoardResponse } from '../../types';
import DispatchLaneRow from './DispatchLaneRow';
import { insertionIndexFromPointer, RUNNING_PIN_REASON } from './DispatchMachineView';
import type { DispatchMachineOption } from './DispatchFilterBar';

type Board = DispatchWorkOrderBoardResponse;
type PendingAction = 'plan' | 'ops' | 'move' | null;

interface PlanCtx {
  /** Monotonic: a late answer from a superseded plan write must not win. */
  seq: number;
  prevBoard: Board;
  nextIds: number[];
  /** The plan exactly as rendered (CAS). */
  expectedIds: number[];
  announce: string;
}

interface OpsCtx {
  seq: number;
  laneId: number;
  prevLane: DispatchLane;
  nextLane: DispatchLane;
  operationIds: number[];
  expectedIds: number[];
  announce: string;
}

/**
 * The "Set position… / Add at position…" dialog. Its default is FROZEN when it
 * opens: the dialog re-seeds its field whenever `defaultValue` changes, so a
 * default computed live from the plan length would overwrite what the planner
 * typed the moment a read changed the plan. The 1..max range check at submit
 * stays live.
 */
interface PositionDialog {
  lane: DispatchLane;
  defaultValue: string;
}

interface PendingFocus {
  laneId: number;
  /** Set for an operation-level control inside an expanded lane. */
  operationId?: number;
  /** Controls to try, in order; falls back to the lane / operation row itself. */
  actions: string[];
}

/** A missing lanes route (404) means the API is older than this page — say so, don't say "broken". */
const OLD_API_DETAIL =
  'The work-order plan is not available from the server yet — it may be mid-update. Retry in a minute, or use the Machines view meanwhile.';

function describeLoadError(err: unknown): string {
  if (dispatchErrorStatus(err) === 404) return OLD_API_DETAIL;
  return dispatchErrorDetail(err, 'Could not load the dispatch plan.');
}

export interface DispatchWorkOrderViewProps {
  filters?: DispatchFilters;
  onMachineOptions?: (options: DispatchMachineOption[]) => void;
}

export default function DispatchWorkOrderView({
  filters = NO_DISPATCH_FILTERS,
  onMachineOptions,
}: DispatchWorkOrderViewProps = {}) {
  const { can } = usePermissions();
  const { showToast } = useToast();
  const canEdit = can('work_orders:edit');

  const [board, setBoard] = useState<Board | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [staleNotice, setStaleNotice] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<string | null>(null);
  const [updatesDeferred, setUpdatesDeferred] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [movingOpIds, setMovingOpIds] = useState<number[]>([]);
  const [expandedIds, setExpandedIds] = useState<number[]>([]);
  const [dragLaneId, setDragLaneId] = useState<number | null>(null);
  const [laneDropSlot, setLaneDropSlot] = useState<number | null>(null);
  const [opDragActive, setOpDragActive] = useState(false);
  const [positionDialog, setPositionDialog] = useState<PositionDialog | null>(null);
  const [addAllLanes, setAddAllLanes] = useState<DispatchLane[] | null>(null);

  const requestRef = useRef(0);
  const interactionRef = useRef(false);
  const epochRef = useRef(0);
  const planSeqRef = useRef(0);
  const opsSeqRef = useRef(0);
  const pendingFocusRef = useRef<PendingFocus | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const planListRef = useRef<HTMLOListElement | null>(null);

  // An open plan dialog counts too: a poll that lands while the planner is
  // typing a position (or reading the add-all confirmation) would change the
  // plan the answer is about. It runs once the dialog closes.
  const planDialogOpen = positionDialog != null || addAllLanes != null;
  const interacting = pendingAction != null || dragLaneId != null || opDragActive || planDialogOpen;
  interactionRef.current = interacting;
  useEffect(() => {
    epochRef.current += 1;
  }, [pendingAction, dragLaneId, opDragActive, planDialogOpen]);

  /** Resolves false when the read failed — callers must not report success then. */
  const load = useCallback(async (options?: { silent?: boolean; background?: boolean }): Promise<boolean> => {
    if (options?.background && interactionRef.current) {
      setUpdatesDeferred(true);
      return false;
    }
    const requestId = ++requestRef.current;
    const epoch = epochRef.current;
    if (options?.silent) setRefreshing(true);
    else setLoading(true);
    try {
      const data = await api.getDispatchWorkOrderBoard();
      // A poll that raced a drag, a dialog or a write is DEFERRED, not just
      // dropped — checked before the request id, because a write supersedes
      // every read issued before it (`dropReadsInFlight`) and the poll must
      // still run once the write settles: an operation-order answer replaces
      // one lane only, so whatever else the poll would have shown is still owed.
      if (options?.background && (interactionRef.current || epoch !== epochRef.current)) {
        setUpdatesDeferred(true);
        return false;
      }
      if (requestId !== requestRef.current) return false;
      if (!isLanesPayload(data)) throw new Error(OLD_API_DETAIL);
      setBoard(data);
      setError(null);
      setStaleNotice(null);
      setUpdatesDeferred(false);
      setLastRefreshed(new Date().toISOString());
      return true;
    } catch (err) {
      if (requestId !== requestRef.current) return false;
      const detail = describeLoadError(err);
      setError(detail);
      setStaleNotice(detail);
      return false;
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  /**
   * Any read already in flight was answered before this point — drop it. Called
   * the moment a write applies its optimistic guess (so a Refresh issued just
   * before the write can never land after it and put the old order back) and
   * again when the write answers with authoritative state. The dropped read's
   * own `finally` no longer matches the request id, so the spinners are
   * cleared here instead.
   */
  const dropReadsInFlight = useCallback(() => {
    requestRef.current += 1;
    setLoading(false);
    setRefreshing(false);
  }, []);

  /** A write answered with authoritative state: any read already in flight is older — drop it. */
  const acceptServerBoard = useCallback(
    (next: Board) => {
      dropReadsInFlight();
      setBoard(next);
      setError(null);
      setStaleNotice(null);
      setLastRefreshed(new Date().toISOString());
    },
    [dropReadsInFlight]
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // Silent by design: a poll replaces the board without announcing anything.
    const refreshVisible = () => {
      if (document.visibilityState === 'visible') void load({ silent: true, background: true });
    };
    const interval = window.setInterval(refreshVisible, DISPATCH_POLL_MS);
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [load]);

  // A poll that was deferred by a drag or a write runs as soon as it ends.
  useEffect(() => {
    if (updatesDeferred && !interacting) void load({ silent: true, background: true });
  }, [updatesDeferred, interacting, load]);

  useEffect(() => {
    if (!onMachineOptions || !board) return;
    onMachineOptions(
      (board.work_centers || []).map(center => ({
        id: center.id,
        code: center.code,
        name: center.name,
        isActive: center.is_active !== false,
      }))
    );
  }, [board, onMachineOptions]);

  // --- Derived ---------------------------------------------------------------
  const lanes = useMemo(() => board?.work_orders ?? [], [board]);
  const plannedLanes = useMemo(() => lanes.filter(lane => lane.dispatch_position != null), [lanes]);
  const unplannedLanes = useMemo(() => lanes.filter(lane => lane.dispatch_position == null), [lanes]);
  const planIds = useMemo(() => plannedLanes.map(lane => lane.work_order_id), [plannedLanes]);
  const filtered = hasAnyFilter(filters);
  const visiblePlanned = useMemo(
    () => (filtered ? plannedLanes.filter(lane => laneMatchesFilters(lane, filters)) : plannedLanes),
    [filtered, filters, plannedLanes]
  );
  const visibleUnplanned = useMemo(
    () => (filtered ? unplannedLanes.filter(lane => laneMatchesFilters(lane, filters)) : unplannedLanes),
    [filtered, filters, unplannedLanes]
  );
  const visiblePlanIds = useMemo(() => visiblePlanned.map(lane => lane.work_order_id), [visiblePlanned]);
  const releasedNotInPlan = unplannedLanes.filter(lane => String(lane.status).toLowerCase() === 'released').length;
  const lateCount = lanes.filter(lane => promiseTone(lane.promise_date) === 'late').length;
  const runningCount = lanes.filter(laneIsRunning).length;
  const busy = pendingAction != null;

  // --- Focus after a keyboard move ------------------------------------------
  useLayoutEffect(() => {
    const pending = pendingFocusRef.current;
    if (!pending) return;
    pendingFocusRef.current = null;
    const row = containerRef.current?.querySelector<HTMLElement>(`[data-lane-row="${pending.laneId}"]`);
    if (!row) return;
    const scope =
      pending.operationId != null
        ? (row.querySelector<HTMLElement>(`[data-lane-op="${pending.operationId}"]`) ?? row)
        : row;
    const attribute = pending.operationId != null ? 'data-op-action' : 'data-lane-action';
    for (const action of pending.actions) {
      const control = scope.querySelector<HTMLButtonElement>(`[${attribute}="${action}"]`);
      if (control && !control.disabled) {
        control.focus();
        return;
      }
    }
    scope.focus();
  }, [board]);

  // --- Plan write (OPTIMISTIC) ----------------------------------------------
  const { run: runPlan } = useOptimisticMutation<Board, PlanCtx>({
    applyOptimistic: ctx => {
      dropReadsInFlight();
      setBoard(applyPlanToBoard(ctx.prevBoard, ctx.nextIds));
      setStatusMessage(ctx.announce);
    },
    rollback: ctx => {
      if (planSeqRef.current === ctx.seq) setBoard(ctx.prevBoard);
    },
    mutate: ctx => api.setDispatchWorkOrderOrder(ctx.nextIds, ctx.expectedIds),
    reconcile: (result, ctx) => {
      if (planSeqRef.current !== ctx.seq) return;
      if (isLanesPayload(result)) acceptServerBoard(result);
      else void load({ silent: true });
    },
    onError: err => {
      // Verbatim: a 409 says the board changed under the planner, a 400 names
      // the id the server did not recognise. Never auto-retry the same body —
      // its CAS list is stale by definition. Re-read instead.
      const detail = dispatchErrorDetail(err, 'Failed to save the plan.');
      showToast('error', detail);
      setStatusMessage(`Plan change not saved. ${detail} Re-reading the board.`);
      void load({ silent: true });
    },
  });

  const changePlan = useCallback(
    (nextIds: number[], announce: string, focus?: PendingFocus) => {
      if (!canEdit || !board || pendingAction) return;
      const expectedIds = planIdsOf(board);
      if (sameIds(expectedIds, nextIds)) return;
      if (focus) pendingFocusRef.current = focus;
      const seq = ++planSeqRef.current;
      setPendingAction('plan');
      void runPlan({ seq, prevBoard: board, nextIds, expectedIds, announce }).finally(() => setPendingAction(null));
    },
    [board, canEdit, pendingAction, runPlan]
  );

  const positionText = (nextIds: number[], id: number) => `position ${nextIds.indexOf(id) + 1} of ${nextIds.length}`;

  const moveLaneUp = (lane: DispatchLane) => {
    const index = visiblePlanIds.indexOf(lane.work_order_id);
    if (index <= 0) return;
    const next = moveIdBefore(planIds, lane.work_order_id, visiblePlanIds[index - 1]);
    changePlan(next, `${lane.work_order_number} moved to plan ${positionText(next, lane.work_order_id)}.`, {
      laneId: lane.work_order_id,
      actions: ['up', 'down', 'top'],
    });
  };

  const moveLaneDown = (lane: DispatchLane) => {
    const index = visiblePlanIds.indexOf(lane.work_order_id);
    if (index < 0 || index >= visiblePlanIds.length - 1) return;
    const next = moveIdAfter(planIds, lane.work_order_id, visiblePlanIds[index + 1]);
    changePlan(next, `${lane.work_order_number} moved to plan ${positionText(next, lane.work_order_id)}.`, {
      laneId: lane.work_order_id,
      actions: ['down', 'up', 'top'],
    });
  };

  const moveLaneToTop = (lane: DispatchLane) => {
    const next = moveIdToIndex(planIds, lane.work_order_id, 0);
    changePlan(next, `${lane.work_order_number} moved to plan ${positionText(next, lane.work_order_id)}.`, {
      laneId: lane.work_order_id,
      actions: ['down', 'position'],
    });
  };

  const removeLane = (lane: DispatchLane) => {
    const next = planIds.filter(id => id !== lane.work_order_id);
    changePlan(next, `${lane.work_order_number} removed from the plan.`, {
      laneId: lane.work_order_id,
      actions: ['add'],
    });
  };

  const addLane = (lane: DispatchLane) => {
    const next = [...planIds.filter(id => id !== lane.work_order_id), lane.work_order_id];
    changePlan(next, `${lane.work_order_number} added to the plan at ${positionText(next, lane.work_order_id)}.`, {
      laneId: lane.work_order_id,
      actions: ['remove', 'up'],
    });
  };

  const confirmAddAll = () => {
    const toAdd = addAllLanes || [];
    setAddAllLanes(null);
    if (toAdd.length === 0) return;
    const inPlan = new Set(planIds);
    const additions = sortLanesByPromiseDate(toAdd.filter(lane => !inPlan.has(lane.work_order_id))).map(
      lane => lane.work_order_id
    );
    changePlan(
      [...planIds, ...additions],
      `${additions.length} work order${additions.length === 1 ? '' : 's'} added to the end of the plan by promise date.`
    );
  };

  const openPositionDialog = (lane: DispatchLane) =>
    setPositionDialog({
      lane,
      defaultValue: String(lane.dispatch_position != null ? lane.dispatch_position : planIds.length + 1),
    });

  /** Live: the highest position the lane can take in the plan as it is NOW. */
  const positionMaxFor = (lane: DispatchLane) =>
    planIds.includes(lane.work_order_id) ? planIds.length : planIds.length + 1;

  const submitPosition = (value: string) => {
    const lane = positionDialog?.lane;
    if (!lane) return;
    const planned = planIds.includes(lane.work_order_id);
    const max = positionMaxFor(lane);
    const position = Number(value);
    if (!Number.isInteger(position) || position < 1 || position > max) {
      showToast('error', `Enter a whole number from 1 to ${max}.`);
      return;
    }
    setPositionDialog(null);
    const next = moveIdToIndex(planIds, lane.work_order_id, position - 1);
    changePlan(
      next,
      `${lane.work_order_number} ${planned ? 'moved to plan' : 'added to the plan at'} ${positionText(next, lane.work_order_id)}.`,
      { laneId: lane.work_order_id, actions: ['position'] }
    );
  };

  // --- Lane drag (pointer-only enhancement) ---------------------------------
  const endLaneDrag = () => {
    setDragLaneId(null);
    setLaneDropSlot(null);
  };

  const handleLaneDragStart = (event: React.DragEvent, lane: DispatchLane) => {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(lane.work_order_id));
    setDragLaneId(lane.work_order_id);
  };

  const markLaneSlot = (slot: number) => {
    if (laneDropSlot !== slot) setLaneDropSlot(slot);
  };

  /** Over a planned lane: above its midpoint inserts before it, below inserts after. */
  const handlePlannedLaneDragOver = (event: React.DragEvent, lane: DispatchLane) => {
    if (dragLaneId == null) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'move';
    const index = visiblePlanIds.indexOf(lane.work_order_id);
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    markLaneSlot(insertionIndexFromPointer([rect], event.clientY) === 0 ? index : index + 1);
  };

  /** Over the plan list's own surface (gaps, padding, an empty plan): slot from pointer geometry. */
  const handlePlanSurfaceDragOver = (event: React.DragEvent) => {
    if (dragLaneId == null) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const rects = Array.from(planListRef.current?.querySelectorAll<HTMLElement>('[data-lane-row]') ?? []).map(element =>
      element.getBoundingClientRect()
    );
    markLaneSlot(insertionIndexFromPointer(rects, event.clientY));
  };

  const handleLaneSurfaceDragLeave = (event: React.DragEvent) => {
    const related = event.relatedTarget as Node | null;
    if (!related || !event.currentTarget.contains(related)) setLaneDropSlot(null);
  };

  const dropLaneAt = (slot: number) => {
    const laneId = dragLaneId;
    endLaneDrag();
    if (laneId == null || !board) return;
    const lane = lanes.find(candidate => candidate.work_order_id === laneId);
    if (!lane) return;
    const next = placeAtVisibleSlot(planIds, visiblePlanIds, laneId, slot);
    const wasPlanned = lane.dispatch_position != null;
    changePlan(
      next,
      `${lane.work_order_number} ${wasPlanned ? 'moved to plan' : 'added to the plan at'} ${positionText(next, laneId)}.`
    );
  };

  const handlePlannedLaneDrop = (event: React.DragEvent, lane: DispatchLane) => {
    event.preventDefault();
    event.stopPropagation();
    const index = visiblePlanIds.indexOf(lane.work_order_id);
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    dropLaneAt(insertionIndexFromPointer([rect], event.clientY) === 0 ? index : index + 1);
  };

  const handlePlanSurfaceDrop = (event: React.DragEvent) => {
    event.preventDefault();
    const rects = Array.from(planListRef.current?.querySelectorAll<HTMLElement>('[data-lane-row]') ?? []).map(element =>
      element.getBoundingClientRect()
    );
    dropLaneAt(insertionIndexFromPointer(rects, event.clientY));
  };

  // --- Operation order within one lane (OPTIMISTIC) ------------------------
  const { run: runOps } = useOptimisticMutation<DispatchLane, OpsCtx>({
    applyOptimistic: ctx => {
      dropReadsInFlight();
      setBoard(previous => (previous ? replaceLane(previous, ctx.nextLane) : previous));
      setStatusMessage(ctx.announce);
    },
    rollback: ctx => {
      if (opsSeqRef.current === ctx.seq)
        setBoard(previous => (previous ? replaceLane(previous, ctx.prevLane) : previous));
    },
    mutate: ctx => api.setDispatchOperationOrder(ctx.laneId, ctx.operationIds, ctx.expectedIds),
    reconcile: (result, ctx) => {
      if (opsSeqRef.current !== ctx.seq) return;
      // The response is that one lane, re-sorted by queue position; other
      // lanes' RUN positions are unchanged by a block permutation. It is
      // authoritative, so a Refresh clicked WHILE the PUT was out (answered
      // before the write committed, arriving after it) is dropped too — the
      // same rule acceptServerBoard applies to the plan write.
      if (isLanePayload(result, ctx.laneId)) {
        dropReadsInFlight();
        setBoard(previous => (previous ? replaceLane(previous, result) : previous));
      } else void load({ silent: true });
    },
    onError: err => {
      const detail = dispatchErrorDetail(err, 'Failed to save the operation order.');
      showToast('error', detail);
      setStatusMessage(`Operation order not saved. ${detail} Re-reading the board.`);
      void load({ silent: true });
    },
  });

  const reorderOperation = useCallback(
    (lane: DispatchLane, operationId: number, toIndex: number, direction?: 'up' | 'down') => {
      if (!canEdit || pendingAction) return;
      const blocked = operationOrderBlockedReason(lane);
      if (blocked) {
        showToast('error', blocked);
        return;
      }
      const op = lane.operations.find(candidate => candidate.operation_id === operationId);
      if (!op) return;
      if (isRunningStatus(op.status) || (op.crew || []).length > 0) {
        showToast('error', RUNNING_PIN_REASON);
        return;
      }
      const result = reorderLaneOperation(lane, operationId, toIndex);
      if (!result) return;
      const machineQueue = result.nextLane.operations.filter(
        candidate =>
          candidate.work_center_id === op.work_center_id && result.operationIds.includes(candidate.operation_id)
      );
      const position = machineQueue.findIndex(candidate => candidate.operation_id === operationId) + 1;
      if (direction) {
        pendingFocusRef.current = {
          laneId: lane.work_order_id,
          operationId,
          actions: direction === 'up' ? ['up', 'down'] : ['down', 'up'],
        };
      }
      const seq = ++opsSeqRef.current;
      setPendingAction('ops');
      void runOps({
        seq,
        laneId: lane.work_order_id,
        prevLane: lane,
        nextLane: result.nextLane,
        operationIds: result.operationIds,
        expectedIds: result.expectedIds,
        announce: `${laneOperationLabel(op)} moved to position ${position} of ${machineQueue.length} for ${
          lane.work_order_number
        } at ${op.work_center_code || 'its machine'}.`,
      }).finally(() => setPendingAction(null));
    },
    [canEdit, pendingAction, runOps, showToast]
  );

  // --- Machine moves (NON-optimistic: the server may refuse) ---------------
  const moveOperations = useCallback(
    async (lane: DispatchLane, ops: DispatchLaneOp[], targetWorkCenterId: number) => {
      if (!canEdit || pendingAction || !board) return;
      const target = (board.work_centers || []).find(center => center.id === targetWorkCenterId);
      // Never a deactivated (or unknown) target: the server 404s it.
      if (!target || target.is_active === false) return;
      const toMove = ops.filter(op => op.work_center_id !== targetWorkCenterId);
      if (toMove.length === 0) {
        showToast('info', `Already at ${target.code}.`);
        return;
      }
      setPendingAction('move');
      setMovingOpIds(toMove.map(op => op.operation_id));
      const moved: DispatchLaneOp[] = [];
      const refused: Array<{ op: DispatchLaneOp; detail: string }> = [];
      try {
        // Sequential, one PUT per operation, each with its own optimistic-lock
        // version: a refusal of one never takes the others down with it.
        for (const op of toMove) {
          try {
            await api.updateOperation(op.operation_id, { version: op.version, work_center_id: targetWorkCenterId });
            moved.push(op);
          } catch (err) {
            refused.push({ op, detail: dispatchErrorDetail(err, 'Move refused') });
          }
        }
        // Re-read after the whole batch; only now does the board change.
        const refreshed = await load({ silent: true });
        const where = `${target.code}${target.name && target.name !== target.code ? ` (${target.name})` : ''}`;
        const refusedText = refused.map(({ op, detail }) => `${laneOperationLabel(op)} — ${detail}`).join('; ');
        if (moved.length === 0) {
          const message =
            toMove.length === 1
              ? refused[0].detail
              : `No operations of ${lane.work_order_number} moved to ${where}. ${refusedText}`;
          showToast('error', message);
          setStatusMessage(`Not moved. ${message}`);
          return;
        }
        if (!refreshed) {
          const message = `Moved ${moved.length} operation${moved.length === 1 ? '' : 's'} of ${
            lane.work_order_number
          } to ${where} on the server, but the board could not be re-read. Refresh to see where it is.`;
          showToast('error', message);
          setStatusMessage(message);
          return;
        }
        if (refused.length > 0) {
          const message = `Moved ${moved.length} of ${toMove.length} operations of ${lane.work_order_number} to ${where}. Not moved: ${refusedText}`;
          showToast('warning', message);
          setStatusMessage(message);
          return;
        }
        // A move clears the operation's own machine rank. If its work order is in
        // the plan (and not held) it stays inside that work order's plan block on
        // the target, after the block's ranked operations; otherwise it joins the
        // target's unranked work below the plan.
        const inPlanBlock = lane.dispatch_position != null && lane.status !== 'on_hold';
        const landing = inPlanBlock
          ? `It runs within ${lane.work_order_number}'s plan block there, after that work order's ranked operations.`
          : "It joins that machine's unranked work, after the plan.";
        const message =
          moved.length === 1
            ? `${laneOperationLabel(moved[0])} of ${lane.work_order_number} moved to ${where}. ${landing}`
            : `Moved ${moved.length} operations of ${lane.work_order_number} to ${where}.`;
        showToast('success', message);
        setStatusMessage(message);
      } finally {
        setPendingAction(null);
        setMovingOpIds([]);
      }
    },
    [board, canEdit, load, pendingAction, showToast]
  );

  const toggleExpanded = (lane: DispatchLane) =>
    setExpandedIds(previous =>
      previous.includes(lane.work_order_id)
        ? previous.filter(id => id !== lane.work_order_id)
        : [...previous, lane.work_order_id]
    );

  // --- Render ----------------------------------------------------------------
  if (loading && !board) {
    return (
      <div className="space-y-2" data-testid="dispatch-lanes-loading" aria-busy="true">
        <Skeleton className="h-4 w-64" />
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-9 w-full rounded-sm" />
        ))}
      </div>
    );
  }

  if (error && !board) {
    return (
      <ErrorState
        title="Couldn't load the dispatch plan"
        message={error}
        onRetry={() => {
          void load();
        }}
      />
    );
  }

  const renderLane = (lane: DispatchLane, index: number, section: 'plan' | 'unplanned') => {
    const planned = section === 'plan';
    return (
      <DispatchLaneRow
        key={lane.work_order_id}
        lane={lane}
        planned={planned}
        canEdit={canEdit}
        busy={busy}
        canMoveUp={planned && index > 0}
        canMoveDown={planned && index < visiblePlanned.length - 1}
        atTop={planned && planIds[0] === lane.work_order_id}
        expanded={expandedIds.includes(lane.work_order_id)}
        dragging={dragLaneId === lane.work_order_id}
        dropBefore={planned && dragLaneId != null && laneDropSlot === index}
        dropAfter={
          planned && dragLaneId != null && index === visiblePlanned.length - 1 && laneDropSlot === visiblePlanned.length
        }
        workCenters={board?.work_centers ?? []}
        movingOpIds={movingOpIds}
        onToggleExpanded={toggleExpanded}
        onMoveUp={moveLaneUp}
        onMoveDown={moveLaneDown}
        onMoveToTop={moveLaneToTop}
        onSetPosition={openPositionDialog}
        onRemove={removeLane}
        onAdd={addLane}
        onDragStart={handleLaneDragStart}
        onDragEnd={endLaneDrag}
        onDragOver={planned ? handlePlannedLaneDragOver : undefined}
        onDrop={planned ? handlePlannedLaneDrop : undefined}
        onReorderOperation={reorderOperation}
        onMoveOperations={moveOperations}
        onOperationDragActiveChange={setOpDragActive}
      />
    );
  };

  const positionLane = positionDialog?.lane ?? null;
  const positionMax = positionLane ? positionMaxFor(positionLane) : 1;

  return (
    <div className="space-y-3" ref={containerRef}>
      {/* Not aria-live: a 10 s poll must never interrupt a screen reader. */}
      <p className="text-xs text-slate-400" data-testid="dispatch-lanes-freshness">
        {lastRefreshed
          ? `Updated ${formatCentralTime(lastRefreshed)} CT · refreshes every ${DISPATCH_POLL_MS / 1000} seconds while visible`
          : 'Waiting for current dispatch data'}
        {updatesDeferred ? ' · Updates deferred until the current change finishes' : ''}
      </p>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-slate-400" data-testid="dispatch-lanes-totals">
          {plannedLanes.length} in plan · {unplannedLanes.length} not in plan
          {lateCount > 0 && <span className="text-red-300"> · {lateCount} late</span>}
          {runningCount > 0 && <> · {runningCount} running</>} ·{' '}
          <span className="text-slate-500">
            the plan is advisory — operators see it on every machine but can still start any job
          </span>
        </p>
        <Button variant="secondary" size="sm" onClick={() => void load({ silent: true })} disabled={refreshing}>
          <ArrowPathIcon className={`mr-1.5 inline h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>

      {!canEdit && (
        <p className="rounded-sm border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
          You can view the plan but not change it.
        </p>
      )}
      {staleNotice && (
        <div
          role="alert"
          data-testid="dispatch-lanes-stale"
          className="flex flex-wrap items-center gap-2 rounded-sm border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-300"
        >
          <span>This plan may be out of date — the last refresh didn&apos;t land. {staleNotice}</span>
          <Button variant="secondary" size="sm" onClick={() => void load({ silent: true })} disabled={refreshing}>
            Retry refresh
          </Button>
        </div>
      )}
      <p
        role="status"
        aria-live="polite"
        data-testid="dispatch-lanes-status"
        className="min-h-[1.25rem] font-mono text-xs text-slate-400"
      >
        {statusMessage}
      </p>

      {filtered && (
        <p className="text-xs text-slate-400" data-testid="dispatch-lanes-filter-summary">
          Showing {visiblePlanned.length + visibleUnplanned.length} of {lanes.length} work orders. Plan numbers are the
          real positions; a move lands next to the work orders you can see.
        </p>
      )}

      {lanes.length === 0 ? (
        <EmptyState
          icon={QueueListIcon}
          title="No open work orders"
          description="Draft, released, in-progress and held work orders appear here to be planned."
        />
      ) : (
        <>
          <section aria-labelledby="dispatch-plan-heading" className="space-y-1.5">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 id="dispatch-plan-heading" className="text-sm font-semibold uppercase tracking-wider text-slate-200">
                Plan
              </h2>
              <span className="text-xs text-slate-500">
                {plannedLanes.length} work order{plannedLanes.length === 1 ? '' : 's'} · every machine runs them top to
                bottom
              </span>
            </div>
            {/* Drop surface for the plan: presentational — the accessible
                equivalents are each lane's Move / Set position / Add buttons. */}
            <div
              role="presentation"
              data-testid="dispatch-plan-surface"
              onDragOver={canEdit ? handlePlanSurfaceDragOver : undefined}
              onDragLeave={canEdit ? handleLaneSurfaceDragLeave : undefined}
              onDrop={canEdit ? handlePlanSurfaceDrop : undefined}
              className={`rounded-sm ${dragLaneId != null && laneDropSlot != null ? 'ring-1 ring-blue-400/50' : ''}`}
            >
              {plannedLanes.length === 0 ? (
                <EmptyState
                  icon={QueueListIcon}
                  title="Nothing in the plan yet"
                  description="Add work orders from “Not in plan” below (or drag them here). Until then each machine runs its own order, then priority and due date."
                />
              ) : visiblePlanned.length === 0 ? (
                <p className="rounded-sm border border-fd-line px-3 py-3 text-xs text-slate-500">
                  No planned work orders match these filters.
                </p>
              ) : (
                <ol
                  ref={planListRef}
                  aria-labelledby="dispatch-plan-heading"
                  className="space-y-1"
                  data-testid="dispatch-plan-list"
                >
                  {visiblePlanned.map((lane, index) => renderLane(lane, index, 'plan'))}
                </ol>
              )}
            </div>
          </section>

          <section aria-labelledby="dispatch-unplanned-heading" className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <h2
                id="dispatch-unplanned-heading"
                className="text-sm font-semibold uppercase tracking-wider text-slate-200"
              >
                Not in plan
              </h2>
              <span
                data-testid="dispatch-released-not-in-plan"
                title="Released work orders that run after everything in the plan until they are placed"
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                  releasedNotInPlan > 0 ? 'border-amber-500/60 text-amber-300' : 'border-fd-line text-slate-500'
                }`}
              >
                {releasedNotInPlan} released, not in plan
              </span>
              {canEdit && visibleUnplanned.length > 0 && (
                <Button
                  variant="secondary"
                  size="sm"
                  className={`ml-auto ${busy ? 'opacity-40' : ''}`}
                  aria-disabled={busy || undefined}
                  title={
                    busy
                      ? 'Waiting for the last change to save…'
                      : 'Append these to the plan, earliest promise date first'
                  }
                  onClick={() => {
                    if (busy) return;
                    setAddAllLanes(visibleUnplanned);
                  }}
                >
                  Add all {visibleUnplanned.length} by promise date
                </Button>
              )}
            </div>
            {unplannedLanes.length === 0 ? (
              <p className="rounded-sm border border-fd-line px-3 py-3 text-xs text-slate-500">
                Every open work order is in the plan.
              </p>
            ) : visibleUnplanned.length === 0 ? (
              <p className="rounded-sm border border-fd-line px-3 py-3 text-xs text-slate-500">
                No work orders outside the plan match these filters.
              </p>
            ) : (
              <ul
                aria-labelledby="dispatch-unplanned-heading"
                className="space-y-1"
                data-testid="dispatch-unplanned-list"
              >
                {visibleUnplanned.map((lane, index) => renderLane(lane, index, 'unplanned'))}
              </ul>
            )}
          </section>
        </>
      )}

      <InputDialog
        open={positionLane != null}
        title={
          positionLane
            ? positionLane.dispatch_position != null
              ? `Set plan position for ${positionLane.work_order_number}`
              : `Add ${positionLane.work_order_number} to the plan`
            : ''
        }
        message={`Enter a position from 1 to ${positionMax}. The other work orders keep their order around it.`}
        label="Plan position"
        defaultValue={positionDialog?.defaultValue ?? ''}
        submitLabel={positionLane?.dispatch_position != null ? 'Move' : 'Add'}
        onSubmit={submitPosition}
        onCancel={() => setPositionDialog(null)}
      />

      <ConfirmDialog
        open={addAllLanes != null}
        variant="info"
        title="Add to the plan by promise date?"
        message={
          addAllLanes
            ? `Appends ${addAllLanes.length} work order${addAllLanes.length === 1 ? '' : 's'}${
                addAllLanes.some(lane => String(lane.status).toLowerCase() === 'draft')
                  ? ` (${addAllLanes.filter(lane => String(lane.status).toLowerCase() === 'draft').length} still in draft — planning does not release them)`
                  : ''
              } to the end of the plan, earliest promise date first. Machines whose queue order changes notify the operators clocked in there.`
            : ''
        }
        confirmLabel="Add to plan"
        onConfirm={confirmAddAll}
        onCancel={() => setAddAllLanes(null)}
      />
    </div>
  );
}

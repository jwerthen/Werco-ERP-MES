/**
 * Dispatch Board v2 — pure helpers for the work-order-first PLAN.
 *
 * DOM-free on purpose: the plan arithmetic (which ids the PUT carries, what the
 * compare-and-swap list is, where a moved work order lands while filters hide
 * its neighbours) is the part that silently corrupts a shop-wide order when it
 * is wrong, so it is unit-tested directly rather than through a rendered board.
 *
 * Two rules every function here keeps:
 *  - NEVER re-sort server order. The plan arrives ordered and each machine queue
 *    arrives ordered; the client only permutes what the user asked it to move.
 *    (The one client sort is "Add all by promise date", which the server
 *    deliberately does not offer — see `sortLanesByPromiseDate`.)
 *  - The numerals on screen are the TRUE plan positions. A filter hides lanes;
 *    it never renumbers them, and a move made while filtered lands relative to
 *    the lanes the user can see, inside the full plan.
 */

import type { DispatchBoardRow, DispatchLane, DispatchLaneOp, DispatchWorkOrderBoardResponse } from '../types';
import { formatOperationLabel } from './operationLabel';
import { isDateBeforeTodayInCentral, isDateTodayInCentral } from './centralTime';

/** Both views poll the active view this often while the tab is visible. */
export const DISPATCH_POLL_MS = 10_000;

/** Why a machine-column move may not cross a work-order band (the server 409 is the backstop). */
export const PLAN_ORDER_EXPLANATION = 'Work orders run in plan order — reorder work orders to change this';

/** Server refusal texts for the operation-order PUT — shown BEFORE the call, so it is never made. */
export const ROUTING_ORDER_REASON = 'Runs in routing sequence';
export const NOT_PLANNED_ORDER_REASON = 'Add this work order to the plan first.';
export const HELD_ORDER_REASON = 'This work order is on hold. Clear the hold before ordering its operations.';

/** Statuses that put an operation in a machine's live queue (server `QUEUE_OPERATION_STATUSES`). */
export const isQueuedStatus = (status: string | null | undefined): boolean => {
  const normalized = String(status || '').toLowerCase();
  return normalized === 'ready' || normalized === 'in_progress';
};

export const isRunningStatus = (status: string | null | undefined): boolean =>
  String(status || '').toLowerCase() === 'in_progress';

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Surface the backend's message verbatim (a refusal must not be reworded). */
export function dispatchErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

export function dispatchErrorStatus(err: unknown): number | null {
  const status = (err as { response?: { status?: unknown } })?.response?.status;
  return typeof status === 'number' ? status : null;
}

/**
 * A lanes payload the view can render. Merging to main auto-deploys, so for a
 * minute the SPA can talk to the previous API: a 404, or a body without
 * `work_orders`, must become a retryable error rather than a crash.
 */
export function isLanesPayload(value: unknown): value is DispatchWorkOrderBoardResponse {
  return !!value && typeof value === 'object' && Array.isArray((value as { work_orders?: unknown }).work_orders);
}

/** A single-lane payload (the operation-order PUT's response). */
export function isLanePayload(value: unknown, workOrderId: number): value is DispatchLane {
  return (
    !!value &&
    typeof value === 'object' &&
    (value as { work_order_id?: unknown }).work_order_id === workOrderId &&
    Array.isArray((value as { operations?: unknown }).operations)
  );
}

// ---------------------------------------------------------------------------
// Plan arithmetic (pure id-list operations)
// ---------------------------------------------------------------------------

/** The plan exactly as rendered: lanes carrying a `dispatch_position`, in order (the CAS list). */
export function planIdsOf(board: Pick<DispatchWorkOrderBoardResponse, 'work_orders'> | null | undefined): number[] {
  return (board?.work_orders || []).filter(lane => lane.dispatch_position != null).map(lane => lane.work_order_id);
}

export const sameIds = (a: readonly number[], b: readonly number[]): boolean =>
  a.length === b.length && a.every((id, index) => id === b[index]);

/** Remove `id`, then insert it immediately BEFORE `targetId` (appended when the target is absent). */
export function moveIdBefore(ids: readonly number[], id: number, targetId: number): number[] {
  const rest = ids.filter(candidate => candidate !== id);
  const at = rest.indexOf(targetId);
  if (at < 0) return [...rest, id];
  return [...rest.slice(0, at), id, ...rest.slice(at)];
}

/** Remove `id`, then insert it immediately AFTER `targetId` (appended when the target is absent). */
export function moveIdAfter(ids: readonly number[], id: number, targetId: number): number[] {
  const rest = ids.filter(candidate => candidate !== id);
  const at = rest.indexOf(targetId);
  if (at < 0) return [...rest, id];
  return [...rest.slice(0, at + 1), id, ...rest.slice(at + 1)];
}

/** Remove `id` (if present), then insert it at final index `index` (clamped). */
export function moveIdToIndex(ids: readonly number[], id: number, index: number): number[] {
  const rest = ids.filter(candidate => candidate !== id);
  const clamped = Math.max(0, Math.min(rest.length, Math.trunc(index)));
  return [...rest.slice(0, clamped), id, ...rest.slice(clamped)];
}

/**
 * Where a drop at insertion slot `slot` (0..visible.length) among the VISIBLE
 * planned lanes lands in the FULL plan. With no filter the visible list IS the
 * plan and this is a plain insert; with a filter the dragged lane goes directly
 * before the visible lane under the slot (or directly after the last visible one),
 * never past lanes the user cannot see.
 */
export function placeAtVisibleSlot(
  planIds: readonly number[],
  visibleIds: readonly number[],
  id: number,
  slot: number
): number[] {
  const others = visibleIds.filter(candidate => candidate !== id);
  // `slot` was measured with the dragged lane still in the visible list.
  const fromVisible = visibleIds.indexOf(id);
  const adjusted = fromVisible >= 0 && slot > fromVisible ? slot - 1 : slot;
  if (others.length === 0) return planIds.includes(id) ? [...planIds] : [...planIds, id];
  if (adjusted < others.length) return moveIdBefore(planIds, id, others[adjusted]);
  return moveIdAfter(planIds, id, others[others.length - 1]);
}

// ---------------------------------------------------------------------------
// Optimistic board
// ---------------------------------------------------------------------------

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

/** The server's "Not in plan" order: priority (null = 99), promise date (nulls last), number, id. */
export function compareUnplannedLanes(a: DispatchLane, b: DispatchLane): number {
  const pa = a.priority ?? 99;
  const pb = b.priority ?? 99;
  if (pa !== pb) return pa - pb;
  const da = a.promise_date || '';
  const db = b.promise_date || '';
  if (da !== db) {
    if (!da) return 1;
    if (!db) return -1;
    return da < db ? -1 : 1;
  }
  const byNumber = collator.compare(a.work_order_number, b.work_order_number);
  if (byNumber !== 0) return byNumber;
  return a.work_order_id - b.work_order_id;
}

/**
 * "Add all by promise date": earliest promise first (nulls last), then work
 * order number, then id. Deliberately NOT the server's unplanned order, which
 * leads with priority.
 */
export function sortLanesByPromiseDate(lanes: readonly DispatchLane[]): DispatchLane[] {
  return [...lanes].sort((a, b) => {
    const da = a.promise_date || '';
    const db = b.promise_date || '';
    if (da !== db) {
      if (!da) return 1;
      if (!db) return -1;
      return da < db ? -1 : 1;
    }
    const byNumber = collator.compare(a.work_order_number, b.work_order_number);
    return byNumber !== 0 ? byNumber : a.work_order_id - b.work_order_id;
  });
}

/**
 * The board as it will look once the plan PUT lands — the optimistic guess,
 * replaced by the server's own board the moment it answers. Listed lanes get
 * positions 1..N in order; every other lane drops out of the plan and takes its
 * place in the unplanned section by the server's own comparator.
 */
export function applyPlanToBoard(
  board: DispatchWorkOrderBoardResponse,
  nextIds: readonly number[]
): DispatchWorkOrderBoardResponse {
  const byId = new Map(board.work_orders.map(lane => [lane.work_order_id, lane] as const));
  const ids = nextIds.filter(id => byId.has(id));
  const inPlan = new Set(ids);
  const planned = ids.map((id, index) => ({ ...(byId.get(id) as DispatchLane), dispatch_position: index + 1 }));
  const unplanned = board.work_orders
    .filter(lane => !inPlan.has(lane.work_order_id))
    .map(lane => (lane.dispatch_position == null ? lane : { ...lane, dispatch_position: null }))
    .sort(compareUnplannedLanes);
  return {
    ...board,
    work_orders: [...planned, ...unplanned],
    ranked_count: planned.length,
    unranked_count: unplanned.length,
  };
}

export const replaceLane = (
  board: DispatchWorkOrderBoardResponse,
  lane: DispatchLane
): DispatchWorkOrderBoardResponse => ({
  ...board,
  work_orders: board.work_orders.map(candidate => (candidate.work_order_id === lane.work_order_id ? lane : candidate)),
});

// ---------------------------------------------------------------------------
// Operations within one lane
// ---------------------------------------------------------------------------

/** The lane's queued subset as rendered, in lane order — the operation-order CAS list. */
export const queuedOperationIds = (lane: Pick<DispatchLane, 'operations'>): number[] =>
  lane.operations.filter(op => isQueuedStatus(op.status)).map(op => op.operation_id);

/** Why a lane's operation order cannot be changed, or null when it can (role aside). */
export function operationOrderBlockedReason(lane: DispatchLane): string | null {
  if (lane.mode === 'routing') return ROUTING_ORDER_REASON;
  if (String(lane.status).toLowerCase() === 'on_hold') return HELD_ORDER_REASON;
  if (lane.dispatch_position == null) return NOT_PLANNED_ORDER_REASON;
  return null;
}

export interface MachineGroup {
  /** `null` when an operation has no work center (rendered as "No machine"). */
  workCenterId: number | null;
  code: string | null;
  ops: DispatchLaneOp[];
}

/**
 * Pool / laser lanes arrive grouped by machine (work-center code), queued
 * operations first in that machine's live-queue order. Group them WITHOUT
 * re-sorting, preserving first-appearance order.
 */
export function groupOperationsByMachine(ops: readonly DispatchLaneOp[]): MachineGroup[] {
  const groups: MachineGroup[] = [];
  const byKey = new Map<string, MachineGroup>();
  ops.forEach(op => {
    const key = op.work_center_id == null ? 'none' : String(op.work_center_id);
    let group = byKey.get(key);
    if (!group) {
      group = { workCenterId: op.work_center_id, code: op.work_center_code, ops: [] };
      byKey.set(key, group);
      groups.push(group);
    }
    group.ops.push(op);
  });
  return groups;
}

export interface OperationReorder {
  nextLane: DispatchLane;
  /** Every queued operation of the work order, in the requested order. */
  operationIds: number[];
  /** The queued subset exactly as rendered (CAS). */
  expectedIds: number[];
}

/**
 * Move one queued operation to final index `toIndex` among the QUEUED operations
 * of its own machine, and build the operation-order PUT for it. Only the
 * relative order of operations sharing a machine means anything to the server,
 * so the permutation never leaves the machine group. The RUN positions the group
 * already holds are handed out again in the new order (an optimistic guess; the
 * PUT's response replaces the lane).
 */
export function reorderLaneOperation(
  lane: DispatchLane,
  operationId: number,
  toIndex: number
): OperationReorder | null {
  const op = lane.operations.find(candidate => candidate.operation_id === operationId);
  if (!op || !isQueuedStatus(op.status)) return null;
  const sameMachine = (candidate: DispatchLaneOp) =>
    isQueuedStatus(candidate.status) && candidate.work_center_id === op.work_center_id;
  const group = lane.operations.filter(sameMachine);
  const from = group.findIndex(candidate => candidate.operation_id === operationId);
  const to = Math.max(0, Math.min(group.length - 1, Math.trunc(toIndex)));
  if (from < 0 || from === to) return null;
  const permuted = [...group];
  const [moved] = permuted.splice(from, 1);
  permuted.splice(to, 0, moved);
  const positions = group.map(candidate => candidate.run_position);
  const renumber = positions.every(position => position != null);
  const sortedPositions = renumber ? [...(positions as number[])].sort((a, b) => a - b) : [];
  let cursor = 0;
  const nextOps = lane.operations.map(candidate => {
    if (!sameMachine(candidate)) return candidate;
    const next = permuted[cursor];
    const position = renumber ? sortedPositions[cursor] : next.run_position;
    cursor += 1;
    return position === next.run_position ? next : { ...next, run_position: position };
  });
  const nextLane = { ...lane, operations: nextOps };
  return {
    nextLane,
    operationIds: queuedOperationIds(nextLane),
    expectedIds: queuedOperationIds(lane),
  };
}

/**
 * The earlier operation a PENDING one is waiting on, for "Waiting on Op 10":
 * the nearest lower-sequence operation that is not complete. On a pool lane only
 * another machine's operation (or a held one) gates it — operations sharing a
 * machine are mutually startable there.
 */
export function waitingOnOperation(lane: DispatchLane, op: DispatchLaneOp): DispatchLaneOp | null {
  let best: DispatchLaneOp | null = null;
  lane.operations.forEach(candidate => {
    if (candidate.operation_id === op.operation_id) return;
    if (candidate.sequence >= op.sequence) return;
    if (String(candidate.status).toLowerCase() === 'complete') return;
    const gates =
      lane.mode === 'routing' ||
      candidate.on_hold ||
      String(candidate.status).toLowerCase() === 'on_hold' ||
      candidate.work_center_id !== op.work_center_id;
    if (!gates) return;
    if (!best || candidate.sequence > best.sequence) best = candidate;
  });
  return best;
}

/** "Op 20 Bend" — how an operation is named in announcements and aria-labels. */
export function laneOperationLabel(
  op: Pick<DispatchLaneOp, 'operation_number' | 'sequence' | 'operation_name'>
): string {
  const number = formatOperationLabel(op.operation_number, op.sequence);
  return op.operation_name ? `${number} ${op.operation_name}` : number;
}

// ---------------------------------------------------------------------------
// Lane facts
// ---------------------------------------------------------------------------

export const laneIsRunning = (lane: DispatchLane): boolean =>
  lane.operations.some(op => isRunningStatus(op.status) || (op.crew || []).length > 0);

/** Distinct crew names across the lane's operations, in operation order. */
export function laneCrewNames(lane: DispatchLane): string[] {
  const names: string[] = [];
  lane.operations.forEach(op =>
    (op.crew || []).forEach(member => {
      const name = (member.display_name || '').trim();
      if (name && !names.includes(name)) names.push(name);
    })
  );
  return names;
}

export const laneIsHeld = (lane: DispatchLane): boolean =>
  String(lane.status).toLowerCase() === 'on_hold' ||
  lane.operations.some(op => op.on_hold || String(op.status).toLowerCase() === 'on_hold');

export type PromiseTone = 'late' | 'today' | null;

/** Late / due-today tone of a date-only promise (Central calendar), or null. */
export function promiseTone(date: string | null | undefined): PromiseTone {
  if (!date) return null;
  if (isDateBeforeTodayInCentral(date)) return 'late';
  if (isDateTodayInCentral(date)) return 'today';
  return null;
}

// ---------------------------------------------------------------------------
// Search + filter chips (URL params `q`, `late`, `running`, `held`, `machine`)
// ---------------------------------------------------------------------------

export interface DispatchFilters {
  /** Debounced, trimmed search text. */
  q: string;
  late: boolean;
  running: boolean;
  held: boolean;
  machine: number | null;
}

export const NO_DISPATCH_FILTERS: DispatchFilters = { q: '', late: false, running: false, held: false, machine: null };

/** Filters that narrow work items (everything except the machine filter). */
export const hasItemFilters = (filters: DispatchFilters): boolean =>
  !!filters.q || filters.late || filters.running || filters.held;

export const hasAnyFilter = (filters: DispatchFilters): boolean => hasItemFilters(filters) || filters.machine != null;

const includesText = (haystack: Array<string | null | undefined>, needle: string): boolean => {
  const query = needle.trim().toLowerCase();
  if (!query) return true;
  return haystack.some(value => typeof value === 'string' && value.toLowerCase().includes(query));
};

/** Does a work-order lane pass every active filter? */
export function laneMatchesFilters(lane: DispatchLane, filters: DispatchFilters): boolean {
  if (
    filters.q &&
    !includesText(
      [lane.work_order_number, lane.part_number, lane.part_name, lane.customer_name, lane.unit_number],
      filters.q
    )
  ) {
    return false;
  }
  if (filters.late && promiseTone(lane.promise_date) !== 'late') return false;
  if (filters.running && !laneIsRunning(lane)) return false;
  if (filters.held && !laneIsHeld(lane)) return false;
  if (filters.machine != null && !lane.operations.some(op => op.work_center_id === filters.machine)) return false;
  return true;
}

/**
 * Does a machine-view row pass the item filters? (The machine filter picks
 * columns, not rows.) A row carries no customer or promise date, so search
 * reads its identifiers and "late" reads its due date.
 */
export function rowMatchesFilters(row: DispatchBoardRow, filters: DispatchFilters): boolean {
  if (
    filters.q &&
    !includesText(
      [row.work_order_number, row.part_number, row.part_name, row.component_part_number, row.unit_number],
      filters.q
    )
  ) {
    return false;
  }
  if (filters.late && !(row.due_date && isDateBeforeTodayInCentral(row.due_date))) return false;
  if (filters.running && !isRunningStatus(row.status)) return false;
  if (filters.held && String(row.work_order_status || '').toLowerCase() !== 'on_hold') return false;
  return true;
}

// ---------------------------------------------------------------------------
// Machine view: work-order bands
// ---------------------------------------------------------------------------

/**
 * Which band a machine-view row sits in: its work order's plan position, or the
 * unplanned tail (`dispatch_position` null — not planned, or planned but held).
 * A pre-v2 payload has no positions at all, so every row is in the tail and no
 * band constraint applies.
 */
export const bandKey = (row: Pick<DispatchBoardRow, 'dispatch_position'>): string =>
  row.dispatch_position == null ? 'tail' : `plan-${row.dispatch_position}`;

type Banded = Pick<DispatchBoardRow, 'dispatch_position'>;

export const columnHasBands = (queue: readonly Banded[]): boolean => queue.some(row => row.dispatch_position != null);

/**
 * Would moving the row at `from` to final index `to` carry it past a row of a
 * different band? Inside one work order's block, and anywhere in the unplanned
 * tail, any order is allowed; across bands the plan decides.
 */
export function crossesBand(queue: readonly Banded[], from: number, to: number): boolean {
  if (from === to || !queue[from]) return false;
  const key = bandKey(queue[from]);
  const [low, high] = from < to ? [from + 1, to] : [to, from - 1];
  for (let index = low; index <= high; index += 1) {
    if (queue[index] && bandKey(queue[index]) !== key) return true;
  }
  return false;
}

/** "7.25 h queued" — planned (not remaining) setup + run hours. */
export function formatQueuedHours(hours: number | null | undefined): string | null {
  if (typeof hours !== 'number' || !Number.isFinite(hours)) return null;
  return `${String(Number(hours.toFixed(1)))} h queued`;
}

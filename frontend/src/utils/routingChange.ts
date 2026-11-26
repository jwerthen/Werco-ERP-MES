/**
 * Changing a work order's routing after release (owner decision 2026-11-25) — the client-side
 * mirrors of the server's routing-change gates (`backend/app/services/routing_change_service.py`).
 *
 * Every helper here is ADVISORY: it only decides which controls to offer, so the page never
 * shows a button the server would always refuse. The server stays the authority and re-judges
 * every request (version, recorded work, sequence collisions, short-count strands); a refusal
 * reaches the user verbatim.
 */
import type { OperationSequenceChange, WorkOrder, WorkOrderOperation } from '../types';

/** Statuses the server treats as FINISHED for predecessor gates: `complete`, and `skipped`. */
const FINISHED_STATUSES = new Set(['complete', 'completed', 'skipped']);

/**
 * Is this operation finished for gating purposes (it no longer blocks the steps after it)?
 * Case-insensitive and tolerant of `undefined`, because board payloads type status loosely.
 */
export function isOperationFinished(status: string | null | undefined): boolean {
  return FINISHED_STATUSES.has(
    String(status ?? '')
      .trim()
      .toLowerCase()
  );
}

export function isOperationSkipped(status: string | null | undefined): boolean {
  return (
    String(status ?? '')
      .trim()
      .toLowerCase() === 'skipped'
  );
}

/** Work-order statuses on which the routing can no longer change (server: 409). */
const TERMINAL_WORK_ORDER_STATUSES = new Set(['complete', 'closed', 'cancelled']);

/**
 * May the routing editor be OFFERED on this work order at all? Hidden on terminal, laser
 * (nests are managed through their own verbs; the server refuses 409) and deleted work orders.
 * The ROLE gate (ADMIN / MANAGER / SUPERVISOR — the routes' `require_role`) is the caller's.
 */
export function routingEditableWorkOrder(
  workOrder: Pick<WorkOrder, 'status' | 'work_order_type'> & { is_deleted?: boolean | null }
): boolean {
  if (workOrder.is_deleted) return false;
  if (TERMINAL_WORK_ORDER_STATUSES.has(workOrder.status)) return false;
  if (workOrder.work_order_type === 'laser_cutting') return false;
  return true;
}

/** Does a change on this work order need a typed reason? Every one does once it left DRAFT. */
export function routingChangeNeedsReason(workOrder: Pick<WorkOrder, 'status'>): boolean {
  return workOrder.status !== 'draft';
}

/**
 * Has any work been recorded against this step — a start, pieces, scrap or labor? The server
 * refuses to skip or reorder one that has (`operation_ids_with_recorded_work`).
 */
export function operationHasRecordedWork(
  op: Pick<WorkOrderOperation, 'actual_start' | 'quantity_complete' | 'quantity_scrapped'> &
    Partial<Pick<WorkOrderOperation, 'has_labor_evidence'>>
): boolean {
  if (op.has_labor_evidence === true) return true;
  if (op.actual_start) return true;
  return Number(op.quantity_complete || 0) > 0 || Number(op.quantity_scrapped || 0) > 0;
}

/** Not started (PENDING / READY) and nothing recorded: the steps skip and reorder accept. */
export function operationNotStarted(op: WorkOrderOperation): boolean {
  return (op.status === 'pending' || op.status === 'ready') && !operationHasRecordedWork(op);
}

export const canSkipOperation = (op: WorkOrderOperation): boolean =>
  operationNotStarted(op) && op.cancelled_nest_id == null;

export const canRestoreOperation = (op: WorkOrderOperation): boolean => op.status === 'skipped';

/** Content edits are refused on a COMPLETE step (its record) and a SKIPPED one (restore first). */
export const canEditOperation = (op: WorkOrderOperation): boolean =>
  op.status !== 'complete' && op.status !== 'skipped' && op.cancelled_nest_id == null;

/** Operations ordered by sequence, then id — the server's route order. Never mutates its input. */
export function operationsBySequence<T extends Pick<WorkOrderOperation, 'id' | 'sequence'>>(operations: T[]): T[] {
  return [...operations].sort((a, b) => a.sequence - b.sequence || a.id - b.id);
}

/**
 * The resequence body that swaps `op` with its nearest neighbour in `direction`, skipping over
 * SKIPPED steps (they are off the route and not part of the uniqueness rule). `null` when there
 * is no such neighbour, when either step has started, or when the two share a sequence (a swap
 * would change nothing).
 */
export function swapWithNeighbour(
  operations: WorkOrderOperation[],
  op: WorkOrderOperation,
  direction: 'up' | 'down'
): { neighbour: WorkOrderOperation; changes: OperationSequenceChange[] } | null {
  const route = operationsBySequence(operations.filter(candidate => candidate.status !== 'skipped'));
  const index = route.findIndex(candidate => candidate.id === op.id);
  if (index < 0) return null;
  const neighbour = route[direction === 'up' ? index - 1 : index + 1];
  if (!neighbour) return null;
  if (!operationNotStarted(op) || !operationNotStarted(neighbour)) return null;
  if (neighbour.sequence === op.sequence) return null;
  return {
    neighbour,
    changes: [
      { id: op.id, version: op.version, sequence: neighbour.sequence },
      { id: neighbour.id, version: neighbour.version, sequence: op.sequence },
    ],
  };
}

export const MIN_SEQUENCE = 10;
export const MAX_SEQUENCE = 990;

/** Sequences already held by a step that is not skipped (a skipped step's may be reused). */
export function usedSequences(operations: WorkOrderOperation[]): number[] {
  return Array.from(new Set(operations.filter(op => op.status !== 'skipped').map(op => op.sequence))).sort(
    (a, b) => a - b
  );
}

/** The default sequence for a new step: 10 past the highest used one, capped at the server maximum. */
export function nextFreeSequence(operations: WorkOrderOperation[]): number {
  const used = usedSequences(operations);
  const candidate = (used.length ? used[used.length - 1] : 0) + 10;
  if (candidate <= MAX_SEQUENCE) return Math.max(MIN_SEQUENCE, candidate);
  for (let seq = MIN_SEQUENCE; seq <= MAX_SEQUENCE; seq += 10) {
    if (!used.includes(seq)) return seq;
  }
  return MAX_SEQUENCE;
}

/**
 * Is this the server's optimistic-lock refusal (409 "Operation was modified by someone else.
 * Refresh and try again.")? Every routing verb and the operation PUT answer it on a stale
 * `version`; other 409s are state refusals the person has to act on, not retry.
 */
export function isOperationVersionConflict(err: unknown): boolean {
  const response = (err as { response?: { status?: unknown; data?: { detail?: unknown } } } | null)?.response;
  if (response?.status !== 409) return false;
  const detail = response.data?.detail;
  return typeof detail === 'string' && detail.toLowerCase().includes('modified by someone else');
}

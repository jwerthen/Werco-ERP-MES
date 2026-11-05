/**
 * Pure plan arithmetic behind the Dispatch Board v2 (work-order order wins).
 *
 * These are the functions that decide WHICH ids a plan / operation-order PUT
 * carries and what its compare-and-swap list is — the part that silently
 * corrupts a shop-wide order if it is wrong — so they are pinned directly.
 */

import {
  applyPlanToBoard,
  bandKey,
  crossesBand,
  formatQueuedHours,
  HELD_ORDER_REASON,
  isLanePayload,
  isLanesPayload,
  laneMatchesFilters,
  moveIdAfter,
  moveIdBefore,
  moveIdToIndex,
  NO_DISPATCH_FILTERS,
  NOT_PLANNED_ORDER_REASON,
  operationOrderBlockedReason,
  placeAtVisibleSlot,
  planIdsOf,
  queuedOperationIds,
  reorderLaneOperation,
  ROUTING_ORDER_REASON,
  sortLanesByPromiseDate,
  waitingOnOperation,
} from './dispatchPlan';
import type { DispatchBoardRow, DispatchLane, DispatchLaneOp, DispatchWorkOrderBoardResponse } from '../types';

const op = (overrides: Partial<DispatchLaneOp> & { operation_id: number }): DispatchLaneOp => ({
  version: 1,
  sequence: 10,
  operation_number: '10',
  operation_name: 'Operation',
  status: 'ready',
  work_center_id: 12,
  work_center_code: 'BRAKE-1',
  run_position: null,
  blocked_by_predecessor: false,
  on_hold: false,
  crew: [],
  quantity_complete: 0,
  quantity_target: 10,
  setup_time_hours: 0,
  run_time_hours: 0,
  component_part_number: null,
  laser_nest: null,
  material_tie: null,
  ...overrides,
});

const lane = (overrides: Partial<DispatchLane> & { work_order_id: number }): DispatchLane => ({
  work_order_number: `WO-${overrides.work_order_id}`,
  version: 1,
  unit_number: null,
  status: 'released',
  work_order_type: 'production',
  sequential_operations: false,
  is_laser: false,
  mode: 'pool',
  dispatch_position: null,
  priority: 5,
  due_date: null,
  must_ship_by: null,
  promise_date: null,
  customer_name: null,
  part_number: null,
  part_name: null,
  quantity_ordered: 1,
  quantity_complete: 0,
  ops_total: 0,
  ops_complete: 0,
  open_blocker_count: 0,
  any_material_short: false,
  operations: [],
  ...overrides,
});

const boardOf = (work_orders: DispatchLane[]): DispatchWorkOrderBoardResponse => ({
  generated_at: '2026-11-04T00:00:00Z',
  work_orders,
  ranked_count: 0,
  unranked_count: 0,
  work_centers: [],
});

describe('id-list moves', () => {
  it('moves before / after a target and to an index, removing the id first', () => {
    expect(moveIdBefore([1, 2, 3, 4], 4, 2)).toEqual([1, 4, 2, 3]);
    expect(moveIdAfter([1, 2, 3, 4], 1, 3)).toEqual([2, 3, 1, 4]);
    expect(moveIdToIndex([1, 2, 3], 3, 0)).toEqual([3, 1, 2]);
    // An id not yet in the list is inserted (Add at position…), clamped to the end.
    expect(moveIdToIndex([1, 2], 9, 99)).toEqual([1, 2, 9]);
    expect(moveIdBefore([1, 2], 9, 1)).toEqual([9, 1, 2]);
  });

  it('places a drop at a VISIBLE slot without jumping over hidden lanes', () => {
    // Plan 1..5; a filter shows only 10, 30, 50.
    const plan = [10, 20, 30, 40, 50];
    const visible = [10, 30, 50];
    // Dragging 50 to the slot before 30 lands directly before 30 — 20 stays put.
    expect(placeAtVisibleSlot(plan, visible, 50, 1)).toEqual([10, 20, 50, 30, 40]);
    // Slot past the last visible lane lands directly after it, not at the end of the plan.
    expect(placeAtVisibleSlot(plan, [10, 30], 10, 2)).toEqual([20, 30, 10, 40, 50]);
    // An unplanned lane dropped into a filtered plan.
    expect(placeAtVisibleSlot(plan, visible, 99, 0)).toEqual([99, 10, 20, 30, 40, 50]);
    // No filter: a plain insert, accounting for the dragged lane's own slot.
    expect(placeAtVisibleSlot([1, 2, 3], [1, 2, 3], 1, 3)).toEqual([2, 3, 1]);
  });
});

describe('applyPlanToBoard (the optimistic guess)', () => {
  it('numbers listed lanes 1..N and drops the rest into the unplanned order the server uses', () => {
    const board = boardOf([
      lane({ work_order_id: 1, dispatch_position: 1, priority: 9 }),
      lane({ work_order_id: 2, dispatch_position: 2, priority: 1 }),
      lane({ work_order_id: 3, priority: 5, promise_date: '2099-01-02' }),
      lane({ work_order_id: 4, priority: 5, promise_date: '2099-01-01' }),
    ]);
    const next = applyPlanToBoard(board, [4, 1]);
    expect(planIdsOf(next)).toEqual([4, 1]);
    expect(next.work_orders.map(l => [l.work_order_id, l.dispatch_position])).toEqual([
      [4, 1],
      [1, 2],
      // Removed lane 2 (priority 1) sorts first among the unplanned, as the server would.
      [2, null],
      [3, null],
    ]);
    expect(next.ranked_count).toBe(2);
    expect(next.unranked_count).toBe(2);
  });
});

describe('sortLanesByPromiseDate', () => {
  it('earliest promise first, no date last, then work order number', () => {
    const lanes = [
      lane({ work_order_id: 1, work_order_number: 'WO-10', promise_date: null }),
      lane({ work_order_id: 2, work_order_number: 'WO-9', promise_date: '2099-03-01' }),
      lane({ work_order_id: 3, work_order_number: 'WO-2', promise_date: '2099-01-01' }),
      lane({ work_order_id: 4, work_order_number: 'WO-1', promise_date: '2099-03-01' }),
    ];
    expect(sortLanesByPromiseDate(lanes).map(l => l.work_order_id)).toEqual([3, 4, 2, 1]);
  });
});

describe('operation order within a lane', () => {
  const poolLane = lane({
    work_order_id: 7,
    dispatch_position: 1,
    operations: [
      op({ operation_id: 1, run_position: 3 }),
      op({ operation_id: 2, run_position: 4 }),
      op({ operation_id: 3, run_position: 5 }),
      op({ operation_id: 9, work_center_id: 14, work_center_code: 'WELD-2', status: 'in_progress', run_position: 1 }),
      op({ operation_id: 8, status: 'pending', sequence: 40 }),
    ],
  });

  it('permutes only the queued operations of ONE machine and reuses that block’s RUN positions', () => {
    const result = reorderLaneOperation(poolLane, 3, 0);
    expect(result).not.toBeNull();
    expect(result!.expectedIds).toEqual([1, 2, 3, 9]);
    expect(result!.operationIds).toEqual([3, 1, 2, 9]);
    expect(result!.nextLane.operations.map(o => [o.operation_id, o.run_position])).toEqual([
      [3, 3],
      [1, 4],
      [2, 5],
      [9, 1],
      [8, null],
    ]);
    expect(queuedOperationIds(result!.nextLane)).toEqual([3, 1, 2, 9]);
  });

  it('refuses a no-op and an off-queue operation', () => {
    expect(reorderLaneOperation(poolLane, 1, 0)).toBeNull();
    expect(reorderLaneOperation(poolLane, 8, 0)).toBeNull();
  });

  it('names why a lane’s operation order is read-only, in the server’s words', () => {
    expect(operationOrderBlockedReason(lane({ work_order_id: 1, mode: 'routing', dispatch_position: 1 }))).toBe(
      ROUTING_ORDER_REASON
    );
    expect(operationOrderBlockedReason(lane({ work_order_id: 1, status: 'on_hold', dispatch_position: 1 }))).toBe(
      HELD_ORDER_REASON
    );
    expect(operationOrderBlockedReason(lane({ work_order_id: 1 }))).toBe(NOT_PLANNED_ORDER_REASON);
    expect(operationOrderBlockedReason(lane({ work_order_id: 1, mode: 'laser', dispatch_position: 2 }))).toBeNull();
  });

  it('a waiting operation names the nearest earlier operation that gates it', () => {
    const routing = lane({
      work_order_id: 2,
      mode: 'routing',
      operations: [
        op({ operation_id: 1, sequence: 10, operation_number: '10', status: 'complete' }),
        op({ operation_id: 2, sequence: 20, operation_number: '20', status: 'in_progress' }),
        op({ operation_id: 3, sequence: 30, operation_number: '30', status: 'pending', blocked_by_predecessor: true }),
      ],
    });
    expect(waitingOnOperation(routing, routing.operations[2])?.operation_id).toBe(2);
    // On a pool lane a same-machine sibling does not gate.
    const pool = { ...routing, mode: 'pool' as const };
    expect(waitingOnOperation(pool, pool.operations[2])).toBeNull();
  });
});

describe('filters', () => {
  const subject = lane({
    work_order_id: 5,
    work_order_number: 'WO-20261104-005',
    customer_name: 'Miratech',
    unit_number: '7',
    promise_date: '2020-01-01',
    operations: [op({ operation_id: 1, work_center_id: 12, crew: [{ user_id: 1, display_name: 'Jon W.' }] })],
  });

  it('search reads number, part, customer and unit; chips read promise, crew, hold and machine', () => {
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, q: 'mira' })).toBe(true);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, q: '7' })).toBe(true);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, q: 'acme' })).toBe(false);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, late: true })).toBe(true);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, running: true })).toBe(true);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, held: true })).toBe(false);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, machine: 12 })).toBe(true);
    expect(laneMatchesFilters(subject, { ...NO_DISPATCH_FILTERS, machine: 14 })).toBe(false);
  });
});

describe('machine-view bands', () => {
  const queue: Array<Pick<DispatchBoardRow, 'dispatch_position'>> = [1, 1, 2, null, null].map(dispatch_position => ({
    dispatch_position,
  }));

  it('allows moves inside one block or the tail, refuses any that pass another band', () => {
    expect(crossesBand(queue, 0, 1)).toBe(false);
    expect(crossesBand(queue, 4, 3)).toBe(false);
    expect(crossesBand(queue, 2, 1)).toBe(true);
    expect(crossesBand(queue, 3, 0)).toBe(true);
    expect(crossesBand(queue, 1, 2)).toBe(true);
    expect(bandKey({ dispatch_position: undefined })).toBe('tail');
  });

  it('formats queued hours, and nothing for a pre-v2 column', () => {
    expect(formatQueuedHours(7.5)).toBe('7.5 h queued');
    expect(formatQueuedHours(3)).toBe('3 h queued');
    expect(formatQueuedHours(undefined)).toBeNull();
  });
});

describe('payload guards (an older API during a deploy)', () => {
  it('accepts only bodies that carry lanes', () => {
    expect(isLanesPayload(boardOf([]))).toBe(true);
    expect(isLanesPayload({ work_centers: [] })).toBe(false);
    expect(isLanesPayload(null)).toBe(false);
    expect(isLanePayload(lane({ work_order_id: 3 }), 3)).toBe(true);
    expect(isLanePayload(lane({ work_order_id: 3 }), 4)).toBe(false);
  });
});

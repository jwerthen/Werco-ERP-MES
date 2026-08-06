/**
 * Duplicate-work-order client boundary — the ENVELOPE contract.
 *
 * `POST /work-orders/{id}/duplicate` does NOT return a work order. It returns a
 * `WorkOrderDuplicateResponse` envelope:
 *
 *     { work_order: {...}, skipped_operations: [...], skipped_material_allocations: [...] }
 *
 * The envelope exists because the two skip lists are SAFETY information: the
 * source's sheet part was soft-deleted since it ran, so the material tie is
 * skipped; the planner sees "created as a draft", releases the laser job
 * believing it carries its material demand, no shortage shows, the nests run,
 * and stock is never deducted. A skip that reaches only the audit chain is a
 * skip nobody reads until the inventory count disagrees.
 *
 * This file pins the shape at its source, and it exists because the page- and
 * component-level suites CANNOT catch a mistake here: they mock this module, so
 * a mock that honours the declared return type is green no matter what the
 * server actually sends. That is precisely how the SPC characteristic list
 * shipped permanently empty (see `api.spc.test.ts`) — the sibling of this bug,
 * with the envelope on the other side of the wire.
 *
 * It has already earned its keep: the client returned `response.data` typed as a
 * bare `WorkOrder` while the server sent the envelope, which made
 * `created.work_order_number` undefined ("undefined created as a draft") and sent
 * both callers to `/work-orders/undefined`. `type-check` could not see it —
 * `response.data` was `any`. The POST is now typed, and the assertion below that
 * the result is NOT a bare work order is the regression guard.
 *
 * axios is mocked at the module boundary (same pattern as api.spc.test.ts).
 */

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  patch: mockPatch,
  delete: mockDelete,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: jest.fn() },
    create,
  };
});

import api from './api';

const ok = (data: unknown) => ({ status: 201, data, headers: {} });

/** The new work order as the server nests it under `work_order`. */
const newWorkOrder = {
  id: 501,
  version: 1,
  work_order_number: 'WO-20260805-007',
  part_id: 10,
  work_order_type: 'laser_cutting',
  quantity_ordered: 18,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'draft',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  estimated_cost: 0,
  actual_cost: 0,
  created_at: '2026-08-05T12:00:00Z',
  updated_at: '2026-08-05T12:00:00Z',
  operations: [],
};

/** Exactly what `WorkOrderDuplicateResponse` serializes to. */
const envelope = (overrides: Record<string, unknown> = {}) => ({
  work_order: newWorkOrder,
  skipped_operations: [],
  skipped_material_allocations: [],
  ...overrides,
});

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

describe('api.duplicateWorkOrder: request', () => {
  it('posts the two fields the planner controls to the duplicate route', async () => {
    mockPost.mockResolvedValue(ok(envelope()));

    await api.duplicateWorkOrder(42, { quantity_ordered: 12, due_date: '2026-09-30' });

    expect(mockPost).toHaveBeenCalledWith('/work-orders/42/duplicate', {
      quantity_ordered: 12,
      due_date: '2026-09-30',
    });
  });

  it('sends an explicit null due date rather than omitting the key', async () => {
    // Blank means "no promise yet". Omitting it would let the server's default
    // decide, and the whole point is that the copy starts unscheduled.
    mockPost.mockResolvedValue(ok(envelope()));

    await api.duplicateWorkOrder(42, { quantity_ordered: 12 });

    expect(mockPost).toHaveBeenCalledWith('/work-orders/42/duplicate', {
      quantity_ordered: 12,
      due_date: null,
    });
  });
});

describe('api.duplicateWorkOrder: the envelope reaches the caller intact', () => {
  it('returns the envelope, NOT a bare work order', async () => {
    // THE regression guard. Unwrapping to `result.work_order` here would compile
    // (it satisfies `WorkOrder`) and would silently drop both skip lists — the
    // only channel that tells a planner the copy is short of material demand.
    mockPost.mockResolvedValue(ok(envelope()));

    const result = await api.duplicateWorkOrder(42, { quantity_ordered: 12 });

    expect(result).toHaveProperty('work_order');
    expect(result).toHaveProperty('skipped_operations');
    expect(result).toHaveProperty('skipped_material_allocations');
    // A bare work order would carry these at the top level instead.
    expect(result).not.toHaveProperty('work_order_number');
  });

  it('carries the new work order through under `work_order`', async () => {
    // Both callers navigate with `result.work_order.id` and the toast quotes
    // `result.work_order.quantity_ordered`; all three were `undefined` when this
    // method returned the envelope typed as a bare work order.
    mockPost.mockResolvedValue(ok(envelope()));

    const result = await api.duplicateWorkOrder(42, { quantity_ordered: 12 });

    expect(result.work_order.id).toBe(501);
    expect(result.work_order.work_order_number).toBe('WO-20260805-007');
    // The server DERIVED 18 from the copied nests' runs; 12 was only requested.
    expect(result.work_order.quantity_ordered).toBe(18);
    expect(result.work_order.status).toBe('draft');
  });

  it('preserves both skip lists verbatim, with the fields the UI names them by', async () => {
    const skippedOperation = {
      source_operation_id: 71,
      operation_number: 'OP20',
      sequence: 20,
      reason: 'laser_nest_deleted',
    };
    const skippedTie = {
      source_allocation_id: 9,
      part_id: 55,
      source_work_order_operation_id: 71,
      reason: 'part_not_available',
    };
    mockPost.mockResolvedValue(
      ok(envelope({ skipped_operations: [skippedOperation], skipped_material_allocations: [skippedTie] }))
    );

    const result = await api.duplicateWorkOrder(42, { quantity_ordered: 12 });

    expect(result.skipped_operations).toEqual([skippedOperation]);
    expect(result.skipped_material_allocations).toEqual([skippedTie]);
  });

  it('passes an empty skip list through as empty — the "nothing was lost" signal', async () => {
    mockPost.mockResolvedValue(ok(envelope()));

    const result = await api.duplicateWorkOrder(42, { quantity_ordered: 12 });

    expect(result.skipped_operations).toEqual([]);
    expect(result.skipped_material_allocations).toEqual([]);
  });
});

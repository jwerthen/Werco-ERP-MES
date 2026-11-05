/**
 * Dispatch v2 client boundary — the PATHS and REQUEST BODIES of the plan verbs.
 *
 * The board components mock this module, so a wrong path or a misspelled body key
 * here would be green in every page suite and a 404 / 422 against the real API.
 * This pins the wire shape at its source:
 *
 * - `setWorkCenterRunOrder` stays BYTE-IDENTICAL to its pre-v2 body when no
 *   `expectedOperationIds` is passed (`{ operation_ids }` only — the server reads
 *   an absent CAS list as "last writer wins"), and sends the CAS list when given;
 * - the two new verbs send BOTH required lists under the server's key names;
 * - every write drops the cached board bodies (both views share the prefix).
 *
 * axios is mocked at the module boundary (same pattern as api.spc.test.ts).
 */

const mockGet = jest.fn();
const mockPut = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  put: mockPut,
  post: jest.fn(),
  patch: jest.fn(),
  delete: jest.fn(),
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
import type { DispatchLane, DispatchWorkOrderBoardResponse } from '../types';

const ok = (data: unknown) => ({ status: 200, data, headers: {} });

const board: DispatchWorkOrderBoardResponse = {
  generated_at: '2026-11-04T15:34:12Z',
  work_orders: [],
  ranked_count: 0,
  unranked_count: 0,
  work_centers: [],
};

const lane: DispatchLane = {
  work_order_id: 41,
  work_order_number: 'WO-20261104-001',
  version: 3,
  unit_number: '7',
  status: 'released',
  work_order_type: 'production',
  sequential_operations: false,
  is_laser: false,
  mode: 'pool',
  dispatch_position: 1,
  priority: 5,
  due_date: '2026-11-12',
  must_ship_by: null,
  promise_date: '2026-11-12',
  customer_name: 'Acme Trailer',
  part_number: 'BRKT-100',
  part_name: 'Bracket',
  quantity_ordered: 10,
  quantity_complete: 0,
  ops_total: 0,
  ops_complete: 0,
  open_blocker_count: 0,
  any_material_short: false,
  operations: [],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

describe('dispatch plan client', () => {
  it('reads the work-order board from the lanes route', async () => {
    mockGet.mockResolvedValue(ok(board));
    await expect(api.getDispatchWorkOrderBoard()).resolves.toEqual(board);
    expect(mockGet).toHaveBeenCalledWith('/shop-floor/dispatch-board/work-orders');
  });

  it('sends the full plan and the rendered plan under the server key names', async () => {
    mockPut.mockResolvedValue(ok(board));
    await expect(api.setDispatchWorkOrderOrder([41, 7], [7])).resolves.toEqual(board);
    expect(mockPut).toHaveBeenCalledWith('/shop-floor/dispatch-board/work-order-order', {
      work_order_ids: [41, 7],
      expected_work_order_ids: [7],
    });
  });

  it('sends an empty plan (clear) with both lists present', async () => {
    mockPut.mockResolvedValue(ok(board));
    await api.setDispatchWorkOrderOrder([], [41]);
    expect(mockPut).toHaveBeenCalledWith('/shop-floor/dispatch-board/work-order-order', {
      work_order_ids: [],
      expected_work_order_ids: [41],
    });
  });

  it('orders one work order’s operations and returns its lane', async () => {
    mockPut.mockResolvedValue(ok(lane));
    await expect(api.setDispatchOperationOrder(41, [102, 101], [101, 102])).resolves.toEqual(lane);
    expect(mockPut).toHaveBeenCalledWith('/shop-floor/dispatch-board/work-orders/41/operation-order', {
      operation_ids: [102, 101],
      expected_operation_ids: [101, 102],
    });
  });

  it('keeps the machine-column body byte-identical when no CAS list is passed', async () => {
    mockPut.mockResolvedValue(ok({ queue: [] }));
    await api.setWorkCenterRunOrder(2, [9, 11, 14]);
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe('/shop-floor/work-centers/2/run-order');
    // The key must be ABSENT, not null/undefined-valued: the server's CAS is opt-in.
    expect(body).toStrictEqual({ operation_ids: [9, 11, 14] });
    expect(Object.keys(body)).toEqual(['operation_ids']);
  });

  it('sends the machine-column CAS list when given (an empty rendered column included)', async () => {
    mockPut.mockResolvedValue(ok({ queue: [] }));
    await api.setWorkCenterRunOrder(2, [11, 9], [9, 11]);
    expect(mockPut).toHaveBeenLastCalledWith('/shop-floor/work-centers/2/run-order', {
      operation_ids: [11, 9],
      expected_operation_ids: [9, 11],
    });

    await api.setWorkCenterRunOrder(2, [], []);
    expect(mockPut).toHaveBeenLastCalledWith('/shop-floor/work-centers/2/run-order', {
      operation_ids: [],
      expected_operation_ids: [],
    });
  });

  it('propagates a refusal to the caller (the board re-reads on 409)', async () => {
    const conflict = {
      response: {
        status: 409,
        data: {
          detail: 'The dispatch board changed while you were editing. It has been refreshed — please try again.',
        },
      },
    };
    mockPut.mockRejectedValue(conflict);
    await expect(api.setDispatchWorkOrderOrder([41], [])).rejects.toBe(conflict);
  });
});

/**
 * Owner ruling 1 (short count at completion) — the office completion clients.
 *
 * `completeWOOperation` used to ALWAYS send `quantity_scrapped` (default 0), which
 * the server applied as an overwrite and so erased scrap the floor had recorded. Its
 * new contract: a plain call omits the param (the server leaves scrap untouched),
 * and the reason / code / override reason / acknowledgement ride only when the
 * caller actually supplies them. `completeWorkOrder` gains the override reason, and
 * the office reduce gains `reopen_operation`.
 */

const mockPost = jest.fn();
const mockAxiosInstance = {
  post: mockPost,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return { __esModule: true, default: { create, post: jest.fn() }, create };
});

import api from './api';

beforeEach(() => {
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: { message: 'Operation completed', is_fully_complete: true } });
});

describe('completeWOOperation', () => {
  it('omits quantity_scrapped by default so a plain call can never touch the floor scrap', async () => {
    await api.completeWOOperation(31, 10);

    expect(mockPost).toHaveBeenCalledWith('/work-orders/operations/31/complete', null, {
      params: { quantity_complete: 10 },
    });
  });

  it('omits quantity_scrapped for an explicit null as well', async () => {
    await api.completeWOOperation(31, 8, { quantityScrapped: null, scrapReason: 'stale', scrapReasonCodeId: 4 });

    const params = mockPost.mock.calls[0][2].params;
    expect(params).toEqual({ quantity_complete: 8 });
  });

  it('forwards the scrap total with its reason and code when the total rises', async () => {
    await api.completeWOOperation(31, 8, { quantityScrapped: 2, scrapReason: 'Burned edge', scrapReasonCodeId: 7 });

    expect(mockPost.mock.calls[0][2].params).toEqual({
      quantity_complete: 8,
      quantity_scrapped: 2,
      scrap_reason: 'Burned edge',
      scrap_reason_code_id: 7,
    });
  });

  it('forwards the override reason (trimmed) and the open-entry acknowledgement', async () => {
    await api.completeWOOperation(31, 10, {
      overrideReason: '  Inspector counted the tray  ',
      acknowledgeOpenEntries: true,
    });

    expect(mockPost.mock.calls[0][2].params).toEqual({
      quantity_complete: 10,
      quantity_override_reason: 'Inspector counted the tray',
      acknowledge_open_entries: true,
    });
  });

  it('never sends a blank override reason or a false acknowledgement', async () => {
    await api.completeWOOperation(31, 10, { overrideReason: '   ', acknowledgeOpenEntries: false });

    expect(mockPost.mock.calls[0][2].params).toEqual({ quantity_complete: 10 });
  });

  it('returns the server body (is_fully_complete false = saved but still open)', async () => {
    const body = {
      message: 'Progress updated',
      is_fully_complete: false,
      operation: { id: 31, status: 'in_progress', quantity_complete: 8, quantity_scrapped: 0, quantity_unaccounted: 2 },
      closed_time_entries: [],
      quality_exceptions: [],
    };
    mockPost.mockResolvedValueOnce({ data: body });

    expect(await api.completeWOOperation(31, 8)).toEqual(body);
  });
});

describe('completeWorkOrder override reason', () => {
  it('sends quantity_override_reason only when non-blank', async () => {
    await api.completeWorkOrder(42, 10, null, undefined, undefined, '  Op 20 finished the last 4  ');
    expect(mockPost).toHaveBeenLastCalledWith('/work-orders/42/complete', null, {
      params: { quantity_complete: 10, quantity_override_reason: 'Op 20 finished the last 4' },
    });

    await api.completeWorkOrder(42, 10, null, undefined, undefined, ' ');
    expect(mockPost.mock.calls[1][2].params).toEqual({ quantity_complete: 10 });
  });
});

describe('reduceWOOperationProduction reopen', () => {
  it('passes reopen_operation through in the body only when the caller sends it', async () => {
    await api.reduceWOOperationProduction(71, { quantity_delta: 3, reason: 'double-scanned', source: 'desktop' });
    expect(mockPost).toHaveBeenLastCalledWith('/work-orders/operations/71/reduce-production', {
      quantity_delta: 3,
      reason: 'double-scanned',
      source: 'desktop',
    });

    await api.reduceWOOperationProduction(71, {
      quantity_delta: 3,
      reason: 'double-scanned',
      source: 'desktop',
      reopen_operation: true,
    });
    expect(mockPost).toHaveBeenLastCalledWith('/work-orders/operations/71/reduce-production', {
      quantity_delta: 3,
      reason: 'double-scanned',
      source: 'desktop',
      reopen_operation: true,
    });
  });
});

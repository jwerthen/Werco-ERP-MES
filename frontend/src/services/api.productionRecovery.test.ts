const mockPost = jest.fn();
const mockAxiosInstance = {
  post: mockPost,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => ({
  __esModule: true,
  default: { create: jest.fn(() => mockAxiosInstance), post: jest.fn() },
}));

import api from './api';

const body = Object.freeze({
  request_id: 'original-production-attempt-0001',
  quantity_complete_delta: 3,
  quantity_scrapped_delta: 1,
  scrap_reason: 'Porosity',
  notes: 'Operator notes',
});

beforeEach(() => mockPost.mockReset());

test('production save has a bounded transport timeout and preserves receipt identity and all entered fields', async () => {
  const receipt = { request_id: body.request_id, replayed: true, operation: { id: 31, quantity_complete: 3 } };
  mockPost.mockResolvedValue({ data: receipt });
  expect(await api.reportOperationProduction(31, body)).toBe(receipt);
  expect(mockPost).toHaveBeenCalledWith('/shop-floor/operations/31/production', body, { timeout: 20000 });
  expect(mockPost.mock.calls[0][1]).toBe(body);
});

test('timeout is returned to recovery unchanged and does not silently send another additive report', async () => {
  const timeout = Object.assign(new Error('timeout of 20000ms exceeded'), { code: 'ECONNABORTED' });
  mockPost.mockRejectedValueOnce(timeout).mockResolvedValueOnce({ data: { request_id: body.request_id, replayed: true } });
  await expect(api.reportOperationProduction(31, body)).rejects.toBe(timeout);
  expect(mockPost).toHaveBeenCalledTimes(1);
  const receipt = await api.reportOperationProduction(31, body);
  expect(receipt.replayed).toBe(true);
  expect(mockPost.mock.calls[1]).toEqual(mockPost.mock.calls[0]);
});

test('correction transport is bounded and preserves the exact audited reason and delta', async () => {
  const correction = { quantity_delta: 2, reason: 'Entered twice', notes: 'Reviewed original count' };
  mockPost.mockResolvedValue({ data: { message: 'Production quantity corrected' } });
  await api.reduceOperationProduction(31, correction);
  expect(mockPost).toHaveBeenCalledWith('/shop-floor/operations/31/reduce-production', correction, { timeout: 20000 });
});

test('both scan lookup paths are bounded and typed resolution keeps caller cancellation and station context', async () => {
  const signal = new AbortController().signal;
  mockPost.mockResolvedValue({ data: { kind: 'unknown' } });
  await api.resolveScanAction('OP:31', 4, signal);
  expect(mockPost).toHaveBeenCalledWith('/scanner/resolve-action', { code: 'OP:31', work_center_id: 4 }, { signal, timeout: 20000 });
  await api.scannerLookup('WO-TEST-31');
  expect(mockPost).toHaveBeenCalledWith('/scanner/lookup', null, { params: { code: 'WO-TEST-31' }, timeout: 20000 });
});

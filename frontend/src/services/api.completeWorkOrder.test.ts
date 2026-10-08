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
  mockPost.mockResolvedValue({ data: { id: 42, status: 'complete' } });
});

it('omits the scrap total for quick completion so the server preserves recorded scrap', async () => {
  await api.completeWorkOrder(42, 10, null);

  expect(mockPost).toHaveBeenCalledWith('/work-orders/42/complete', null, {
    params: { quantity_complete: 10 },
  });
});

it('sends the entered scrap total and both reason fields when scrap is recorded', async () => {
  await api.completeWorkOrder(42, 8, 2, 'Surface scratches', 7);

  expect(mockPost).toHaveBeenCalledWith('/work-orders/42/complete', null, {
    params: {
      quantity_complete: 8,
      quantity_scrapped: 2,
      scrap_reason: 'Surface scratches',
      scrap_reason_code_id: 7,
    },
  });
});

it('retains the existing zero-scrap default for callers that omit the argument', async () => {
  await api.completeWorkOrder(42, 10);

  expect(mockPost).toHaveBeenCalledWith('/work-orders/42/complete', null, {
    params: { quantity_complete: 10, quantity_scrapped: 0 },
  });
});

it.each([null, 0])('does not submit stale scrap reasons when the scrap total is %s', async quantity => {
  await api.completeWorkOrder(42, 10, quantity, 'Old reason', 7);

  expect(mockPost.mock.calls[0][2].params).not.toHaveProperty('scrap_reason');
  expect(mockPost.mock.calls[0][2].params).not.toHaveProperty('scrap_reason_code_id');
});

it('passes the bypassed-step notice through to the caller', async () => {
  const response = {
    id: 42,
    status: 'complete',
    steps_bypassed: { count: 1, steps: [{ operation: '10', step_id: 1, label: 'Measure slot', serials: [] }] },
  };
  mockPost.mockResolvedValueOnce({ data: response });

  expect(await api.completeWorkOrder(42, 10, null)).toEqual(response);
});

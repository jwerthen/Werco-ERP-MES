const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('axios', () => ({
  __esModule: true,
  default: {
    create: () => ({
      post: mockPost,
      get: mockGet,
      defaults: { headers: { common: {} } },
      interceptors: { request: { use: jest.fn() }, response: { use: jest.fn() } },
    }),
  },
}));

import api from './api';
import type { NestingRunRequest } from '../types/nestingRun';

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

test('start retries preserve exact saved input identity and cancellation carries intended company and version', async () => {
  const request: NestingRunRequest = {
    draft_id: 41,
    revision_number: 3,
    expected_company_id: 2,
    input_sha256: 'a'.repeat(64),
    request_key: '12345678-1234-4234-8234-123456789abc',
  };
  const controller = new AbortController();
  const receipt = { id: 11, status: 'QUEUED' };
  mockPost.mockResolvedValue({ data: receipt });
  expect(await api.startNestingRun(request, controller.signal)).toBe(receipt);
  expect(await api.startNestingRun(request, controller.signal)).toBe(receipt);
  expect(mockPost.mock.calls.slice(0, 2)).toEqual([
    ['/quote-nesting/runs', request, { signal: controller.signal }],
    ['/quote-nesting/runs', request, { signal: controller.signal }],
  ]);
  await api.cancelNestingRun(11, 2, 7, controller.signal);
  expect(mockPost).toHaveBeenLastCalledWith(
    '/quote-nesting/runs/11/cancel',
    { expected_company_id: 2, expected_version: 7 },
    { signal: controller.signal }
  );
});

test('history and geometry reads use explicit revision/run/checkpoint paths and abort signals', async () => {
  const controller = new AbortController();
  mockGet.mockResolvedValue({ data: {} });
  await api.listNestingRuns(41, 3, 2, controller.signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/runs', {
    params: { draft_id: 41, revision_number: 3, page: 2, per_page: 10 },
    signal: controller.signal,
  });
  await api.getNestingRunCheckpoint(11, 4, controller.signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/runs/11/checkpoints/4', { signal: controller.signal });
  await api.getNestingRunReport(11, controller.signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/runs/11/report', {
    signal: controller.signal,
    timeout: 120000,
  });
  expect(mockPost).not.toHaveBeenCalled();
});

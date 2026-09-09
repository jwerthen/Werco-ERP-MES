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
import type { NestingSourceRequest } from '../types/nestingSource';

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

test('source writes retain exact revision identity, raw bytes, abort signal and expected-company transport', async () => {
  const request: NestingSourceRequest = {
    expected_company_id: 2,
    expected_input_sha256: 'a'.repeat(64),
    request_key: '11111111-1111-4111-8111-111111111111',
    source_sha256: 'b'.repeat(64),
    byte_count: 3,
    source_name: 'original.dxf',
    mime_type: 'application/dxf',
    targets: [{ group_id: 'g1', part_id: 'p1' }],
  };
  const response = { id: 51 };
  const signal = new AbortController().signal;
  const base = '/quote-nesting/drafts/41/revisions/1/sources';
  mockPost.mockResolvedValue({ data: response });
  expect(await api.createNestingSourceIntent(41, 1, request, signal)).toBe(response);
  expect(mockPost).toHaveBeenLastCalledWith(base, request, { signal });
  const bytes = new Uint8Array([0xef, 0xbb, 0xbf]).buffer;
  await api.uploadNestingSource(41, 1, 51, 2, bytes, signal);
  expect(mockPost).toHaveBeenLastCalledWith(`${base}/51/content`, bytes, {
    params: { expected_company_id: 2 },
    signal,
    timeout: 120_000,
    headers: { 'Content-Type': 'application/octet-stream' },
  });
  expect(mockPost.mock.calls[1][1]).toBe(bytes);
  await api.finalizeNestingSource(41, 1, 51, 2, signal);
  expect(mockPost).toHaveBeenLastCalledWith(
    `${base}/51/finalize`,
    { expected_company_id: 2 },
    { signal, timeout: 120_000 }
  );
});

test('source list and download stay behind authenticated client and bounded pagination', async () => {
  const signal = new AbortController().signal;
  mockGet.mockResolvedValue({ data: new Blob(['abc']) });
  await api.listNestingSources(41, 1, 2, signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/drafts/41/revisions/1/sources', {
    params: { page: 2, per_page: 10 },
    signal,
  });
  await api.downloadNestingSource(41, 1, 51, signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/drafts/41/revisions/1/sources/51/download', {
    signal,
    timeout: 120_000,
    responseType: 'blob',
  });
});

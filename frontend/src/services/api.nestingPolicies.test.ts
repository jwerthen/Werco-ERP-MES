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
import { policyContent, policyReceipt, policyState } from '../test-utils/nestingPolicyFixtures';
beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

test('policy writes retain company, CAS version, UUID and exact reviewed content across retries', async () => {
  mockPost.mockResolvedValue({ data: policyReceipt });
  const signal = new AbortController().signal;
  const base = {
    expected_company_id: 2,
    expected_version: 1,
    request_key: '12345678-1234-4234-8234-123456789abc',
    reason: 'Reviewed synthetic policy',
  };
  const publish = {
    ...base,
    revision_number: 1,
    content_sha256: policyReceipt.revision.content_sha256,
    effective_at: '2026-10-01T05:00:00Z',
  };
  expect(await api.publishNestingSpacingPolicy(publish, signal)).toBe(policyReceipt);
  await api.publishNestingSpacingPolicy(publish, signal);
  expect(mockPost.mock.calls).toEqual(
    Array(2).fill(['/quote-nesting/spacing-policies/publications', publish, { signal }])
  );
  await api.createNestingSpacingRevision({ ...base, content: policyContent }, signal);
  expect(mockPost).toHaveBeenLastCalledWith(
    '/quote-nesting/spacing-policies/revisions',
    { ...base, content: policyContent },
    { signal }
  );
  await api.withdrawNestingSpacingPolicy(62, base, signal);
  expect(mockPost).toHaveBeenLastCalledWith('/quote-nesting/spacing-policies/publications/62/withdraw', base, {
    signal,
  });
});

test('history and decimal resolution use bounded explicit routes and carry abort signals', async () => {
  const signal = new AbortController().signal;
  mockGet.mockResolvedValue({ data: policyState });
  expect(await api.getNestingSpacingPolicies(2, signal)).toBe(policyState);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/spacing-policies', {
    params: { page: 2, per_page: 20 },
    signal,
  });
  await api.getNestingSpacingRevision(7, signal);
  expect(mockGet).toHaveBeenLastCalledWith('/quote-nesting/spacing-policies/revisions/7', { signal });
  mockPost.mockResolvedValue({ data: { status: 'unmatched' } });
  const request = { material: 'Aluminum' as const, thickness_in: '0.333333333' };
  await api.resolveNestingSpacingPolicy(request, signal);
  expect(mockPost).toHaveBeenLastCalledWith('/quote-nesting/spacing-policies/resolve', request, { signal });
});

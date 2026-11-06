import {
  canWriteJobCosts,
  canPublishDocuments,
  canDeleteDocuments,
  canEditFAIs,
  canWriteQuotes,
} from './recordWriteAccess';

it('requires a signed-in actor and preserves explicit elevated server roles', () => {
  for (const check of [canWriteJobCosts, canPublishDocuments, canDeleteDocuments, canEditFAIs, canWriteQuotes]) {
    expect(check(null)).toBe(false);
    expect(check(undefined)).toBe(false);
    expect(check({ role: 'viewer' })).toBe(false);
    expect(check({ role: 'viewer', is_superuser: true })).toBe(true);
    expect(check({ role: 'platform_admin' })).toBe(true);
  }
});

it('quote writes match the server QUOTE_WRITE_ROLES gate', () => {
  for (const role of ['admin', 'manager', 'supervisor'] as const) expect(canWriteQuotes({ role })).toBe(true);
  for (const role of ['operator', 'quality', 'shipping', 'viewer'] as const)
    expect(canWriteQuotes({ role })).toBe(false);
});

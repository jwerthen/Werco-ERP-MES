import { canWriteJobCosts, canPublishDocuments, canDeleteDocuments, canEditFAIs } from './recordWriteAccess';

it('requires a signed-in actor and preserves explicit elevated server roles', () => {
  for (const check of [canWriteJobCosts, canPublishDocuments, canDeleteDocuments, canEditFAIs]) {
    expect(check(null)).toBe(false);
    expect(check(undefined)).toBe(false);
    expect(check({ role: 'viewer' })).toBe(false);
    expect(check({ role: 'viewer', is_superuser: true })).toBe(true);
    expect(check({ role: 'platform_admin' })).toBe(true);
  }
});

import {
  canWriteJobCosts,
  canPublishDocuments,
  canDeleteDocuments,
  canEditFAIs,
  canWriteQuotes,
  canApproveFAIs,
  canExportRecords,
} from './recordWriteAccess';

it('requires a signed-in actor and preserves explicit elevated server roles', () => {
  for (const check of [
    canWriteJobCosts,
    canPublishDocuments,
    canDeleteDocuments,
    canEditFAIs,
    canWriteQuotes,
    canApproveFAIs,
    canExportRecords,
  ]) {
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

it('FAI approval matches the server APPROVE_ROLES gate', () => {
  for (const role of ['admin', 'manager', 'quality', 'platform_admin'] as const)
    expect(canApproveFAIs({ role })).toBe(true);
  for (const role of ['supervisor', 'operator', 'shipping', 'viewer'] as const)
    expect(canApproveFAIs({ role })).toBe(false);
});

it('server exports match the EXPORT_ROLES gate (Quality is not in it)', () => {
  for (const role of ['admin', 'manager', 'platform_admin'] as const) expect(canExportRecords({ role })).toBe(true);
  for (const role of ['supervisor', 'quality', 'operator', 'shipping', 'viewer'] as const)
    expect(canExportRecords({ role })).toBe(false);
  expect(canExportRecords({ role: 'quality', is_superuser: true })).toBe(true);
});

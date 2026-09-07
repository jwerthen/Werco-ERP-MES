import { safeReturnPath } from './defaultLanding';
it.each(['https://evil.test', '//evil.test', '/\\evil.test', '/login?returnTo=/x', '/x\ny'])(
  'rejects unsafe or looping destination %s',
  path => expect(safeReturnPath(path)).toBeNull()
);
it('preserves a local record, query and fragment', () =>
  expect(safeReturnPath('/work-orders/42?tab=materials#issued')).toBe('/work-orders/42?tab=materials#issued'));

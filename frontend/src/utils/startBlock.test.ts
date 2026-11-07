import { startBlockedLabel } from './startBlock';

describe('startBlockedLabel', () => {
  it('names an unreleased work order', () => {
    expect(startBlockedLabel('draft')).toBe('Not released');
  });

  it('names a work-order hold', () => {
    expect(startBlockedLabel('on_hold')).toBe('WO on hold');
  });

  it('tolerates an upper-case wire value', () => {
    expect(startBlockedLabel('ON_HOLD')).toBe('WO on hold');
  });

  it.each(['complete', 'closed', 'cancelled', 'released', undefined, null, ''])(
    'stays neutral for %p (the server reason text says which)',
    status => {
      expect(startBlockedLabel(status as string | null | undefined)).toBe('Unavailable');
    }
  );
});

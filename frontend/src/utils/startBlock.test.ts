import { startBlockKind, startBlockedLabel } from './startBlock';
import { WORK_ORDER_NOT_RELEASED_REASON, WORK_ORDER_ON_HOLD_REASON } from '../components/kiosk/heldOperationFixtures';

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

describe('startBlockKind', () => {
  it('is null when the row carries no work-order refusal (never "startable")', () => {
    expect(startBlockKind('draft', null)).toBeNull();
    expect(startBlockKind('on_hold', '')).toBeNull();
    expect(startBlockKind('on_hold', '   ')).toBeNull();
    expect(startBlockKind(undefined, undefined)).toBeNull();
  });

  it('reads the structured work_order_status first', () => {
    expect(startBlockKind('draft', WORK_ORDER_NOT_RELEASED_REASON)).toBe('draft');
    expect(startBlockKind('ON_HOLD', WORK_ORDER_ON_HOLD_REASON)).toBe('on_hold');
  });

  it('a status the row DID carry wins over the text — terminal is "other"', () => {
    expect(startBlockKind('complete', 'cannot start operation: work order is complete')).toBe('other');
    // Even text that reads like a hold does not override a status that says otherwise.
    expect(startBlockKind('cancelled', WORK_ORDER_ON_HOLD_REASON)).toBe('other');
  });

  it("without a status, falls back to the server constants' text (MSG_WORK_ORDER_NOT_RELEASED / _ON_HOLD)", () => {
    expect(startBlockKind(null, WORK_ORDER_NOT_RELEASED_REASON)).toBe('draft');
    expect(startBlockKind(undefined, WORK_ORDER_ON_HOLD_REASON)).toBe('on_hold');
    // Robust to a rewording around the key phrases.
    expect(startBlockKind('', 'Work order is not released yet.')).toBe('draft');
    expect(startBlockKind('', 'Work order is on hold — a supervisor must Resume Work Order first')).toBe('on_hold');
  });

  it('text it cannot place is "other", so callers give generic advice', () => {
    expect(startBlockKind(null, 'cannot start operation: work order is closed')).toBe('other');
  });
});

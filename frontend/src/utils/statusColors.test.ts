/**
 * statusColors — canonical status-color source-of-truth tests.
 */

import {
  statusVariant,
  statusColor,
  statusColorMap,
  variantClass,
  UNKNOWN_STATUS_CLASS,
} from './statusColors';

describe('statusVariant', () => {
  it('resolves in_progress to exactly ONE color (blue) regardless of casing/format', () => {
    expect(statusVariant('in_progress')).toBe('blue');
    expect(statusVariant('In Progress')).toBe('blue');
    expect(statusVariant('IN-PROGRESS')).toBe('blue');
    // The whole point: it must not vary per page anymore.
    expect(statusColor('in_progress')).toBe(variantClass.blue);
  });

  it.each([
    // green — done / good / active-good
    ['completed', 'green'],
    ['released', 'green'],
    ['active', 'green'],
    ['approved', 'green'],
    ['passed', 'green'],
    ['accepted', 'green'],
    ['available', 'green'],
    // blue — in-flight good
    ['ready', 'blue'],
    ['sent', 'blue'],
    ['in_use', 'blue'],
    // amber — waiting / not-yet-started
    ['pending', 'amber'],
    ['draft', 'amber'],
    ['on_hold', 'amber'],
    ['scheduled', 'amber'],
    ['under_review', 'amber'],
    // red — bad / needs attention
    ['failed', 'red'],
    ['rejected', 'red'],
    ['overdue', 'red'],
    ['cancelled', 'red'],
    ['scrap', 'red'],
    ['open', 'red'],
    // slate — dormant / neutral terminal
    ['obsolete', 'slate'],
    ['inactive', 'slate'],
    ['void', 'slate'],
    ['retired', 'slate'],
    ['closed', 'slate'],
  ] as const)('maps domain status %s -> %s', (status, expected) => {
    expect(statusVariant(status)).toBe(expected);
  });

  it('falls back to slate for unknown / empty statuses', () => {
    expect(statusVariant('totally_made_up')).toBe('slate');
    expect(statusVariant('')).toBe('slate');
    expect(statusVariant(null)).toBe('slate');
    expect(statusVariant(undefined)).toBe('slate');
  });
});

describe('statusColor', () => {
  it('returns the variant bg/text class pair', () => {
    expect(statusColor('completed')).toBe(variantClass.green);
    expect(statusColor('pending')).toBe(variantClass.amber);
    expect(statusColor('unknown')).toBe(UNKNOWN_STATUS_CLASS);
  });

  it('every variant class is a bg/text pair in instrument-panel style', () => {
    Object.values(variantClass).forEach((cls) => {
      expect(cls).toMatch(/bg-/);
      expect(cls).toMatch(/text-/);
    });
  });
});

describe('statusColorMap', () => {
  it('is derived from statusVariant and cannot drift', () => {
    Object.entries(statusColorMap).forEach(([status, cls]) => {
      expect(cls).toBe(statusColor(status));
    });
  });

  it('includes the previously-disagreeing keys with one resolved value each', () => {
    expect(statusColorMap.in_progress).toBe(variantClass.blue);
    expect(statusColorMap.released).toBe(variantClass.green);
    expect(statusColorMap.cancelled).toBe(variantClass.red);
  });
});

describe('domain-semantic guards (centralization must not re-flatten these)', () => {
  // A down work center is an active alarm an operator must act on — not a dormant/neutral state.
  it('offline resolves to red', () => {
    expect(statusColor('offline')).toBe(variantClass.red);
  });
  // A damaged tool needs attention, like lost / needs_repair.
  it('damaged resolves to red', () => {
    expect(statusColor('damaged')).toBe(variantClass.red);
  });
});

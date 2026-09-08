import { autoQuotingSpacing } from './spacing';
import { inToMm, mmToIn } from './units';

describe('automatic quoting allowances', () => {
  it.each([
    [1 / 16, 1 / 8, 3 / 8],
    [1 / 8, 1 / 8, 3 / 8],
    [3 / 16, 3 / 16, 3 / 8],
    [1 / 4, 1 / 4, 1 / 2],
    [1 / 2, 1 / 2, 1],
    [1, 1, 2],
  ])('uses inch policy values for %s-inch material', (thickness, expectedGap, expectedMargin) => {
    const spacing = autoQuotingSpacing(inToMm(thickness));
    expect(mmToIn(spacing.gap)).toBeCloseTo(expectedGap, 12);
    expect(mmToIn(spacing.margin)).toBeCloseTo(expectedMargin, 12);
  });

  it('accepts metric thickness without rounding to an inch stock size', () => {
    expect(autoQuotingSpacing(6)).toEqual({ gap: 6, margin: 12 });
  });

  it('returns independent values for each material group', () => {
    const first = autoQuotingSpacing(6);
    first.gap = 99;
    first.margin = 99;
    expect(autoQuotingSpacing(6)).toEqual({ gap: 6, margin: 12 });
  });

  it.each([0, -1, NaN, Infinity, -Infinity, Number.MAX_VALUE])('rejects invalid thickness %s', thickness => {
    expect(() => autoQuotingSpacing(thickness)).toThrow();
  });
});

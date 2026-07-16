import { formatCurrency, formatPercent } from '../numberFormat';

describe('formatCurrency', () => {
  it('formats with thousands separators and two decimals', () => {
    expect(formatCurrency(2592)).toBe('$2,592.00');
  });

  it('formats millions with grouped separators', () => {
    expect(formatCurrency(1234567.89)).toBe('$1,234,567.89');
  });

  it('formats zero as $0.00', () => {
    expect(formatCurrency(0)).toBe('$0.00');
  });

  it('treats null, undefined, and NaN as zero (never renders "NaN")', () => {
    expect(formatCurrency(null)).toBe('$0.00');
    expect(formatCurrency(undefined)).toBe('$0.00');
    expect(formatCurrency(NaN)).toBe('$0.00');
  });

  it('formats negative amounts with a leading minus', () => {
    expect(formatCurrency(-42.5)).toBe('-$42.50');
    expect(formatCurrency(-1234.56)).toBe('-$1,234.56');
  });

  it('rounds to the nearest cent', () => {
    expect(formatCurrency(1.991)).toBe('$1.99');
    expect(formatCurrency(1.999)).toBe('$2.00');
  });

  it('rounds an exact half-cent up (0.125 is exactly representable in binary)', () => {
    // Intl.NumberFormat defaults to halfExpand rounding.
    expect(formatCurrency(0.125)).toBe('$0.13');
  });

  it('keeps sub-dollar amounts two-decimal', () => {
    expect(formatCurrency(0.5)).toBe('$0.50');
  });
});

describe('formatPercent', () => {
  it('rounds long decimals to two places', () => {
    expect(formatPercent(42.6767676767)).toBe('42.68%');
  });

  it('renders whole numbers without decimals', () => {
    expect(formatPercent(85)).toBe('85%');
  });

  it('strips trailing zeros but keeps significant decimals', () => {
    expect(formatPercent(90.5)).toBe('90.5%');
  });

  it('strips a decimal part that rounds away entirely', () => {
    expect(formatPercent(85.001)).toBe('85%');
    expect(formatPercent(99.999)).toBe('100%');
  });

  it('treats null, undefined, and NaN as zero', () => {
    expect(formatPercent(null)).toBe('0%');
    expect(formatPercent(undefined)).toBe('0%');
    expect(formatPercent(NaN)).toBe('0%');
  });

  it('renders zero as 0%', () => {
    expect(formatPercent(0)).toBe('0%');
  });

  it('handles the 42.675 edge deterministically (float is just below the half)', () => {
    // The double closest to 42.675 is 42.67499999999999715...; toFixed rounds
    // the exact double value, so this is stably "42.67" on every engine.
    expect(formatPercent(42.675)).toBe('42.67%');
  });

  it('respects a custom maxDecimals', () => {
    expect(formatPercent(33.35, 1)).toBe('33.4%');
    expect(formatPercent(42.6767, 0)).toBe('43%');
    expect(formatPercent(12.3456, 3)).toBe('12.346%');
  });

  it('formats negative percentages', () => {
    expect(formatPercent(-5)).toBe('-5%');
    expect(formatPercent(-12.344)).toBe('-12.34%');
  });
});

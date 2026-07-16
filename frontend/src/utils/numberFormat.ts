/**
 * Shared display formatters for dollar amounts and percentages.
 *
 * Both treat null/undefined/NaN as zero rather than rendering "NaN" — the
 * extraction pipeline can legitimately hand back missing numeric fields.
 */

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
});

/** "$2,592.00"-style USD with thousands separators; null/undefined/NaN -> "$0.00". */
export function formatCurrency(value: number | null | undefined): string {
  const n = typeof value === 'number' && !Number.isNaN(value) ? value : 0;
  return usdFormatter.format(n);
}

/**
 * Percentage rounded to at most `maxDecimals` places with trailing zeros
 * stripped: 42.6767676767 -> "42.68%", 85 -> "85%", 90.5 -> "90.5%";
 * null/undefined/NaN -> "0%".
 */
export function formatPercent(value: number | null | undefined, maxDecimals = 2): string {
  const n = typeof value === 'number' && !Number.isNaN(value) ? value : 0;
  // Number(toFixed) drops trailing zeros ("85.00" -> 85, "90.50" -> 90.5).
  return `${Number(n.toFixed(maxDecimals))}%`;
}

/** Whether increasing a metric represents an improvement. Keep KPI consumers consistent. */
export const METRIC_POLARITY = {
  oee: 'higher',
  on_time_delivery: 'higher',
  on_time_delivery_ship: 'higher',
  otif: 'higher',
  first_pass_yield: 'higher',
  yield: 'higher',
  quote_win_rate: 'higher',
  backlog_hours: 'higher',
  inventory_turnover: 'higher',
  scrap_rate: 'lower',
  open_ncrs: 'lower',
  total_ncrs: 'lower',
  ncr_count: 'lower',
  reject_rate: 'lower',
  receiving_reject_rate: 'lower',
  defect_rate: 'lower',
} as const;

export type MetricKey = keyof typeof METRIC_POLARITY;
type MetricComparison = {
  value: number | null;
  prior_value: number | null;
  change_pct: number | null;
};

export function getMetricTrend(metric: MetricKey, comparison: MetricComparison) {
  // The API's scrap/NCR `trend` is already inverted for goodness. Use signed
  // change instead so the arrow describes movement, then apply polarity once.
  // A zero percent change with a zero prior value can still be a real increase.
  const { value, prior_value: prior, change_pct: change } = comparison;
  const delta =
    change != null && Number.isFinite(change) && change !== 0
      ? change
      : value != null && prior != null && Number.isFinite(value) && Number.isFinite(prior)
        ? value - prior
        : 0;
  const direction = delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
  const improving = (METRIC_POLARITY[metric] === 'higher') === (direction === 'up');
  const color = direction === 'flat' ? 'text-slate-400' : improving ? 'text-green-500' : 'text-red-500';
  return { direction, color };
}

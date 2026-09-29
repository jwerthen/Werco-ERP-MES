import { getMetricTrend, MetricKey } from './metricPolarity';

test.each<MetricKey>(['scrap_rate', 'open_ncrs', 'total_ncrs', 'ncr_count', 'reject_rate', 'receiving_reject_rate', 'defect_rate'])(
  '%s treats increasing losses as danger and decreasing losses as success',
  metric => {
    expect(getMetricTrend(metric, { value: 4, prior_value: 2, change_pct: 100 })).toEqual({ direction: 'up', color: 'text-red-500' });
    expect(getMetricTrend(metric, { value: 2, prior_value: 4, change_pct: -50 })).toEqual({ direction: 'down', color: 'text-green-500' });
  }
);

test.each<MetricKey>(['oee', 'on_time_delivery', 'on_time_delivery_ship', 'otif', 'first_pass_yield', 'yield', 'quote_win_rate'])(
  '%s treats increasing performance as success',
  metric => {
    expect(getMetricTrend(metric, { value: 95, prior_value: 90, change_pct: 5.6 })).toEqual({ direction: 'up', color: 'text-green-500' });
  }
);

test('unavailable comparisons and unchanged measurements remain neutral', () => {
  expect(getMetricTrend('scrap_rate', { value: null, prior_value: null, change_pct: null })).toEqual({ direction: 'flat', color: 'text-slate-400' });
  expect(getMetricTrend('scrap_rate', { value: 2, prior_value: 2, change_pct: 0 })).toEqual({ direction: 'flat', color: 'text-slate-400' });
});

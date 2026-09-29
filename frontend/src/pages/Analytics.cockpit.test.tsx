/**
 * Analytics cockpit — instrument-panel overview regression.
 *
 * The overview view was overhauled into a compact cockpit: a `MiniStatStrip`
 * of KPI tiles up top, then a two-panel `CockpitPanel` grid (Production Trends
 * chart + Capacity Forecast). The old redundant "Quick Links" navigation row
 * was removed. This guards that strip + the two panels render, and that the
 * Quick Links row stays gone.
 *
 * jsdom has no ResizeObserver and setupTests does not mock it; recharts'
 * ResponsiveContainer needs one, so we stub it at the top of the file.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Analytics from './Analytics';

// recharts ResponsiveContainer relies on ResizeObserver, absent in jsdom.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getKPIDashboard: jest.fn(),
    getCapacityForecast: jest.fn(),
    getProductionTrends: jest.fn(),
  },
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 't',
  buildWsUrl: () => 'ws://localhost/ws',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const kpi = (value: number | null, target: number | null = 90) => ({
  value,
  target,
  prior_value: value,
  change_pct: 2.5,
  trend: 'up' as const,
  sparkline: [1, 2, 3, 4],
});

const kpiDashboard = {
  oee: kpi(82.4),
  on_time_delivery: kpi(95.1),
  first_pass_yield: kpi(98.2),
  scrap_rate: kpi(1.3, 2),
  open_ncrs: { ...kpi(4, 0), trend: 'down' as const },
  quote_win_rate: kpi(33.0),
  backlog_hours: kpi(120),
  inventory_turnover: kpi(4.2),
  period_start: '2026-05-29',
  period_end: '2026-06-28',
};

const capacityForecast = {
  weeks: [
    {
      week_start: '2026-06-29',
      week_end: '2026-07-05',
      overall_utilization: 72,
      work_centers: [
        {
          work_center_id: 1,
          work_center_name: 'Laser cell 1',
          committed_hours: 30,
          available_hours: 40,
          utilization_pct: 75,
          is_overloaded: false,
        },
      ],
    },
  ],
  alerts: [],
};

const productionTrends = {
  time_series: [
    { date: '2026-06-27', units_produced: 100, units_scrapped: 2, total_hours: 8 },
    { date: '2026-06-28', units_produced: 120, units_scrapped: 3, total_hours: 8 },
  ],
  totals: {},
};

const renderAnalytics = () =>
  render(
    <MemoryRouter initialEntries={['/analytics']}>
      <Analytics />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getKPIDashboard.mockResolvedValue(kpiDashboard as any);
  mockedApi.getCapacityForecast.mockResolvedValue(capacityForecast as any);
  mockedApi.getProductionTrends.mockResolvedValue(productionTrends as any);
});

test('renders the MiniStat KPI strip after load', async () => {
  renderAnalytics();
  // The dashboard heading confirms the overview loaded (not the spinner).
  expect(await screen.findByText('Analytics Dashboard')).toBeInTheDocument();

  // A representative spread of the KPI tiles in the strip.
  expect(screen.getByText('OEE')).toBeInTheDocument();
  expect(screen.getByText('On-Time Delivery')).toBeInTheDocument();
  expect(screen.getByText('First Pass Yield')).toBeInTheDocument();
  expect(screen.getByText('Inventory Turnover')).toBeInTheDocument();
  // A formatted KPI value renders inside the strip.
  expect(screen.getByText('82.4%')).toBeInTheDocument();
});

test('renders the two cockpit panels in the overview grid', async () => {
  renderAnalytics();
  expect(await screen.findByText('Analytics Dashboard')).toBeInTheDocument();

  // CockpitPanel titles render as card headings.
  expect(screen.getByText('Production Trends')).toBeInTheDocument();
  expect(screen.getByText('Capacity Forecast (4 Weeks)')).toBeInTheDocument();
  // The capacity panel renders its work-center row from the loaded data.
  expect(screen.getByText('Laser cell 1')).toBeInTheDocument();
});

test('the redundant Quick Links row is gone', async () => {
  renderAnalytics();
  await screen.findByText('Analytics Dashboard');

  expect(screen.queryByText(/quick links/i)).toBeNull();
});

test('promotes a severe OTD target miss above the KPI strip', async () => {
  mockedApi.getKPIDashboard.mockResolvedValue({ ...kpiDashboard, on_time_delivery: kpi(44.4, 95), oee: kpi(null) } as any);
  renderAnalytics();
  const attention = await screen.findByRole('region', { name: 'Critical KPI target misses' });
  expect(within(attention).getByText('On-Time Delivery: 44.4%')).toBeInTheDocument();
  expect(within(attention).queryByText(/OEE/)).not.toBeInTheDocument();
});

test('shows turnover as an unverified annualized estimate with the known denominator issue', async () => {
  mockedApi.getKPIDashboard.mockResolvedValue({ ...kpiDashboard, inventory_turnover: kpi(913.59, 4) } as any);
  renderAnalytics();
  const value = await screen.findByText('913.59');
  const tile = value.closest('.card') as HTMLElement;
  expect(within(tile).getByText('Unverified')).toBeInTheDocument();
  expect(within(tile).getByText(/Uses average inventory-row value, not average total inventory/)).toBeInTheDocument();
  expect(within(tile).getByText(/Not comparable to the 4.00×\/year turnover target/)).toBeInTheDocument();
  expect(within(tile).getByText(/vs prior/)).toHaveClass('text-slate-400');
  expect(tile.querySelector('.text-emerald-400')).toBeNull();
});

test('warns about a discontinuous production spike with its unchanged date and quantity', async () => {
  mockedApi.getProductionTrends.mockResolvedValue({ time_series: [{ date: '2026-09-01', units_produced: 0 }, { date: '2026-09-02', units_produced: 800 }], totals: {} } as any);
  renderAnalytics();
  const warning = await screen.findByRole('note', { name: 'Production data review' });
  expect(within(warning).getByText(/0 → 800 units/)).toBeInTheDocument();
  expect(within(warning).getByText(/original values are shown/)).toBeInTheDocument();
});

test.each([
  { metric: 'scrap_rate', title: 'Scrap Rate', value: 3.59, prior: 1.3, change: 176.3, apiTrend: 'down', movement: 'increased', color: 'text-red-500' },
  { metric: 'scrap_rate', title: 'Scrap Rate', value: 1.3, prior: 3.59, change: -63.8, apiTrend: 'up', movement: 'decreased', color: 'text-green-500' },
  { metric: 'open_ncrs', title: 'Open NCRs', value: 4, prior: 2, change: 100, apiTrend: 'down', movement: 'increased', color: 'text-red-500' },
  { metric: 'open_ncrs', title: 'Open NCRs', value: 2, prior: 4, change: -50, apiTrend: 'up', movement: 'decreased', color: 'text-green-500' },
  { metric: 'first_pass_yield', title: 'First Pass Yield', value: 98, prior: 90, change: 8.9, apiTrend: 'up', movement: 'increased', color: 'text-green-500' },
  { metric: 'on_time_delivery', title: 'On-Time Delivery', value: 90, prior: 95, change: -5.3, apiTrend: 'down', movement: 'decreased', color: 'text-red-500' },
])('$title $movement uses metric polarity even when the API trend is already flipped', async ({ metric, title, value, prior, change, apiTrend, movement, color }) => {
  mockedApi.getKPIDashboard.mockResolvedValue({
    ...kpiDashboard,
    [metric]: { ...kpi(value), prior_value: prior, change_pct: change, trend: apiTrend },
  } as any);
  renderAnalytics();
  await screen.findByText('Analytics Dashboard');
  const trend = screen.getByLabelText(`${title} ${movement} from prior period`);
  expect(within(trend).getByText(/vs prior/)).toHaveClass(color);
  expect(trend.querySelector('svg')).toHaveClass(color);
});

test.each([
  { value: 2, prior: 0 },
  { value: 0, prior: 2 },
  { value: 0, prior: 0 },
  { value: null, prior: 2 },
])('hides percentage comparison when current $value or prior $prior is zero or unavailable', async ({ value, prior }) => {
  mockedApi.getKPIDashboard.mockResolvedValue({ ...kpiDashboard, open_ncrs: { ...kpi(value, 0), prior_value: prior, change_pct: 100, trend: 'down' } } as any);
  renderAnalytics();
  const title = await screen.findByText('Open NCRs');
  const tile = title.closest('.card') as HTMLElement;
  expect(within(tile).queryByText(/vs prior/)).not.toBeInTheDocument();
  expect(within(tile).getByText(value === null ? 'n/a' : String(value))).toBeInTheDocument();
});

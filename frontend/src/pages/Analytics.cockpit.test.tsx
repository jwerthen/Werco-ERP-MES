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
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Analytics from './Analytics';

// recharts ResponsiveContainer relies on ResizeObserver, absent in jsdom.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

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

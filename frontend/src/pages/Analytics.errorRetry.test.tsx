/**
 * Analytics — load-failure banner uses the shared <ErrorState> with a working
 * Retry that re-runs loadData(true).
 *
 * The bare red banner div was replaced with <ErrorState>, kept in the same
 * banner slot (not a full-page swap) because the content below may be
 * stale-but-present — the Reports.tsx degradation posture.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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

const capacityForecast = { weeks: [], alerts: [] };
const productionTrends = { time_series: [], totals: {} };

const http = (status: number, detail: string) => {
  const err = new Error(detail) as Error & {
    response: { status: number; data: { detail: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const renderAnalytics = () =>
  render(
    <MemoryRouter initialEntries={['/analytics']}>
      <Analytics />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getCapacityForecast.mockResolvedValue(capacityForecast as any);
  mockedApi.getProductionTrends.mockResolvedValue(productionTrends as any);
});

test('a failed load renders ErrorState and Retry re-runs the fetch to success', async () => {
  mockedApi.getKPIDashboard
    .mockRejectedValueOnce(http(500, 'KPI backend unavailable'))
    .mockResolvedValueOnce(kpiDashboard as any);

  renderAnalytics();

  // The shared ErrorState renders in the banner slot with the server detail.
  const errorState = await screen.findByTestId('error-state');
  expect(errorState).toHaveTextContent('KPI backend unavailable');

  // Retry re-runs loadData(true); the second call succeeds and content renders.
  fireEvent.click(screen.getByRole('button', { name: /retry/i }));

  await waitFor(() => expect(mockedApi.getKPIDashboard).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('OEE')).toBeInTheDocument();
  expect(screen.getByText('82.4%')).toBeInTheDocument();
  // The banner clears once the reload succeeds.
  expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
});

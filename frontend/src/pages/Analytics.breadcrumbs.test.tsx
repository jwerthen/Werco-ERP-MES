/**
 * Analytics — breadcrumb mount behavior.
 *
 * One unconditional `{crumbParent && <Breadcrumbs …>}` mount covers all seven
 * /analytics/* sub-views. This locks both halves of that contract:
 *   1. The bare /analytics hub renders NO breadcrumb nav (it is a top-level
 *      page — getBreadcrumbParent returns null there).
 *   2. A sub-view (/analytics/production) renders exactly one, linking back
 *      up to the Analytics hub with the routeMeta leaf title.
 *
 * Mock scaffold mirrors Analytics.cockpit.test.tsx (recharts needs a
 * ResizeObserver stub in jsdom).
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

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getKPIDashboard: jest.fn(),
    getCapacityForecast: jest.fn(),
    getProductionTrends: jest.fn(),
    getOEEDetails: jest.fn(),
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

const productionTrends = {
  time_series: [
    { date: '2026-06-27', units_produced: 100, units_scrapped: 2, total_hours: 8 },
  ],
  totals: {},
};

const oeeDetails = {
  summary: { availability: 91.2, performance: 88.4, quality: 99.1, oee: 79.9 },
  time_series: [],
};

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Analytics />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getKPIDashboard.mockResolvedValue(kpiDashboard as any);
  mockedApi.getCapacityForecast.mockResolvedValue(capacityForecast as any);
  mockedApi.getProductionTrends.mockResolvedValue(productionTrends as any);
  mockedApi.getOEEDetails.mockResolvedValue(oeeDetails as any);
});

test('the bare /analytics hub renders no breadcrumb nav', async () => {
  renderAt('/analytics');
  expect(await screen.findByText('Analytics Dashboard')).toBeInTheDocument();

  expect(screen.queryByRole('navigation', { name: /breadcrumb/i })).toBeNull();
});

test('a sub-view renders one breadcrumb linking back to the Analytics hub', async () => {
  renderAt('/analytics/production');
  // The production view has loaded once its OEE cards render.
  expect(await screen.findByText('Availability')).toBeInTheDocument();

  const nav = screen.getByRole('navigation', { name: /breadcrumb/i });
  const parentLink = screen.getByRole('link', { name: 'Analytics' });
  expect(nav).toContainElement(parentLink);
  expect(parentLink).toHaveAttribute('href', '/analytics');
  // Leaf label resolves from the same routeMeta source as the tab title.
  expect(nav).toHaveTextContent('Production Analytics');
});

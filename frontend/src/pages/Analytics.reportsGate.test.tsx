/**
 * Analytics — Custom Reports gating.
 *
 * Every report-builder endpoint is ADMIN / MANAGER, which is exactly `analytics:export`.
 * The tab, the route (routeAccess.ts) and the page's own loading all key off that one
 * permission, so a Supervisor / Operator / Viewer is never shown a link to a screen of
 * 403s and no report endpoint is called for anyone outside the reports view.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Analytics from './Analytics';
import { canAccessPath } from '../utils/routeAccess';
import { getPermissionsForRole, type Permission } from '../utils/permissions';
import type { UserRole } from '../types';

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

let mockGranted: (permission: string) => boolean = () => true;
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({
    can: (permission: string) => mockGranted(permission),
    canAny: (permissions: string[]) => permissions.some(permission => mockGranted(permission)),
  }),
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 7, role: 'manager', is_superuser: false } }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getKPIDashboard: jest.fn(),
    getCapacityForecast: jest.fn(),
    getProductionTrends: jest.fn(),
    getDataSources: jest.fn(),
    listReportTemplates: jest.fn(),
  },
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 't',
  buildWsUrl: () => 'ws://localhost/ws',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const kpi = { value: 90, target: 90, prior_value: 90, change_pct: 0, trend: 'flat' as const, sparkline: [] };

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Analytics />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockGranted = () => true;
  mockedApi.getKPIDashboard.mockResolvedValue({
    oee: kpi,
    on_time_delivery: kpi,
    first_pass_yield: kpi,
    scrap_rate: kpi,
    open_ncrs: kpi,
    quote_win_rate: kpi,
    backlog_hours: kpi,
    inventory_turnover: kpi,
    period_start: '2026-10-01',
    period_end: '2026-10-31',
  });
  mockedApi.getCapacityForecast.mockResolvedValue({ weeks: [], alerts: [] });
  mockedApi.getProductionTrends.mockResolvedValue({ time_series: [], totals: {} });
  mockedApi.getDataSources.mockResolvedValue({});
  mockedApi.listReportTemplates.mockResolvedValue([]);
});

test('shows the Custom Reports tab with analytics:export and never loads report data on another view', async () => {
  renderAt('/analytics');
  expect(await screen.findByRole('button', { name: 'Custom Reports' })).toBeInTheDocument();
  expect(mockedApi.getDataSources).not.toHaveBeenCalled();
  expect(mockedApi.listReportTemplates).not.toHaveBeenCalled();
});

test('hides the Custom Reports tab without analytics:export', async () => {
  mockGranted = permission => permission !== 'analytics:export';
  renderAt('/analytics');
  expect(await screen.findByRole('button', { name: 'Production' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Custom Reports' })).not.toBeInTheDocument();
});

test('the reports view mounts the builder (which loads its own data) only when permitted', async () => {
  renderAt('/analytics/reports');
  expect(await screen.findByRole('heading', { name: 'Report builder' })).toBeInTheDocument();
  await waitFor(() => expect(mockedApi.getDataSources).toHaveBeenCalledTimes(1));
  expect(mockedApi.listReportTemplates).toHaveBeenCalledTimes(1);
  // The page-level loaders are not run for the reports view.
  expect(mockedApi.getKPIDashboard).not.toHaveBeenCalled();
  // No page Refresh / period selector there: the builder reloads its own data.
  expect(screen.queryByRole('button', { name: /Refresh/ })).not.toBeInTheDocument();
});

test('the reports view calls no report endpoint for a user without analytics:export', async () => {
  mockGranted = permission => permission !== 'analytics:export';
  renderAt('/analytics/reports');
  expect(await screen.findByRole('heading', { name: 'Custom Reports' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Report builder' })).not.toBeInTheDocument();
  expect(mockedApi.getDataSources).not.toHaveBeenCalled();
  expect(mockedApi.listReportTemplates).not.toHaveBeenCalled();
});

describe('routeAccess: /analytics/reports follows the report endpoints (ADMIN / MANAGER)', () => {
  const allows = (role: UserRole, path: string) =>
    canAccessPath(path, (permission: Permission) => getPermissionsForRole(role).includes(permission));

  it.each<UserRole>(['platform_admin', 'admin', 'manager'])('lets %s reach Custom Reports', role => {
    expect(allows(role, '/analytics/reports')).toBe(true);
    expect(allows(role, '/analytics/reports?template=12')).toBe(true);
  });

  it.each<UserRole>(['supervisor', 'operator', 'quality', 'shipping', 'viewer'])(
    'refuses Custom Reports to %s but keeps the rest of Analytics',
    role => {
      expect(allows(role, '/analytics/reports')).toBe(false);
      expect(allows(role, '/analytics/reports?template=12')).toBe(false);
      expect(allows(role, '/analytics')).toBe(true);
      expect(allows(role, '/analytics/production')).toBe(true);
    }
  );
});

/**
 * Analytics — Custom Reports gating, per REAL role.
 *
 * Analytics.reportsGate.test.tsx proves the tab follows `can('analytics:export')` with a
 * mocked `can`. This file runs the REAL usePermissions hook for every role the app knows,
 * plus a superuser, so the chain role → permission set → tab → route is pinned end to end
 * against the server's rule: every report-builder endpoint is require_role([ADMIN,
 * MANAGER]), which also waves through PLATFORM_ADMIN and any superuser.
 *
 * The role list is derived from ROLE_LABELS, so a role added later must be classified
 * here before this suite passes.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Analytics from './Analytics';
import { canAccessPath } from '../utils/routeAccess';
import { hasPermission, ROLE_LABELS } from '../utils/permissions';
import type { UserRole } from '../types';

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

let mockUser: { id: number; role: UserRole; is_superuser: boolean } = { id: 7, role: 'manager', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser }),
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

// The server's rule: require_role([ADMIN, MANAGER]) plus the platform principal it always admits.
const REPORT_ROLES: UserRole[] = ['platform_admin', 'admin', 'manager'];
const ALL_ROLES = Object.keys(ROLE_LABELS) as UserRole[];

beforeEach(() => {
  jest.clearAllMocks();
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

const renderAnalyticsAs = (role: UserRole, isSuperuser = false) => {
  mockUser = { id: 7, role, is_superuser: isSuperuser };
  return render(
    <MemoryRouter initialEntries={['/analytics']}>
      <Analytics />
    </MemoryRouter>
  );
};

test('every known role is covered and only the report roles hold analytics:export', () => {
  expect(ALL_ROLES).toHaveLength(8);
  expect(ALL_ROLES.filter(role => hasPermission(role, 'analytics:export')).sort()).toEqual(
    [...REPORT_ROLES].sort()
  );
});

describe.each(ALL_ROLES)('role %s', role => {
  const allowed = REPORT_ROLES.includes(role);

  it(`${allowed ? 'shows' : 'hides'} the Custom Reports tab and ${allowed ? 'admits' : 'refuses'} the route`, async () => {
    renderAnalyticsAs(role);
    // The page has rendered its section tabs once Production is there.
    expect(await screen.findByRole('button', { name: 'Production' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Custom Reports' }) !== null).toBe(allowed);
    expect(canAccessPath('/analytics/reports', permission => hasPermission(role, permission))).toBe(allowed);
    // No report endpoint is called from the hub, whatever the role.
    expect(mockedApi.getDataSources).not.toHaveBeenCalled();
    expect(mockedApi.listReportTemplates).not.toHaveBeenCalled();
  });
});

test('a superuser sees the tab whatever its role, as require_role admits a superuser', async () => {
  renderAnalyticsAs('supervisor', true);
  expect(await screen.findByRole('button', { name: 'Custom Reports' })).toBeInTheDocument();
});

/**
 * Customizable dashboard, page level: a saved layout decides what renders and what is
 * fetched; cross-links and links degrade instead of dead-ending; an empty layout and a
 * layout that cannot load both leave the user somewhere useful; Customize round-trips.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../types/dashboardLayout';
import type { WallboardResponse } from '../types/wallboard';
import { setCustomPermissions } from '../utils/permissions';
import { clearDashboardSourceCache } from '../components/dashboard/useDashboardSources';
import Dashboard from './Dashboard';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardWithCache: jest.fn(),
    getQualitySummary: jest.fn(),
    getEquipmentDueSoon: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getCapacityHeatmap: jest.fn(),
    getDashboardLayout: jest.fn(),
    saveDashboardLayout: jest.fn(),
    resetDashboardLayout: jest.fn(),
    getWallboard: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 1, role: 'manager', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../components/cockpit', () => {
  const actual = jest.requireActual('../components/cockpit');
  return { __esModule: true, ...actual, SetupNudge: () => null };
});
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const dashboardData = {
  summary: {
    active_work_orders: 42,
    due_today: 5,
    overdue: 3,
    signed_in_users: 2,
    checked_in_users: 1,
    idle_signed_in_users: 1,
    completed_today: 7,
  },
  work_centers: [
    {
      id: 1,
      code: 'LASER-1',
      name: 'Laser cell 1',
      type: 'laser',
      status: 'in_use',
      active_operations: 2,
      queued_operations: 3,
      active_people_count: 1,
      active_people: [],
    },
  ],
  signed_in_users: [
    {
      id: 11,
      employee_id: 'E011',
      name: 'Alex Reyes',
      role: 'operator',
      department: 'Production',
      connected_since: '2026-06-28T16:00:00Z',
      has_active_job: true,
      active_job_count: 1,
      active_work_centers: ['Laser cell 1'],
      active_work_orders: ['WO-3041'],
    },
    {
      id: 21,
      employee_id: 'E021',
      name: 'Lena Chen',
      role: 'supervisor',
      department: 'Production',
      connected_since: '2026-06-28T15:00:00Z',
      has_active_job: false,
      active_job_count: 0,
      active_work_centers: [],
      active_work_orders: [],
    },
  ],
  active_assignments: [
    {
      time_entry_id: 101,
      clock_in: '2026-06-28T18:00:00Z',
      entry_type: 'run',
      user: { id: 11, employee_id: 'E011', name: 'Alex Reyes', role: 'operator', department: 'Production' },
      work_order: {
        id: 3041,
        work_order_number: 'WO-3041',
        status: 'in_progress',
        part_number: 'PN-880',
        part_name: 'Mount bracket',
        priority: 2,
        due_date: '2026-07-03',
        quantity_ordered: 100,
        quantity_complete: 70,
      },
      operation: { id: 1, operation_number: '10', name: 'Laser cut', status: 'in_progress', sequence: 1 },
      work_center: { id: 1, code: 'LASER-1', name: 'Laser cell 1', status: 'in_use', type: 'laser' },
    },
  ],
  recent_completions: [],
};

const layoutResponse = (layout: DashboardLayoutValues | null, overrides: Partial<DashboardLayoutResponse> = {}) => ({
  company_id: 1,
  version: layout ? 2 : 0,
  layout,
  updated_at: null,
  can_edit: true,
  ...overrides,
});

const layoutOf = (...widgets: { id: string; size?: string }[]): DashboardLayoutValues => ({
  format_version: 1,
  widgets,
});

const httpError = (status: number, detail = 'Request failed') =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

const renderDashboard = () =>
  render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>
  );

/** Wait until the page left its loading state. */
const loaded = () => screen.findByRole('button', { name: 'Refresh dashboard' }).then(() => waitForLoaded());
const waitForLoaded = () => waitFor(() => expect(screen.queryByText('Loading dashboard…')).not.toBeInTheDocument());

const kpiStrip = () => document.querySelector('.grid.grid-cols-2[data-tour="dashboard-stats"]') as HTMLElement | null;
const stripLabels = () =>
  Array.from(kpiStrip()?.children ?? []).map(child => child.querySelector('.stat-label')?.textContent);

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  clearDashboardSourceCache();
  setCustomPermissions(null);
  mockUser = { id: 1, role: 'manager', is_superuser: false };
  mockedApi.getDashboardWithCache.mockResolvedValue({ data: dashboardData as any, fromCache: false, changed: true });
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 2, open_cars: 1, pending_fais: 0, ncr_dispositions: {} });
  mockedApi.getEquipmentDueSoon.mockResolvedValue([{ days_until_due: 5 }]);
  mockedApi.getLowStockAlerts.mockResolvedValue([{ id: 1 }]);
  mockedApi.getCapacityHeatmap.mockResolvedValue({
    start_date: '2026-06-28',
    end_date: '2026-07-04',
    overload_cells: 0,
    overloaded_work_centers: [],
    work_centers: [],
  });
  mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(null));
});

afterEach(() => setCustomPermissions(null));

it('renders only the saved widgets, in saved order and size, and fetches only what they need', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' }, { id: 'work_center_status', size: 'full' }))
  );
  renderDashboard();
  await loaded();

  expect(stripLabels()).toEqual(['Due Today', 'Overdue']);
  const panel = screen.getByRole('heading', { name: 'Work Center Status' });
  expect(panel.closest('.xl\\:col-span-12')).not.toBeNull();
  for (const hidden of ['Capacity Overview', 'Live Shop Activity', 'Signed In Right Now', 'Recent Completions']) {
    expect(screen.queryByRole('heading', { name: hidden })).not.toBeInTheDocument();
  }
  // The attention banner is not in this layout, so the overdue exception renders nowhere.
  expect(screen.queryByRole('region', { name: 'First exception to review' })).not.toBeInTheDocument();
  await act(async () => undefined);
  expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();
  expect(mockedApi.getEquipmentDueSoon).not.toHaveBeenCalled();
  expect(mockedApi.getLowStockAlerts).not.toHaveBeenCalled();
  expect(mockedApi.getCapacityHeatmap).not.toHaveBeenCalled();
  expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(1);
});

it('renders the classic layout for the default (byte-identical panel widths)', async () => {
  renderDashboard();
  await loaded();
  const cellOf = (title: string) =>
    screen
      .getByRole('heading', { name: title })
      .closest('.min-w-0.xl\\:col-span-7, .min-w-0.xl\\:col-span-5, .min-w-0.xl\\:col-span-12');
  expect(cellOf('Capacity Overview')).toHaveClass('xl:col-span-7');
  expect(cellOf('Live Shop Activity')).toHaveClass('xl:col-span-5');
  expect(cellOf('Work Center Status')).toHaveClass('xl:col-span-7');
  expect(cellOf('Signed In Right Now')).toHaveClass('xl:col-span-5');
  expect(cellOf('Recent Completions')).toHaveClass('lg:col-span-2', 'xl:col-span-12');
  await waitFor(() => expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1));
});

it('fetches a source shared by two widgets once', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(layoutOf({ id: 'kpi.open_ncrs' }, { id: 'quality_pulse' }))
  );
  renderDashboard();
  await loaded();
  expect(await screen.findByRole('heading', { name: 'Quality Pulse' })).toBeInTheDocument();
  await waitFor(() => expect(within(kpiStrip()!).getByText('2')).toBeInTheDocument());
  expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1);
});

describe('opt-in panels render through the extra catalog', () => {
  const wallboard: WallboardResponse = {
    work_centers: [],
    late_wos: [
      { wo_number: 'WO-2001', part_number: 'SHAFT-9', due_date: '2026-11-02', days_late: 9, status: 'in_progress' },
    ],
    blocked_wos: [{ wo_number: 'WO-3001', category: 'material_missing', age_hours: 30.5 }],
    late_total: 14,
    blocked_total: 5,
    down_total: 0,
    ship: {
      due_today: 0,
      shipped_today: 0,
      due_this_week: 0,
      due_today_rows: [],
      next_due_date: null,
      next_due_count: 0,
    },
    today: null,
    quality: { open_ncr_count: 0, newest_ncr_age_days: null, wos_on_hold: 0 },
    jobs: [],
    jobs_total: 0,
    generated_at: '2026-11-11T15:00:00Z',
  };

  it('sizes each panel from the saved layout and shares one wallboard request between them', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'late_jobs', size: 'full' }, { id: 'blocked_jobs' }))
    );
    mockedApi.getWallboard.mockResolvedValue(wallboard);
    renderDashboard();
    await loaded();
    const late = await screen.findByRole('heading', { name: 'Late Jobs' });
    expect(late.closest('.min-w-0.xl\\:col-span-12')).not.toBeNull();
    // No stored size: the widget's own default ('third').
    expect(screen.getByRole('heading', { name: 'Blocked Jobs' }).closest('.min-w-0.xl\\:col-span-4')).not.toBeNull();
    await waitFor(() => expect(screen.getByText('5 blocked')).toBeInTheDocument());
    expect(mockedApi.getWallboard).toHaveBeenCalledTimes(1);
    // Nothing on this layout reads the core sources.
    expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();
    expect(mockedApi.getCapacityHeatmap).not.toHaveBeenCalled();
    // Panels sit in the grid, in saved order, after no KPI strip.
    expect(kpiStrip()).toBeNull();
  });

  it('shows a 403 inside the panel, never in the page error banner', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf({ id: 'late_jobs' })));
    mockedApi.getWallboard.mockRejectedValue(httpError(403, 'Forbidden'));
    renderDashboard();
    await loaded();
    expect(await screen.findByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByText(/Shop wallboard:/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry unavailable sections' })).not.toBeInTheDocument();
  });

  it('names a failed opt-in source in the page error banner by its label', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf({ id: 'late_jobs' })));
    mockedApi.getWallboard.mockRejectedValue(httpError(500));
    renderDashboard();
    await loaded();
    expect(
      await screen.findByText('Shop wallboard: Could not refresh. No verified data is available.')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry unavailable sections' })).toBeInTheDocument();
  });
});

it('degrades cross-links whose target widget is hidden to plain text', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(
      layoutOf(
        { id: 'kpi.idle_operators' },
        { id: 'kpi.completed_today' },
        { id: 'work_center_status' },
        { id: 'presence' }
      )
    )
  );
  renderDashboard();
  await loaded();
  // Presence is visible: the idle tile still jumps there.
  expect(screen.getByRole('button', { name: /Signed In, Idle/ })).toBeInTheDocument();
  // Recent Completions and Live Shop Activity are hidden: no dead jumps.
  expect(screen.queryByRole('button', { name: /Operations Completed Today/ })).not.toBeInTheDocument();
  expect(screen.getByText('Operations Completed Today')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /1 ppl/ })).not.toBeInTheDocument();
  expect(screen.getByText('1 ppl')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Alex Reyes/ })).not.toBeInTheDocument();
  expect(screen.getAllByText('Alex Reyes').length).toBeGreaterThan(0);
});

it('drops the idle-tile jump when the presence panel is hidden', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(layoutOf({ id: 'kpi.idle_operators' }, { id: 'live_activity' }))
  );
  renderDashboard();
  await loaded();
  expect(screen.queryByRole('button', { name: /Signed In, Idle/ })).not.toBeInTheDocument();
  expect(screen.getByText('Signed In, Idle')).toBeInTheDocument();
});

describe('links the router would refuse are not rendered as links', () => {
  it('hides Work Center "View All" from a manager (its route needs admin:settings)', async () => {
    renderDashboard();
    await loaded();
    const hrefs = screen.getAllByRole('link', { name: 'View All' }).map(link => link.getAttribute('href'));
    expect(hrefs).toEqual(['/work-orders']);
  });

  it('shows Work Center "View All" to an admin', async () => {
    mockUser = { id: 2, role: 'admin', is_superuser: false };
    renderDashboard();
    await loaded();
    const hrefs = screen.getAllByRole('link', { name: 'View All' }).map(link => link.getAttribute('href'));
    expect(hrefs).toEqual(expect.arrayContaining(['/work-centers', '/work-orders']));
  });

  it('de-links quality targets for the shipping role, including the attention CTA', async () => {
    mockUser = { id: 3, role: 'shipping', is_superuser: false };
    mockedApi.getDashboardWithCache.mockResolvedValue({
      data: { ...dashboardData, summary: { ...dashboardData.summary, overdue: 0 } } as any,
      fromCache: false,
      changed: true,
    });
    renderDashboard();
    await loaded();
    const attention = await screen.findByRole('region', { name: 'First exception to review' });
    expect(within(attention).getByText('2 open NCR(s) require attention')).toBeInTheDocument();
    expect(within(attention).queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('Open NCRs').closest('a')).toBeNull();
    expect(screen.getByText('Calibration Due').closest('a')).toBeNull();
    // A target the role CAN open stays a link.
    expect(screen.getByText('Low Stock Items').closest('a')).toHaveAttribute(
      'href',
      '/warehouse?tab=inventory&filter=low_stock'
    );
  });

  it('keeps every link for a platform admin even under the empty override login stores', async () => {
    setCustomPermissions({ platform_admin: [] });
    mockUser = { id: 4, role: 'platform_admin', is_superuser: false };
    renderDashboard();
    await loaded();
    expect(screen.getAllByRole('link', { name: 'View All' }).map(link => link.getAttribute('href'))).toContain(
      '/work-centers'
    );
    expect(screen.getByText('Calibration Due').closest('a')).toHaveAttribute('href', '/calibration?filter=due_soon');
    expect(screen.getByRole('link', { name: 'Shop Floor' })).toHaveAttribute('href', '/shop-floor');
  });
});

it('offers Customize from an empty dashboard', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf()));
  renderDashboard();
  await loaded();
  expect(screen.getByText('Your dashboard is empty')).toBeInTheDocument();
  const buttons = screen.getAllByRole('button', { name: 'Customize dashboard' });
  expect(buttons).toHaveLength(2);
  fireEvent.click(buttons[1]);
  expect(await screen.findByRole('dialog', { name: 'Customize dashboard' })).toBeInTheDocument();
  expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();
  expect(mockedApi.getCapacityHeatmap).not.toHaveBeenCalled();
});

it('moves the tour anchor to the panel grid when no KPI is shown', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf({ id: 'capacity_overview' })));
  renderDashboard();
  await loaded();
  const anchor = document.querySelector('[data-tour="dashboard-stats"]');
  expect(anchor).not.toBeNull();
  expect(within(anchor as HTMLElement).getByRole('heading', { name: 'Capacity Overview' })).toBeInTheDocument();
});

it('shows the default with a status notice when the saved layout cannot load, and reloads it', async () => {
  mockedApi.getDashboardLayout
    .mockRejectedValueOnce(httpError(500))
    .mockResolvedValue(layoutResponse(layoutOf({ id: 'kpi.overdue' })));
  renderDashboard();
  await loaded();
  expect(screen.getByRole('heading', { name: 'Capacity Overview' })).toBeInTheDocument();
  const notice = screen.getByText(/Your saved dashboard layout couldn.t be loaded; showing the default\./);
  expect(notice.closest('[role="status"]')).not.toBeNull();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Reload layout' }));
  });
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Capacity Overview' })).not.toBeInTheDocument());
  expect(screen.queryByText(/couldn.t be loaded/)).not.toBeInTheDocument();
  expect(stripLabels()).toEqual(['Overdue']);
});

it('re-reads a failed layout before opening Customize', async () => {
  mockedApi.getDashboardLayout.mockRejectedValueOnce(httpError(500)).mockResolvedValue(layoutResponse(null));
  renderDashboard();
  await loaded();
  fireEvent.click(screen.getByRole('button', { name: 'Customize dashboard' }));
  expect(await screen.findByRole('dialog', { name: 'Customize dashboard' })).toBeInTheDocument();
  expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);
});

it('hides Customize, without a notice, when the API predates the feature (404)', async () => {
  mockedApi.getDashboardLayout.mockRejectedValue(httpError(404, 'Not Found'));
  renderDashboard();
  await loaded();
  expect(screen.getByRole('heading', { name: 'Capacity Overview' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Customize dashboard' })).not.toBeInTheDocument();
  expect(screen.queryByText(/couldn.t be loaded/)).not.toBeInTheDocument();
});

it('saves a customized layout and renders it (newly needed sources fetched at once)', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }))
  );
  mockedApi.saveDashboardLayout.mockImplementation(async body => layoutResponse(body.layout, { version: 3 }));
  renderDashboard();
  await loaded();
  expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole('button', { name: 'Customize dashboard' }));
  const dialog = await screen.findByRole('dialog', { name: 'Customize dashboard' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Remove Overdue' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add Open NCRs' }));
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save layout' }));
  });

  expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith({
    expected_company_id: 1,
    expected_version: 2,
    layout: { format_version: 1, widgets: [{ id: 'kpi.due_today' }, { id: 'kpi.open_ncrs' }] },
  });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Customize dashboard' })).not.toBeInTheDocument());
  expect(stripLabels()).toEqual(['Due Today', 'Open NCRs']);
  await waitFor(() => expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(within(kpiStrip()!).getByText('2')).toBeInTheDocument());
});

it('skips the background poll while the tab is hidden and catches up once visible', async () => {
  jest.useFakeTimers();
  let visibility: DocumentVisibilityState = 'visible';
  const visibilitySpy = jest.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  try {
    renderDashboard();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(1);

    visibility = 'hidden';
    await act(async () => {
      await jest.advanceTimersByTimeAsync(30_000);
    });
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(1);

    visibility = 'visible';
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(2);
  } finally {
    visibilitySpy.mockRestore();
    jest.useRealTimers();
  }
});

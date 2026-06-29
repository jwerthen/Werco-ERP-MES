/**
 * Dashboard cockpit — de-duplication + cross-link regression.
 *
 * The dashboard was redesigned into a compact "command cockpit" where the four
 * live panels (Capacity, Live Shop Activity, Work Center Status, Signed In) are
 * co-visible and the operator overlap is de-duplicated: an operator's live job
 * renders ONCE in Live Shop Activity, while Work Center Status keeps only a
 * People count and Presence shows on-the-job users as chips. This guards that
 * de-dup split and the cross-link anchors that tie the panels together.
 *
 * Both the desktop and mobile breakpoints mount in jsdom (CSS classes don't
 * prune the DOM), so assertions prefer role/anchor queries that stay stable.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Dashboard from './Dashboard';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardWithCache: jest.fn(),
    getQualitySummary: jest.fn(),
    getEquipmentDueSoon: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getCapacityHeatmap: jest.fn(),
  },
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

// One operator (id 11) clocked into a job, plus one idle signed-in user (id 21).
// active_people on the work center carries the SAME operator (the redundant copy
// the redesign de-duplicates away).
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
      active_people: [
        {
          user_id: 11,
          name: 'Alex Reyes',
          employee_id: 'E011',
          work_order_number: 'WO-3041',
          operation_name: 'Laser cut',
          clock_in: '2026-06-28T18:00:00Z',
        },
      ],
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
        customer_name: 'Acme Aero',
        priority: 2,
        due_date: '2026-07-03',
        quantity_ordered: 100,
        quantity_complete: 70,
      },
      operation: { id: 1, operation_number: '10', name: 'Laser cut', status: 'in_progress', sequence: 1, quantity_complete: 70, quantity_scrapped: 0 },
      work_center: { id: 1, code: 'LASER-1', name: 'Laser cell 1', status: 'in_use', type: 'laser' },
    },
  ],
  recent_completions: [
    { work_order_number: 'WO-2998', operation_name: 'Deburr', work_center_name: 'Finish', operator_name: 'Alex Reyes', completed_at: '2026-06-28T17:00:00Z', quantity_complete: 10 },
  ],
};

const heatmap = {
  start_date: '2026-06-28',
  end_date: '2026-07-04',
  overload_cells: 0,
  overloaded_work_centers: [],
  work_centers: [
    {
      work_center_id: 1,
      work_center_code: 'LASER-1',
      work_center_name: 'Laser cell 1',
      capacity_hours_per_day: 8,
      days: ['2026-06-28', '2026-06-29', '2026-06-30', '2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04'].map((date, i) => ({
        date,
        scheduled_hours: i,
        capacity_hours: 8,
        utilization_pct: i * 12,
        job_count: i,
        overloaded: false,
      })),
    },
  ],
};

const renderDashboard = () => render(<MemoryRouter><Dashboard /></MemoryRouter>);

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView; the cross-links call it.
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getDashboardWithCache.mockResolvedValue({ data: dashboardData as any, fromCache: false, changed: true });
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 2 } as any);
  mockedApi.getEquipmentDueSoon.mockResolvedValue([{ days_until_due: -1 }, { days_until_due: 5 }] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue([{ id: 1, is_critical: true }, { id: 2 }] as any);
  mockedApi.getCapacityHeatmap.mockResolvedValue(heatmap as any);
});

test('renders the four cockpit panels after load', async () => {
  renderDashboard();
  expect(await screen.findByText('Capacity Overview')).toBeInTheDocument();
  expect(screen.getByText('Live Shop Activity')).toBeInTheDocument();
  expect(screen.getByText('Work Center Status')).toBeInTheDocument();
  expect(screen.getByText('Signed In Right Now')).toBeInTheDocument();
});

test('de-dup: on-the-job operator renders as a presence chip, idle user as a row', async () => {
  renderDashboard();
  await screen.findByText('Capacity Overview');

  // The clocked-in operator is shown as a chip BUTTON (jumps to their job)…
  expect(screen.getByRole('button', { name: /Alex Reyes/i })).toBeInTheDocument();
  // …and the idle user is a plain row, NOT a chip button.
  expect(screen.queryByRole('button', { name: /Lena Chen/i })).toBeNull();
  // (Lena appears both as an idle row and in the "not on a job" summary line.)
  expect(screen.getAllByText('Lena Chen').length).toBeGreaterThan(0);
});

test('de-dup: the live job (WO link) is rendered once, in Live Shop Activity', async () => {
  renderDashboard();
  await screen.findByText('Capacity Overview');

  // The canonical job render is the work-order Link in the activity row.
  const woLinks = screen.getAllByRole('link', { name: 'WO-3041' });
  expect(woLinks).toHaveLength(1);
  // The live-activity row carries the cross-link anchor for the operator.
  expect(document.getElementById('assign-101')).not.toBeNull();
});

test('cross-link: Work Center People count keeps the number and jumps to the live group', async () => {
  renderDashboard();
  await screen.findByText('Capacity Overview');

  // The Work Center Status group anchor exists, keyed on the stable work_center id.
  expect(document.getElementById('wc-live-1')).not.toBeNull();

  const getById = jest.spyOn(document, 'getElementById');
  // People count is preserved as a clickable count (the per-person detail moved to Live Activity).
  const pplButton = screen.getByRole('button', { name: /1 ppl/i });
  fireEvent.click(pplButton);
  expect(getById).toHaveBeenCalledWith('wc-live-1');
  getById.mockRestore();
});

test('cross-link: clicking an on-the-job presence chip jumps to its assignment row', async () => {
  renderDashboard();
  await screen.findByText('Capacity Overview');

  const getById = jest.spyOn(document, 'getElementById');
  fireEvent.click(screen.getByRole('button', { name: /Alex Reyes/i }));
  expect(getById).toHaveBeenCalledWith('assign-101');
  getById.mockRestore();
});

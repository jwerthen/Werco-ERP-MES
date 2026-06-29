/**
 * ShopFloor cockpit render-correctness test.
 *
 * Locks the behavior of the instrument-panel shop-floor overhaul: after the
 * initial data load the page must render the work-center selector, the Job
 * Queue header with its live job count, and one operations-grid row per queued
 * operation (with work-order number, part, operation, and a Start control).
 *
 * On mount the page issues three API reads — getWorkCenters() and
 * getMyActiveJob() in parallel, then getWorkCenterQueue(id) once a work center
 * is selected. All three are mocked; a missing mock would make the page throw.
 */

import React from 'react';
import { render, screen, within, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkOrder: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    createWorkOrderBlocker: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    // admin + superuser so priority/edit controls render
    user: { id: 1, role: 'admin', is_superuser: true },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenter = {
  id: 7,
  version: 1,
  code: 'CNC-1',
  name: 'CNC Mill 1',
  work_center_type: 'milling',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const queueItems = [
  {
    operation_id: 101,
    work_order_id: 1001,
    work_order_number: 'WO-5001',
    part_number: 'PN-AAA',
    part_name: 'Bracket Assembly',
    operation_number: '10',
    operation_name: 'Rough Mill',
    status: 'ready',
    quantity_ordered: 50,
    quantity_complete: 10,
    priority: 2,
    due_date: '2026-07-15',
    setup_time_hours: 1,
    run_time_hours: 4,
  },
  {
    operation_id: 102,
    work_order_id: 1002,
    work_order_number: 'WO-5002',
    part_number: 'PN-BBB',
    part_name: 'Housing',
    operation_number: '20',
    operation_name: 'Finish Mill',
    status: 'in_progress',
    quantity_ordered: 20,
    quantity_complete: 5,
    priority: 4,
    due_date: '2026-07-20',
    setup_time_hours: 0.5,
    run_time_hours: 2,
  },
];

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

/** Wait for the loaded data load and return the Job Queue <table>. */
async function getQueueTable(): Promise<HTMLElement> {
  const woCells = await screen.findAllByText('WO-5001');
  const table = woCells.map((el) => el.closest('table')).find(Boolean);
  if (!table) throw new Error('expected the Job Queue <table> to render');
  return table as HTMLElement;
}

describe('ShopFloor cockpit: station header + operations grid render', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: queueItems });
  });

  it('renders the work-center selector and the Job Queue header with a job count', async () => {
    renderShopFloor();

    // Page heading from the cockpit header.
    expect(await screen.findByRole('heading', { name: /Shop Floor/i })).toBeInTheDocument();

    // Work-center selector button for the loaded station.
    expect(await screen.findByRole('button', { name: 'CNC Mill 1' })).toBeInTheDocument();

    // Job Queue section header.
    const queueHeading = screen.getByRole('heading', { name: /Job Queue/i });

    // Station summary line (sibling of the heading) shows the station name and
    // the live job count for the loaded queue (2 jobs).
    const summary = queueHeading.parentElement as HTMLElement;
    await waitFor(() => {
      expect(within(summary).getByText('2')).toBeInTheDocument();
    });
    expect(within(summary).getByText(/CNC Mill 1/)).toBeInTheDocument();
    // "job" appears in the heading ("Job Queue") and the count line ("2 jobs").
    expect(within(summary).getAllByText(/job/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders one operations-grid row per queued operation, with WO, part, operation and a Start control', async () => {
    renderShopFloor();
    const table = await getQueueTable();

    // Both work orders render in the grid.
    expect(within(table).getByText('WO-5001')).toBeInTheDocument();
    expect(within(table).getByText('WO-5002')).toBeInTheDocument();

    // Part + operation detail render.
    expect(within(table).getByText('PN-AAA')).toBeInTheDocument();
    expect(within(table).getByText('Rough Mill')).toBeInTheDocument();
    expect(within(table).getByText('PN-BBB')).toBeInTheDocument();
    expect(within(table).getByText('Finish Mill')).toBeInTheDocument();

    // One <tbody> row per queued operation (rows that contain a WO number).
    const dataRows = within(table)
      .getAllByRole('row')
      .filter((row) => within(row).queryByText(/^WO-50\d\d$/));
    expect(dataRows).toHaveLength(2);

    // The "ready" op exposes a Start control; the in-progress op shows Active.
    expect(within(table).getByRole('button', { name: /Start/i })).toBeInTheDocument();
    expect(within(table).getByText(/Active/i)).toBeInTheDocument();
  });
});

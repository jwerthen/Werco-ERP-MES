/**
 * ShopFloor back-entry (offline catch-up) toggle test.
 *
 * Locks the backfill-source-tagging UI contract:
 *  - The "Back-entry (offline catch-up)" toggle is a supervisor+ capability
 *    (gated on `work_orders:edit`): it renders for a supervisor/manager but NOT
 *    for an operator, so operators cannot tag their own live work as backfill to
 *    dodge the live-capture metrics.
 *  - With the toggle ON, driving a clock-in from the queue calls api.clockIn with
 *    `source: 'backfill'`; with the toggle OFF, the payload carries no `source`
 *    key at all (server stores NULL — never guessed).
 *
 * Mirrors ShopFloor.cockpit.test.tsx: the same api / AuthContext / useWebSocket /
 * realtime mocks, and it drives clock-in through the real rendered Start control.
 */

import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';

// Mutable so each test can swap the current user's role before rendering. The
// `mock` prefix is required for a jest.mock factory to reference it (out-of-scope
// rule exemption).
let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'supervisor',
  is_superuser: false,
};

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
    user: mockUser,
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

// One READY operation -> the grid exposes a single Start control we can click.
const readyQueueItem = {
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
};

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

/** Wait for the queue to finish loading, then return the Start control. */
async function findStartButton(): Promise<HTMLElement> {
  return screen.findByRole('button', { name: /Start/i });
}

const backEntryQuery = { name: /back-entry/i } as const;

describe('ShopFloor back-entry toggle: RBAC visibility', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [readyQueueItem] });
  });

  it('renders the back-entry toggle for a supervisor (work_orders:edit)', async () => {
    mockUser = { id: 1, role: 'supervisor', is_superuser: false };
    renderShopFloor();

    // The toggle is a checkbox with an explicit back-entry aria-label.
    expect(await screen.findByRole('checkbox', backEntryQuery)).toBeInTheDocument();
  });

  it('renders the back-entry toggle for a manager', async () => {
    mockUser = { id: 2, role: 'manager', is_superuser: false };
    renderShopFloor();

    expect(await screen.findByRole('checkbox', backEntryQuery)).toBeInTheDocument();
  });

  it('does NOT render the back-entry toggle for an operator', async () => {
    mockUser = { id: 3, role: 'operator', is_superuser: false };
    renderShopFloor();

    // Wait for the page to fully load (queue rendered) before asserting absence,
    // so this isn't a false pass on an unmounted toggle.
    await findStartButton();
    expect(screen.queryByRole('checkbox', backEntryQuery)).not.toBeInTheDocument();
  });
});

describe('ShopFloor back-entry toggle: clock-in source payload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'supervisor', is_superuser: false };
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [readyQueueItem] });
    mockedApi.clockIn.mockResolvedValue(undefined as any);
  });

  it("sends source: 'backfill' on clock-in when back-entry is ON", async () => {
    renderShopFloor();

    const toggle = await screen.findByRole('checkbox', backEntryQuery);
    fireEvent.click(toggle);
    expect((toggle as HTMLInputElement).checked).toBe(true);

    fireEvent.click(await findStartButton());

    await waitFor(() => expect(mockedApi.clockIn).toHaveBeenCalledTimes(1));
    expect(mockedApi.clockIn).toHaveBeenCalledWith(
      expect.objectContaining({
        work_order_id: readyQueueItem.work_order_id,
        operation_id: readyQueueItem.operation_id,
        work_center_id: workCenter.id,
        entry_type: 'run',
        source: 'backfill',
      })
    );
  });

  it('omits the source key on clock-in when back-entry is OFF', async () => {
    renderShopFloor();

    // Do not toggle: back-entry stays OFF.
    fireEvent.click(await findStartButton());

    await waitFor(() => expect(mockedApi.clockIn).toHaveBeenCalledTimes(1));
    const payload = mockedApi.clockIn.mock.calls[0][0];
    expect(payload).not.toHaveProperty('source');
    // Sanity: the rest of the clock-in payload is still intact.
    expect(payload).toEqual(
      expect.objectContaining({
        work_order_id: readyQueueItem.work_order_id,
        operation_id: readyQueueItem.operation_id,
        entry_type: 'run',
      })
    );
  });
});

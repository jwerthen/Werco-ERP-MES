/**
 * ShopFloor (Time Clock) — Start on a job the WORK ORDER refuses.
 *
 * Owner ruling 2026-11-05: the floor never starts work a supervisor has not
 * released or has put on hold. The queue row carries the server's
 * `start_blocked_reason`, and the Start control is disabled with that text —
 * INCLUDING in back-entry mode, because the server refuses a `source=backfill`
 * clock-in exactly like a live one (no exemption: the server accepts that
 * channel from any caller). A supervisor resumes the job, files the paper labor,
 * then re-holds it.
 *
 * Harness mirrors ShopFloor.backEntry.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';

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
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Verbatim from backend app/services/operation_action_gates.py.
const WO_ON_HOLD =
  "This work order is on hold — work cannot start until a supervisor resumes it (Resume Work Order on the work order). Resuming or clearing an operation's hold does not lift it.";

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

const readyItem = {
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
  work_order_status: 'released',
  start_blocked_reason: null,
};

const heldWoItem = {
  ...readyItem,
  work_order_status: 'on_hold',
  start_blocked_reason: WO_ON_HOLD,
};

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.clockIn.mockResolvedValue(undefined as never);
});

describe('ShopFloor Time Clock — work-order start refusal', () => {
  it('disables Start on a held job, labels it, and shows the server reason', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [heldWoItem] });
    renderShopFloor();

    const start = await screen.findByRole('button', { name: 'WO on hold' });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute('title', WO_ON_HOLD);
    expect(screen.getByText(WO_ON_HOLD)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Start$/ })).not.toBeInTheDocument();

    fireEvent.click(start);
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('keeps Start disabled with back-entry ON (the server refuses backfill alike)', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [heldWoItem] });
    renderShopFloor();

    const toggle = await screen.findByRole('checkbox', { name: /back-entry/i });
    fireEvent.click(toggle);
    const confirmation = await screen.findByRole('dialog', { name: 'Enter offline catch-up mode?' });
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Enable back-entry' }));
    expect(toggle).toBeChecked();

    const start = screen.getByRole('button', { name: 'WO on hold' });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('leaves a released job with an enabled Start (control)', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [readyItem] });
    renderShopFloor();

    const start = await screen.findByRole('button', { name: /^Start$/ });
    expect(start).toBeEnabled();
    expect(screen.queryByText(WO_ON_HOLD)).not.toBeInTheDocument();
  });
});

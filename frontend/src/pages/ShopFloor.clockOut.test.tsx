/**
 * ShopFloor (Time Clock) — Clock Out starts at 0 good.
 *
 * Owner ruling 1 capped a clock-out at the operation's good ceiling (the target, less
 * anything an upstream close declared short) and refuses 400 above it. The Time Clock
 * used to pre-fill `quantity_ordered − quantity_complete`, so:
 *  - after an upstream short, the DEFAULT submit was a guaranteed 400;
 *  - on every other job, the default submit was a full good count — which completes the
 *    operation at clock-out — whether or not the operator made those pieces.
 * It now starts at 0, as KEEP OPEN on the kiosk and Check Out on the desk do: the
 * operator enters what they made this session.
 *
 * Harness mirrors ShopFloor.startBlocked.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  useAuth: () => ({ user: { id: 1, role: 'operator', is_superuser: false }, isAuthenticated: true, isLoading: false }),
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
  code: 'DEB-1',
  name: 'Deburr 1',
  work_center_type: 'assembly',
  hourly_rate: 60,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

// Sequenced job, 10 target: Op 10 closed 2 short, so at most 8 good can land on Op 20.
const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 101,
  work_center_id: 7,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 10,
  quantity_complete: 6,
  upstream_quantity_short: 2,
  quantity_good_ceiling: 8,
  quantity_unaccounted: 2,
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
  mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' } as never);
});

describe('ShopFloor Time Clock — Clock Out', () => {
  it('starts Quantity Produced at 0 — never a figure above the good ceiling, never phantom good', async () => {
    renderShopFloor();
    fireEvent.click(await screen.findByRole('button', { name: /Clock Out/ }));

    const dialog = await screen.findByRole('dialog');
    // The old prefill was 10 − 6 = 4: above the 8 − 6 = 2 the server accepts here.
    expect(within(dialog).getByLabelText('Quantity Produced')).toHaveValue(0);
  });

  it('a default submit clocks out with 0 good, leaving the operation for the operator to count', async () => {
    renderShopFloor();
    fireEvent.click(await screen.findByRole('button', { name: /Clock Out/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /Complete Clock Out/ }));

    await waitFor(() =>
      expect(mockedApi.clockOut).toHaveBeenCalledWith(501, expect.objectContaining({ quantity_produced: 0 }))
    );
  });

  it('what the operator keys is what is sent', async () => {
    renderShopFloor();
    fireEvent.click(await screen.findByRole('button', { name: /Clock Out/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Quantity Produced'), { target: { value: '2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /Complete Clock Out/ }));

    await waitFor(() =>
      expect(mockedApi.clockOut).toHaveBeenCalledWith(501, expect.objectContaining({ quantity_produced: 2 }))
    );
  });
});

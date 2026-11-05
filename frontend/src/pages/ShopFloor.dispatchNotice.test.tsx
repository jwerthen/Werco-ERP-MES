/**
 * ShopFloor (/shop-floor "Time Clock") — the floor dispatch notice
 * (Dispatch Board v2, SPEC §7). The notice rides the page's 10-second
 * my-active-job poll: one strip per machine the user is clocked in at,
 * acknowledged per (user, work center).
 */

import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';
import type { DispatchNotice, MyActiveJobResponse } from '../types';

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
    user: { id: 1, role: 'operator', is_superuser: false },
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

const SERVER_TIME = '2026-11-04T16:41:07.957738Z';

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

function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 7,
    work_center_code: 'CNC-1',
    change_kind: 'machine_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 102,
      work_order_number: 'WO-9002',
      unit_number: null,
      operation_number: '10',
      operation_name: 'Mill',
      part_number: 'PN-XXX',
    },
    next_up_after: {
      operation_id: 101,
      work_order_number: 'WO-9001',
      unit_number: null,
      operation_number: '10',
      operation_name: 'Mill',
      part_number: 'PN-XXX',
    },
    moved_up: [101],
    updates_in_window: 1,
    ...overrides,
  };
}

function activePayload(notices?: DispatchNotice[]): MyActiveJobResponse {
  const payload: MyActiveJobResponse = { active_jobs: [], active_job: null, server_time: SERVER_TIME };
  if (notices) payload.dispatch_notices = notices;
  return payload;
}

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

describe('ShopFloor dispatch notice', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  });

  it('shows the notice from my-active-job, names the machine, and "Got it" hides it', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([makeNotice()]));
    renderShopFloor();

    const strip = await screen.findByTestId('dispatch-notice');
    expect(strip).toHaveTextContent('CNC-1');
    expect(strip).toHaveTextContent('RUN NEXT: WO-9001 · Op 10 Mill — moved up by Jon W. 10:34 AM');

    fireEvent.click(screen.getByRole('button', { name: /got it/i }));
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /shop floor/i })).toBeInTheDocument();
  });

  it('picks the notice up on the 10s my-active-job poll', async () => {
    jest.useFakeTimers();
    try {
      mockedApi.getMyActiveJob.mockResolvedValueOnce(activePayload([]));
      mockedApi.getMyActiveJob.mockResolvedValue(activePayload([makeNotice()]));
      renderShopFloor();
      await act(async () => {});
      expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(10_000);
      });
      expect(screen.getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-9001');
    } finally {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    }
  });

  it('renders exactly as before against an older backend that sends no notice fields', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    renderShopFloor();
    await screen.findByRole('heading', { name: /shop floor/i });
    expect(screen.queryByTestId('dispatch-notice-banner')).not.toBeInTheDocument();
  });
});

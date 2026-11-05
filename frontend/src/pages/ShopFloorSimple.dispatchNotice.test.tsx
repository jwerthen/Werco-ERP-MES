/**
 * ShopFloorSimple (/shop-floor/operations — where every operator lands) — the
 * floor dispatch notice (Dispatch Board v2, SPEC §7).
 *
 * Two feeds: my-active-job carries the newest notice for every machine the
 * operator is clocked in at (so it reaches them even on "All work centers"),
 * and the operations list carries the one machine it is filtered to. The same
 * notice on both is one strip. Acknowledged per (user, work center).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import type { DispatchNotice, MyActiveJobResponse, WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    resolveScanAction: jest.fn(),
    scannerLookup: jest.fn(),
    getOperationDetails: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const SERVER_TIME = '2026-11-04T16:41:07.957738Z';

const LASER_1: WorkCenter = {
  id: 1,
  version: 0,
  name: 'Laser 1',
  code: 'LASER1',
  work_center_type: 'laser_cutting',
  hourly_rate: 95,
  capacity_hours_per_day: 16,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-07-01T12:00:00Z',
  updated_at: '2026-07-01T12:00:00Z',
};

function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 1,
    work_center_code: 'LASER1',
    change_kind: 'operation_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 202,
      work_order_number: 'WO-8002',
      unit_number: null,
      operation_number: 'Nest 2',
      operation_name: 'Laser Cut - CNC0412',
      part_number: null,
    },
    next_up_after: {
      operation_id: 201,
      work_order_number: 'WO-8001',
      unit_number: null,
      operation_number: 'Nest 1',
      operation_name: 'Laser Cut - CNC0411',
      part_number: null,
    },
    moved_up: [201],
    updates_in_window: 1,
    ...overrides,
  };
}

const OPERATION = {
  id: 201,
  work_order_id: 41,
  work_order_number: 'WO-8001',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: 'Nest 1',
  operation_name: 'Laser Cut - CNC0411',
  description: null,
  work_center_id: 1,
  work_center_name: 'Laser 1',
  status: 'ready',
  quantity_ordered: 25,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
  run_order: 1,
};

function activePayload(notices?: DispatchNotice[]): MyActiveJobResponse {
  const payload: MyActiveJobResponse = { active_jobs: [], active_job: null, server_time: SERVER_TIME };
  if (notices) payload.dispatch_notices = notices;
  return payload;
}

function renderShopFloor(url = '/shop-floor/operations') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ShopFloorSimple dispatch notice', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([LASER_1]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([]));
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [OPERATION] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  });

  it('shows the notice for a machine the operator is clocked in at (from my-active-job), naming the machine', async () => {
    const weld = makeNotice({
      id: 90240,
      work_center_id: 14,
      work_center_code: 'WELD-2',
      next_up_after: {
        operation_id: 3101,
        work_order_number: 'WO-1234',
        unit_number: '7',
        operation_number: '20',
        operation_name: 'Bend',
        part_number: null,
      },
    });
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([weld]));
    renderShopFloor();

    const strip = await screen.findByTestId('dispatch-notice');
    expect(strip).toHaveTextContent('WELD-2');
    expect(strip).toHaveTextContent('RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved up by Jon W. 10:34 AM');
    expect(screen.getByRole('alert')).toBe(strip.querySelector('[role="alert"]'));
  });

  it('shows the filtered machine’s notice from /shop-floor/operations, once even when my-active-job repeats it', async () => {
    const notice = makeNotice();
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [OPERATION],
      dispatch_notices: [notice],
      server_time: SERVER_TIME,
    });
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([notice]));
    renderShopFloor('/shop-floor/operations?work_center_id=1');

    await waitFor(() =>
      expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith(expect.objectContaining({ work_center_id: 1 }))
    );
    const strips = await screen.findAllByTestId('dispatch-notice');
    expect(strips).toHaveLength(1);
    expect(strips[0]).toHaveTextContent('LASER1');
    expect(strips[0]).toHaveTextContent('RUN NEXT: WO-8001 · Nest 1 Laser Cut - CNC0411');
  });

  it('"Got it" hides it for this user, across a reload, without leaving the page', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([makeNotice()]));
    const first = renderShopFloor();

    fireEvent.click(await screen.findByRole('button', { name: /got it/i }));
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
    expect(screen.getByTestId('shop-floor-op-201')).toBeInTheDocument();
    first.unmount();

    renderShopFloor();
    await screen.findByTestId('shop-floor-op-201');
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
  });

  it('renders exactly as before against an older backend that sends no notice fields', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [OPERATION] });
    renderShopFloor('/shop-floor/operations?work_center_id=1');

    await screen.findByTestId('shop-floor-op-201');
    expect(screen.queryByTestId('dispatch-notice-banner')).not.toBeInTheDocument();
  });
});

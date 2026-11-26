/**
 * WorkOrderDetail — in-process inspection sign-off on the operation rows.
 *
 * An inspection-point operation shows "Inspection required" until the audited
 * `POST /shop-floor/operations/{id}/inspection` is recorded, then "Inspected". The
 * Sign off button is offered to exactly the route's roles (ADMIN / MANAGER /
 * SUPERVISOR / QUALITY, plus the platform principals `require_role` waves through),
 * and is NON-optimistic: the row only flips after the page re-reads the work order.
 *
 * Harness mirrors WorkOrderDetail.completeOperationGate.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    listComments: jest.fn().mockResolvedValue([]),
    getWatchState: jest.fn().mockResolvedValue({ watching: false, watcher_count: 0 }),
    getMaterialAllocations: jest.fn(),
    signOffOperationInspection: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 1, role: 'quality', is_superuser: false };

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

const inspectionOp = (inspectionComplete: boolean) => ({
  id: 71,
  work_order_id: 42,
  sequence: 20,
  operation_number: '20',
  name: 'Deburr',
  status: 'in_progress',
  quantity_complete: 0,
  estimated_hours: 1,
  requires_inspection: true,
  inspection_complete: inspectionComplete,
  // A routing inspection point is stamped "final"; the sign-off must keep it.
  inspection_type: 'final',
});

const plainOp = {
  id: 72,
  work_order_id: 42,
  sequence: 30,
  operation_number: '30',
  name: 'Pack',
  status: 'pending',
  quantity_complete: 0,
  estimated_hours: 1,
  requires_inspection: false,
  inspection_complete: false,
};

const workOrder = (inspectionComplete: boolean) => ({
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations: [inspectionOp(inspectionComplete), plainOp],
});

function renderDetail() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/42']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'quality', is_superuser: false };
  mockedApi.getWorkOrder.mockResolvedValue(workOrder(false));
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
});

describe('WorkOrderDetail inspection sign-off', () => {
  it.each(['admin', 'manager', 'supervisor', 'quality', 'platform_admin'])('offers Sign off to %s', async role => {
    mockUser = { id: 2, role, is_superuser: false };
    renderDetail();
    expect(await screen.findByRole('button', { name: /Sign off inspection for Op 20/ })).toBeInTheDocument();
  });

  it.each(['operator', 'viewer', 'shipping'])('shows only the chip to %s', async role => {
    mockUser = { id: 3, role, is_superuser: false };
    renderDetail();
    const row = await screen.findByText('Deburr');
    expect(within(row.closest('tr') as HTMLElement).getByText('inspection required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sign off inspection/ })).not.toBeInTheDocument();
  });

  it('marks only the inspection-point operation', async () => {
    renderDetail();
    const packRow = (await screen.findByText('Pack')).closest('tr') as HTMLElement;
    expect(within(packRow).queryByText(/inspect/i)).not.toBeInTheDocument();
  });

  it('signs off, then re-reads the work order and shows Inspected', async () => {
    mockedApi.signOffOperationInspection.mockResolvedValue({
      message: 'Operation inspection recorded',
      operation_id: 71,
      work_order_id: 42,
      requires_inspection: true,
      inspection_complete: true,
      inspection_type: 'final',
      signed_off_by_id: 1,
      signed_off_by_name: 'Robin Vale',
      signed_off_at: '2026-11-23T15:04:05Z',
    });
    renderDetail();

    fireEvent.click(await screen.findByRole('button', { name: /Sign off inspection for Op 20/ }));
    mockedApi.getWorkOrder.mockResolvedValue(workOrder(true));
    const readsBefore = mockedApi.getWorkOrder.mock.calls.length;
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Sign off' }));

    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(readsBefore));
    expect(mockedApi.signOffOperationInspection).toHaveBeenCalledWith(71, {});
    const row = (await screen.findByText('Deburr')).closest('tr') as HTMLElement;
    await waitFor(() => expect(within(row).getByText('inspected')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Sign off inspection/ })).not.toBeInTheDocument();
  });

  it('keeps the chip but offers no Sign off on a cancelled nest', async () => {
    mockedApi.getWorkOrder.mockResolvedValue({
      ...workOrder(false),
      operations: [{ ...inspectionOp(false), cancelled_nest_id: 9 }, plainOp],
    });
    renderDetail();
    const row = (await screen.findByText('Deburr')).closest('tr') as HTMLElement;
    expect(within(row).getByText('inspection required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sign off inspection/ })).not.toBeInTheDocument();
  });

  it('keeps the chip but offers no Sign off on a skipped step (it never ran)', async () => {
    mockedApi.getWorkOrder.mockResolvedValue({
      ...workOrder(false),
      operations: [{ ...inspectionOp(false), status: 'skipped', skip_reason: 'Not on this rev' }, plainOp],
    });
    renderDetail();
    const row = (await screen.findByText('Deburr')).closest('tr') as HTMLElement;
    expect(within(row).getByText('inspection required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sign off inspection/ })).not.toBeInTheDocument();
  });
});

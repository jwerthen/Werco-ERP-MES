/**
 * WorkOrderDetail — laser-nest dispatch controls.
 *
 *  1. Inline due-date edit: a pencil on the Due Date tile (work_orders:edit →
 *     admin/manager/supervisor) swaps in a date input; Save calls
 *     api.updateWorkOrder with { due_date, version } (optimistic locking) and
 *     refetches on success. Server-gated ⇒ NON-optimistic.
 *  2. Per-nest work-center visibility + reassign: each nest row shows its
 *     op's work center name; managers get a select of active work centers
 *     that calls api.updateOperation with { work_center_id, version }. A
 *     refusal (op in progress) surfaces the server's verbatim detail as an
 *     error toast and the select stays on the server's value.
 *
 * Harness mirrors WorkOrderDetail.stepsBypassed.test.tsx (side-channels
 * mocked, real ToastProvider so toast text is assertable).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui/Toast';

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
    getWorkCenters: jest.fn(),
    updateWorkOrder: jest.fn(),
    updateOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'admin',
  is_superuser: false,
};

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

const ERMAKSAN = {
  id: 5,
  version: 1,
  code: 'LSR-1',
  name: 'Ermaksan Fiber Laser',
  work_center_type: 'laser_cutting',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};
const TUBE = { ...ERMAKSAN, id: 6, code: 'LSR-2', name: 'HSG Tube Laser' };

const NEST_OP = {
  id: 71,
  version: 4,
  work_order_id: 42,
  work_center_id: 6,
  work_center_name: 'HSG Tube Laser',
  sequence: 10,
  operation_number: 'OP10',
  name: 'Laser: N-100',
  status: 'ready',
  quantity_complete: 0,
  quantity_scrapped: 0,
  estimated_hours: 1,
  laser_nest: {
    id: 501,
    nest_name: 'N-100',
    cnc_number: '8001',
    planned_runs: 3,
    completed_runs: 0,
    remaining_runs: 3,
    material: 'A36',
    thickness: '0.25"',
    sheet_size: '60x120',
    has_document: false,
  },
};

/** Standalone laser WO (no part) with one nest op, due 2026-07-25. */
const workOrderFixture = {
  id: 42,
  version: 2,
  work_order_number: 'WO-0042',
  part_id: null,
  work_order_type: 'laser_cutting',
  quantity_ordered: 3,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'released',
  priority: 3,
  due_date: '2026-07-25',
  estimated_hours: 1,
  actual_hours: 0,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  operations: [NEST_OP],
};

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
  mockUser = { id: 1, role: 'admin', is_superuser: false };

  mockedApi.getWorkOrder.mockResolvedValue({ ...workOrderFixture });
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([ERMAKSAN, TUBE]);
  mockedApi.updateWorkOrder.mockResolvedValue({});
  mockedApi.updateOperation.mockResolvedValue({});
});

describe('inline due-date edit', () => {
  it('saves through updateWorkOrder with the version, toasts, and refetches', async () => {
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit due date' }));

    // Seeded from the WO's current due date.
    const input = screen.getByLabelText('Due date');
    expect(input).toHaveValue('2026-07-25');
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;

    fireEvent.change(input, { target: { value: '2026-08-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save due date' }));

    await waitFor(() =>
      expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { due_date: '2026-08-01', version: 2 })
    );
    expect(await screen.findByText('Due date set to Aug 1, 2026')).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
  });

  it('surfaces the server refusal verbatim and keeps the editor open (non-optimistic)', async () => {
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { data: { detail: 'Work order was modified by another user — reload and retry.' } },
    });
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit due date' }));
    fireEvent.change(screen.getByLabelText('Due date'), { target: { value: '2026-08-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save due date' }));

    expect(
      await screen.findByText('Work order was modified by another user — reload and retry.')
    ).toBeInTheDocument();
    // Still editing — the tile reflects only what the server confirmed.
    expect(screen.getByLabelText('Due date')).toBeInTheDocument();
  });

  it('cancel closes the editor without calling the API', async () => {
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit due date' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel due date edit' }));

    expect(screen.queryByLabelText('Due date')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('hides the pencil from roles without work_orders:edit (operator)', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });

    expect(screen.queryByRole('button', { name: 'Edit due date' })).not.toBeInTheDocument();
  });
});

describe('per-nest work-center reassign', () => {
  it('shows the nest op work center and reassigns via updateOperation with the op version', async () => {
    renderDetail();
    const select = (await screen.findByLabelText('Work center for 8001')) as HTMLSelectElement;
    expect(select).toHaveValue('6'); // current: HSG Tube Laser
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;

    fireEvent.change(select, { target: { value: '5' } });

    await waitFor(() =>
      expect(mockedApi.updateOperation).toHaveBeenCalledWith(71, { work_center_id: 5, version: 4 })
    );
    expect(await screen.findByText('Op 10 moved to Ermaksan Fiber Laser')).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
  });

  it('surfaces an in-progress refusal verbatim and snaps back to the server value', async () => {
    const refusal = 'Operation 10 is in progress — finish or hold it before moving work centers.';
    mockedApi.updateOperation.mockRejectedValue({ response: { data: { detail: refusal } } });
    renderDetail();
    const select = (await screen.findByLabelText('Work center for 8001')) as HTMLSelectElement;

    fireEvent.change(select, { target: { value: '5' } });

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    // NON-optimistic: the select re-renders from the op's (unchanged) state.
    await waitFor(() => expect(select).toHaveValue('6'));
  });

  it('operators see the work center name but no reassign select', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });

    expect(screen.queryByLabelText('Work center for 8001')).not.toBeInTheDocument();
    // The name still renders in the nest meta line.
    expect(screen.getByText('HSG Tube Laser')).toBeInTheDocument();
  });
});

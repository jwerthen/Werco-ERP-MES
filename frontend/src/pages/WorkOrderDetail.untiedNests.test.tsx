/**
 * WorkOrderDetail — the untied-nest marker on the Material Ties panel and the
 * Release warning.
 *
 * "N of M nests have no sheet tied" comes from
 * GET /laser-nests/work-orders/{id}/tie-summary (ADMIN / MANAGER / SUPERVISOR).
 * Release on such a job WARNS and still releases (the plan's recommended
 * default: warn, never refuse); a job whose nests are all tied, or that has no
 * nests, releases straight away. Roles below work_orders:edit never call the
 * route.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import type { WorkOrderNestTieSummary } from '../types/untiedNests';

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
    getWorkOrderNestTieSummary: jest.fn(),
    releaseWorkOrder: jest.fn(),
    startWorkOrder: jest.fn(),
    updateWorkOrder: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 1, role: 'supervisor', is_superuser: false };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const OP = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: '10',
  name: 'Laser Cut',
  status: 'pending',
  quantity_complete: 0,
  estimated_hours: 1,
};

const workOrder = {
  id: 42,
  version: 3,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'draft',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations: [OP],
};

function summary(overrides: Partial<WorkOrderNestTieSummary> = {}): WorkOrderNestTieSummary {
  return {
    work_order_id: 42,
    work_order_number: 'WO-0042',
    nest_count: 3,
    tied_count: 1,
    untied_count: 2,
    job_level_tie_count: 0,
    message: '2 of 3 nests have no sheet tied',
    untied: [
      {
        laser_nest_id: 1,
        work_order_operation_id: 71,
        work_order_id: 42,
        work_order_number: 'WO-0042',
        operation_number: '10',
        cnc_number: 'CNC-1001',
        nest_name: 'Nest 1',
        operation_status: 'pending',
      },
      {
        laser_nest_id: 2,
        work_order_operation_id: 72,
        work_order_id: 43,
        work_order_number: 'WO-0043',
        operation_number: '10',
        cnc_number: 'CNC-1002',
        nest_name: 'Nest 2',
        operation_status: 'pending',
      },
    ],
    ...overrides,
  };
}

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

const releaseButton = () => screen.getByRole('button', { name: /^release$/i });

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getWorkOrder.mockResolvedValue(workOrder as never);
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] } as never);
  mockedApi.getMaterialRequirements.mockResolvedValue(null as never);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([] as never);
  mockedApi.getActiveUsers.mockResolvedValue([] as never);
  mockedApi.getUsers.mockResolvedValue([] as never);
  mockedApi.getDocuments.mockResolvedValue([] as never);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.releaseWorkOrder.mockResolvedValue({} as never);
});

describe('WorkOrderDetail — untied laser nests', () => {
  it('shows "N of M nests have no sheet tied" on the Material Ties panel', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValue(summary());
    renderDetail();

    const notice = await screen.findByTestId('wo-nest-tie-summary');
    expect(notice).toHaveTextContent('2 of 3 nests have no sheet tied');
    expect(within(notice).getByText('CNC-1001')).toBeInTheDocument();
    expect(within(notice).getByRole('link', { name: /untied laser nests/i })).toHaveAttribute(
      'href',
      '/laser-nests/untied?work_order_id=42'
    );
    expect(mockedApi.getWorkOrderNestTieSummary).toHaveBeenCalledWith(42);
  });

  it('warns at Release and still releases on confirm', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValue(summary());
    renderDetail();
    await screen.findByTestId('wo-nest-tie-summary');

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('2 of 3 nests have no sheet tied');
    expect(mockedApi.releaseWorkOrder).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /release anyway/i }));
    });
    await waitFor(() => expect(mockedApi.releaseWorkOrder).toHaveBeenCalledWith(42));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('releases straight away once every nest is tied (re-read at click time)', async () => {
    mockedApi.getWorkOrderNestTieSummary
      .mockResolvedValueOnce(summary())
      .mockResolvedValue(summary({ tied_count: 3, untied_count: 0, message: null, untied: [] }));
    renderDetail();
    await screen.findByTestId('wo-nest-tie-summary');

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    await waitFor(() => expect(mockedApi.releaseWorkOrder).toHaveBeenCalledWith(42));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows nothing and releases directly for a job with no nests', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValue(
      summary({ nest_count: 0, tied_count: 0, untied_count: 0, message: null, untied: [] })
    );
    renderDetail();
    await screen.findAllByText('Laser Cut');
    await waitFor(() => expect(mockedApi.getWorkOrderNestTieSummary).toHaveBeenCalled());
    expect(screen.queryByTestId('wo-nest-tie-summary')).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    expect(mockedApi.releaseWorkOrder).toHaveBeenCalledWith(42);
  });

  it('"Not yet" closes the warning without releasing', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValue(summary());
    renderDetail();
    await screen.findByTestId('wo-nest-tie-summary');

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /not yet/i }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.releaseWorkOrder).not.toHaveBeenCalled();
    expect(releaseButton()).toBeInTheDocument();
  });

  it('names a whole-job tie in the Release warning', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValue(summary({ job_level_tie_count: 1 }));
    renderDetail();
    await screen.findByTestId('wo-nest-tie-summary');

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/1 whole-work-order material tie;/);
    expect(dialog).toHaveTextContent(/draw the same sheet twice/);
  });

  it('never blocks Release: a failed click-time re-read still releases', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockResolvedValueOnce(summary()).mockRejectedValue(new Error('network down'));
    renderDetail();
    await screen.findByTestId('wo-nest-tie-summary');

    await act(async () => {
      fireEvent.click(releaseButton());
    });
    await waitFor(() => expect(mockedApi.releaseWorkOrder).toHaveBeenCalledWith(42));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders nothing when the summary read is refused', async () => {
    mockedApi.getWorkOrderNestTieSummary.mockRejectedValue({ response: { status: 403 } });
    renderDetail();
    await screen.findAllByText('Laser Cut');
    await waitFor(() => expect(mockedApi.getWorkOrderNestTieSummary).toHaveBeenCalled());
    expect(screen.queryByTestId('wo-nest-tie-summary')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('never reads the summary for a role below work_orders:edit', async () => {
    mockUser = { id: 2, role: 'quality', is_superuser: false };
    renderDetail();
    await screen.findAllByText('Laser Cut');
    expect(mockedApi.getWorkOrderNestTieSummary).not.toHaveBeenCalled();
    expect(screen.queryByTestId('wo-nest-tie-summary')).not.toBeInTheDocument();
  });
});

/**
 * WorkOrderDetail — Start, Resume Work Order, Hold Work Order, and Complete on a held job.
 *
 * Owner ruling 2026-11-05 made the work-order start supervisor-tier and
 * released-only, and took the hold-lift off it. The office therefore needs its
 * own two doors for a WORK-ORDER hold, and they are named so they cannot be
 * confused with the OPERATION-level "Clear hold" on the table below:
 *
 *  - Start: ADMIN/MANAGER/SUPERVISOR only (work_orders:edit), released only, a
 *    LoadingButton (non-optimistic), a refusal shown verbatim;
 *  - Resume Work Order: on_hold + supervisor-tier, behind a ConfirmDialog; the
 *    server decides where the job goes back to and says so, and a DRAFT landing
 *    is a `warning` (the call succeeded, but the job is not back on the floor);
 *  - Hold Work Order: released / in_progress + supervisor-tier, the existing
 *    version-gated PUT {status: 'on_hold'}; never offered on a draft (the server
 *    refuses DRAFT→ON_HOLD);
 *  - Complete on a READY operation of a held job is disabled — completing would
 *    START it, which the server refuses 409 — while an IN_PROGRESS operation
 *    stays completable (labor already running must always be closable).
 *
 * Harness mirrors WorkOrderDetail.clearHold.test.tsx (side channels mocked,
 * ToastProvider mounted so toast text actually renders).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    getMaterialAllocations: jest.fn(),
    startWorkOrder: jest.fn(),
    resumeWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    updateWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'supervisor',
  is_superuser: false,
};

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

// Verbatim from backend app/services/operation_action_gates.py / the resume route.
const WO_ON_HOLD =
  "This work order is on hold — work cannot start until a supervisor resumes it (Resume Work Order on the work order). Resuming or clearing an operation's hold does not lift it.";
const RESUMED_TO_DRAFT =
  'No release is on record for this work order, so it returned to DRAFT — release it to put it back on the floor.';
const RESUMED_TO_IN_PROGRESS = 'Work order resumed — it is back in progress.';
const COMPLETE_ON_HOLD_TITLE = 'Resume the work order first — completing would start this operation';

const READY_OP = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: '10',
  name: 'Laser Cut',
  status: 'ready',
  quantity_complete: 0,
  estimated_hours: 1,
};

const RUNNING_OP = {
  id: 72,
  work_order_id: 42,
  sequence: 10,
  operation_number: '20',
  name: 'Deburr',
  status: 'in_progress',
  quantity_complete: 0,
  estimated_hours: 1,
};

const workOrderFixture = (status: string, operations: unknown[] = [READY_OP]) => ({
  id: 42,
  version: 3,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status,
  priority: 3,
  estimated_hours: 8,
  actual_hours: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations,
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

function setStatus(status: string, operations?: unknown[]) {
  mockedApi.getWorkOrder.mockResolvedValue(workOrderFixture(status, operations));
}

/** The page has loaded once the operations table renders. */
async function loaded() {
  await screen.findAllByText(/Laser Cut|Deburr/);
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  setStatus('released');
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.startWorkOrder.mockResolvedValue({ message: 'Work order started' });
  mockedApi.updateWorkOrder.mockResolvedValue({});
});

describe('WorkOrderDetail — Start', () => {
  it.each(['operator', 'viewer', 'quality'])('hides Start from %s (the server would 403)', async role => {
    mockUser = { id: 2, role, is_superuser: false };
    renderDetail();

    await loaded();
    expect(screen.queryByRole('button', { name: /^start$/i })).not.toBeInTheDocument();
  });

  it('starts a released job for a supervisor, then reloads (non-optimistic)', async () => {
    renderDetail();
    await loaded();
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^start$/i }));
    });

    expect(mockedApi.startWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.startWorkOrder).toHaveBeenCalledWith(42);
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
    expect(await screen.findByText('Work order started')).toBeInTheDocument();
  });

  it('shows a 409 refusal verbatim in a toast', async () => {
    mockedApi.startWorkOrder.mockRejectedValue({ response: { status: 409, data: { detail: WO_ON_HOLD } } });
    renderDetail();
    await loaded();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^start$/i }));
    });

    expect(await screen.findByText(WO_ON_HOLD)).toBeInTheDocument();
  });

  it('is not offered on a held or draft job', async () => {
    setStatus('on_hold');
    const { unmount } = renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: /^start$/i })).not.toBeInTheDocument();
    unmount();

    setStatus('draft');
    renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: /^start$/i })).not.toBeInTheDocument();
  });
});

describe('WorkOrderDetail — Resume Work Order', () => {
  it('is offered only on a held job, and only to the supervisor tier', async () => {
    setStatus('on_hold');
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    const { unmount } = renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: 'Resume Work Order' })).not.toBeInTheDocument();
    unmount();

    setStatus('released');
    mockUser = { id: 1, role: 'supervisor', is_superuser: false };
    renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: 'Resume Work Order' })).not.toBeInTheDocument();
  });

  it('confirms first, calls resume once, toasts the DRAFT outcome verbatim, and reloads', async () => {
    setStatus('on_hold');
    mockedApi.resumeWorkOrder.mockResolvedValue({
      work_order_id: 42,
      message: RESUMED_TO_DRAFT,
      status: 'draft',
      restore_basis: 'no_release_record',
      operations_returned_to_pending: 1,
    });
    renderDetail();
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Resume Work Order' }));
    const dialog = await screen.findByRole('dialog');
    // The copy says where the job can land, and that operation holds stay.
    expect(dialog).toHaveTextContent(/In Progress/);
    expect(dialog).toHaveTextContent(/Released/);
    expect(dialog).toHaveTextContent(/Draft/);
    expect(dialog).toHaveTextContent(/Holds on individual operations stay in place/);
    expect(mockedApi.resumeWorkOrder).not.toHaveBeenCalled();

    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume Work Order' }));
    });

    expect(mockedApi.resumeWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.resumeWorkOrder).toHaveBeenCalledWith(42);
    expect(await screen.findByText(RESUMED_TO_DRAFT)).toBeInTheDocument();
    // A draft landing SUCCEEDED but left the job off the floor: warning, not success.
    expect(screen.getByText(RESUMED_TO_DRAFT).closest('[role="alert"]')).not.toBeNull();
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('toasts an ordinary resume as success', async () => {
    setStatus('on_hold');
    mockedApi.resumeWorkOrder.mockResolvedValue({
      work_order_id: 42,
      message: RESUMED_TO_IN_PROGRESS,
      status: 'in_progress',
      restore_basis: 'release_record_with_work',
      operations_returned_to_pending: 0,
    });
    renderDetail();
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Resume Work Order' }));
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume Work Order' }));
    });

    expect(await screen.findByText(RESUMED_TO_IN_PROGRESS)).toBeInTheDocument();
    expect(screen.getByText(RESUMED_TO_IN_PROGRESS).closest('[role="alert"]')).toBeNull();
  });

  it('keeps the dialog open and shows a refusal verbatim', async () => {
    setStatus('on_hold');
    const audit503 = 'The audit record for resuming this work order could not be saved; nothing was changed. Retry.';
    mockedApi.resumeWorkOrder.mockRejectedValue({ response: { status: 503, data: { detail: audit503 } } });
    renderDetail();
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Resume Work Order' }));
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume Work Order' }));
    });

    expect(await screen.findByText(audit503)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

describe('WorkOrderDetail — Hold Work Order', () => {
  it.each(['released', 'in_progress'])('PUTs {status: on_hold, version} on a %s job after confirming', async status => {
    setStatus(status, [RUNNING_OP]);
    renderDetail();
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Hold Work Order' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/cannot check in to or start any of its operations/);
    expect(dialog).toHaveTextContent(/already clocked in can still clock out/);
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Hold Work Order' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { status: 'on_hold', version: 3 });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it.each(['draft', 'on_hold'])('is not offered on a %s job', async status => {
    setStatus(status);
    renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: 'Hold Work Order' })).not.toBeInTheDocument();
  });

  it('is hidden from an operator', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    await loaded();
    expect(screen.queryByRole('button', { name: 'Hold Work Order' })).not.toBeInTheDocument();
  });
});

describe('WorkOrderDetail — Complete on a held job', () => {
  it('disables Complete on a READY operation (completing would start it)', async () => {
    setStatus('on_hold', [READY_OP]);
    renderDetail();
    await loaded();

    const complete = screen.getByTitle(COMPLETE_ON_HOLD_TITLE);
    expect(complete).toBeDisabled();
    expect(complete).toHaveTextContent('Complete');
  });

  it('keeps Complete enabled on an IN_PROGRESS operation (running labor stays closable)', async () => {
    setStatus('on_hold', [RUNNING_OP]);
    renderDetail();
    await loaded();

    expect(screen.queryByTitle(COMPLETE_ON_HOLD_TITLE)).not.toBeInTheDocument();
    expect(screen.getByTitle('Complete Operation')).toBeEnabled();
  });

  it('keeps Complete enabled on a READY operation of a released job (control)', async () => {
    setStatus('released', [READY_OP]);
    renderDetail();
    await loaded();

    expect(screen.getByTitle('Complete Operation')).toBeEnabled();
  });
});

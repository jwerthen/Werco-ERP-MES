/**
 * Process Sheets PR 3 — the office/admin side of the completion gate.
 *
 * Completing an operation from WorkOrderDetail is server-gated: when the backend
 * refuses with 409 {code:"STEPS_INCOMPLETE", missing:[...]} (an OBJECT detail),
 * the page must (a) toast the missing labels/serials readably — never JSON soup
 * or a false success — and (b) auto-expand that operation's read-only
 * OperationStepsPanel so the evidence gaps are visible inline. A plain string
 * 409 detail still surfaces verbatim WITHOUT opening the panel.
 *
 * Harness mirrors WorkOrderDetail.completeGuard.test.tsx (all side-channels
 * mocked), plus a real ToastProvider so the toast text is assertable and a
 * mocked getOperationSteps for the auto-opened panel.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
    completeWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    getOperationSteps: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
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

const OPERATION = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: 'OP10',
  name: 'Final inspect',
  status: 'in_progress',
  quantity_complete: 0,
  estimated_hours: 1,
};

const workOrderFixture = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 4,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations: [OPERATION],
};

const STEPS_VIEW = {
  operation_id: 71,
  work_order_id: 42,
  work_order_number: 'WO-0042',
  operation_status: 'in_progress',
  is_serialized: false,
  serial_numbers: [],
  steps: [
    {
      id: 501,
      work_order_operation_id: 71,
      source_sheet_id: 5,
      source_sheet_revision: 'B',
      sequence: 10,
      label: 'Torque check',
      instruction_text: null,
      step_type: 'checkbox',
      is_required: true,
      config: null,
      requires_gauge: false,
      spc_characteristic_id: null,
      created_at: '2026-07-01T12:00:00Z',
      records: [],
      complete: false,
      missing_serials: [],
    },
  ],
  steps_total: 1,
  steps_recorded: 0,
  completeness: {},
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

/** Open the per-row operation CompleteWorkModal and submit it with the defaults. */
async function submitOperationCompletion() {
  const rowButton = await screen.findByTitle('Complete Operation');
  fireEvent.click(rowButton);
  const dialog = await screen.findByRole('dialog');
  // act-wrap the submit so the mutation's promise continuations (toast, panel
  // open, in-flight reset) flush inside the act boundary.
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: /^Complete$/ }));
  });
  return dialog;
}

describe('WorkOrderDetail STEPS_INCOMPLETE completion refusal', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    mockedApi.getWorkOrder.mockResolvedValue({ ...workOrderFixture });
    mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
    mockedApi.getMaterialRequirements.mockResolvedValue(null);
    mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
    mockedApi.getActiveUsers.mockResolvedValue([]);
    mockedApi.getUsers.mockResolvedValue([]);
    mockedApi.getDocuments.mockResolvedValue([]);
    mockedApi.getOperationSteps.mockResolvedValue(STEPS_VIEW);
  });

  it('toasts the missing steps readably and auto-opens the operation evidence panel', async () => {
    mockedApi.completeWOOperation.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'STEPS_INCOMPLETE',
            detail: 'Required process-sheet steps are missing conforming records for this operation',
            missing: [{ step_id: 501, label: 'Torque check', serials: ['SN-2'] }],
          },
        },
      },
    });

    renderDetail();
    await submitOperationCompletion();

    // Human toast — labels + outstanding serials, never "[object Object]".
    expect(
      await screen.findByText('Required process steps are missing records: Torque check (SN-2)')
    ).toBeInTheDocument();

    // The refused operation's evidence panel auto-expands and fetches the view.
    expect(await screen.findByTestId('operation-steps-panel')).toBeInTheDocument();
    expect(mockedApi.getOperationSteps).toHaveBeenCalledWith(71);
    expect(screen.getByText('Torque check')).toBeInTheDocument();

    // Server-gated + non-optimistic: the completion modal is gone, nothing "completed".
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.completeWOOperation).toHaveBeenCalledTimes(1);
  });

  it('surfaces a plain string 409 verbatim and does NOT open the evidence panel', async () => {
    mockedApi.completeWOOperation.mockRejectedValue({
      response: { status: 409, data: { detail: 'Operation is on hold and cannot be completed' } },
    });

    renderDetail();
    const dialog = await submitOperationCompletion();

    expect(await screen.findByText('Operation is on hold and cannot be completed')).toBeInTheDocument();
    expect(screen.queryByTestId('operation-steps-panel')).not.toBeInTheDocument();
    expect(mockedApi.getOperationSteps).not.toHaveBeenCalled();

    // String-detail refusals keep the modal open for a retry; wait for the
    // in-flight flag to clear (submit re-enables) so no state lands post-test.
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /^Complete$/ })).toBeEnabled());
  });

  it('the row "Steps" toggle expands and collapses the read-only panel manually', async () => {
    renderDetail();

    const toggle = await screen.findByTitle('Process steps evidence');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await act(async () => {
      fireEvent.click(toggle); // panel fetch resolves inside the act boundary
    });

    expect(await screen.findByTestId('operation-steps-panel')).toBeInTheDocument();
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(mockedApi.getOperationSteps).toHaveBeenCalledWith(71);

    fireEvent.click(toggle);
    await waitFor(() => expect(screen.queryByTestId('operation-steps-panel')).not.toBeInTheDocument());
  });
});

/**
 * WorkOrderDetail — supervisor "Correct count" (office reduce-production).
 *
 * The operations table gains a role-gated "Correct count" action (work_orders:edit
 * → admin/manager/supervisor) on operations with a recorded count. It opens a
 * small quantity+reason modal that calls api.reduceWOOperationProduction — a
 * server-gated correction, so the flow is NON-optimistic:
 *  - submit stays disabled until quantity > 0 AND a non-blank reason is typed;
 *  - the exact payload {quantity_delta, reason, source:'desktop'} is pinned;
 *  - success closes the modal and refetches the WO (server state only);
 *  - a refusal renders the server's verbatim `detail` INLINE in the modal as
 *    role="alert" (production feedback: toast-only refusals were unreadable),
 *    with the modal open and form state intact.
 *
 * Harness mirrors WorkOrderDetail.completeGuard.test.tsx (side-channels mocked).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';

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
    reduceWOOperationProduction: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

// Mutable so individual tests can exercise the role gate (jest allows lazily
// referenced `mock*` variables inside the hoisted factory).
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

const COUNTED_OP = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: 'OP10',
  name: 'Deburr',
  status: 'in_progress',
  quantity_complete: 5,
  estimated_hours: 1,
};

const ZERO_COUNT_OP = {
  id: 72,
  work_order_id: 42,
  sequence: 20,
  operation_number: 'OP20',
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
  operations: [COUNTED_OP, ZERO_COUNT_OP],
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/work-orders/42']}>
      <Routes>
        <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
      </Routes>
    </MemoryRouter>
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
  mockedApi.reduceWOOperationProduction.mockResolvedValue({});
});

async function openCorrectModal() {
  renderDetail();
  // Only the op WITH a recorded count offers the action.
  const button = await screen.findByRole('button', { name: /correct count/i });
  fireEvent.click(button);
  return screen.findByRole('heading', { name: /correct count — op 10 deburr/i });
}

describe('WorkOrderDetail supervisor Correct count', () => {
  it('offers the action only on operations with a recorded count', async () => {
    renderDetail();
    // Exactly ONE button — op 20 has quantity_complete 0.
    const buttons = await screen.findAllByRole('button', { name: /correct count/i });
    expect(buttons).toHaveLength(1);
  });

  it('hides the action from roles without work_orders:edit (operator)', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    // Wait for the table to render, then assert absence.
    await screen.findByText('Deburr');
    expect(screen.queryByRole('button', { name: /correct count/i })).not.toBeInTheDocument();
  });

  it('requires a reason, pins the exact payload, and refetches on success', async () => {
    await openCorrectModal();

    // Quantity defaults to 1 but the reason is blank → submit blocked.
    const submit = screen.getByRole('button', { name: /remove from completed/i });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/quantity to remove/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/reason for correction/i), {
      target: { value: 'operator double-scanned the tray' },
    });
    expect(submit).toBeEnabled();

    const woReadsBefore = mockedApi.getWorkOrder.mock.calls.length;
    fireEvent.click(submit);

    await waitFor(() => expect(mockedApi.reduceWOOperationProduction).toHaveBeenCalledTimes(1));
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenCalledWith(71, {
      quantity_delta: 3,
      reason: 'operator double-scanned the tray',
      source: 'desktop',
    });
    // Non-optimistic: the page re-reads the WO instead of patching the count.
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(woReadsBefore));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /correct count — op 10 deburr/i })).not.toBeInTheDocument()
    );
  });

  it('disables submit while the request is in flight (double-click guard)', async () => {
    let resolveReduce: (value: unknown) => void = () => {};
    mockedApi.reduceWOOperationProduction.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveReduce = resolve;
        })
    );
    await openCorrectModal();

    fireEvent.change(screen.getByLabelText(/reason for correction/i), {
      target: { value: 'double-click test' },
    });
    const submit = screen.getByRole('button', { name: /remove from completed/i });
    fireEvent.click(submit);

    // In flight: the submit control is disabled and a re-click cannot fire a second call.
    await waitFor(() => expect(submit).toBeDisabled());
    fireEvent.click(submit);
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenCalledTimes(1);
    // Non-optimistic: still no refusal, no local patch — the modal simply waits.
    expect(screen.getByRole('heading', { name: /correct count — op 10 deburr/i })).toBeInTheDocument();

    resolveReduce({});
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /correct count — op 10 deburr/i })).not.toBeInTheDocument()
    );
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenCalledTimes(1);
  });

  it('renders the server refusal verbatim INLINE in the modal and keeps it open', async () => {
    const refusal = 'Approved labor cannot be corrected here -- unapprove it first';
    mockedApi.reduceWOOperationProduction.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    await openCorrectModal();

    fireEvent.change(screen.getByLabelText(/reason for correction/i), { target: { value: 'oops' } });
    fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));

    const inline = await screen.findByTestId('wo-correct-error');
    expect(inline).toHaveTextContent(refusal);
    expect(inline).toHaveAttribute('role', 'alert');
    // Modal stays open with form state intact; the count was never patched locally.
    expect(screen.getByRole('heading', { name: /correct count — op 10 deburr/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/reason for correction/i)).toHaveValue('oops');
    expect(within(screen.getByRole('dialog')).getByText(/completed now:/i)).toHaveTextContent('5 / 10');
  });
});

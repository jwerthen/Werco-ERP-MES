/**
 * Process Sheets PR 3 — the force-complete override notice.
 *
 * POST /work-orders/{id}/complete may return steps_bypassed:
 * {count, steps:[{operation, step_id, label, serials}], truncated} | null.
 * Non-null means an AUTHORIZED user force-completed the WO with required step
 * records bypassed — a deliberate, audited override. The page must surface a
 * clear notice ("Completed with N step records bypassed: …") in an INFO tone
 * (role=status, never an error alert — the action succeeded by design). A null
 * steps_bypassed keeps today's behavior: modal closes, reload, no notice.
 *
 * Harness mirrors WorkOrderDetail.stepsIncomplete.test.tsx (side-channels
 * mocked, real ToastProvider so the toast text/role are assertable).
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
  operations: [],
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

/** Open the WO-level CompleteWorkModal (the header button, not a row action) and submit. */
async function submitWorkOrderCompletion() {
  await screen.findAllByText('WO-0042');
  const headerComplete = screen
    .getAllByRole('button', { name: /^complete$/i })
    .find((b) => !b.getAttribute('title'));
  expect(headerComplete).toBeDefined();
  fireEvent.click(headerComplete as HTMLElement);
  const dialog = await screen.findByRole('dialog');
  // act-wrap so the mutation's continuations (toast, reload) flush in-boundary.
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: /^Complete$/ }));
  });
}

describe('WorkOrderDetail steps_bypassed force-complete notice', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    mockedApi.getWorkOrder.mockResolvedValue({ ...workOrderFixture });
    mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
    mockedApi.getMaterialRequirements.mockResolvedValue(null);
    mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
    mockedApi.getActiveUsers.mockResolvedValue([]);
    mockedApi.getUsers.mockResolvedValue([]);
    mockedApi.getDocuments.mockResolvedValue([]);
  });

  it('a completion carrying steps_bypassed shows the override notice (info tone, labels listed)', async () => {
    mockedApi.completeWorkOrder.mockResolvedValue({
      message: 'Work order completed',
      quality_exceptions: [],
      steps_bypassed: {
        count: 3,
        steps: [
          { operation: 'OP10', step_id: 501, label: 'Torque check', serials: ['SN-1', 'SN-2'] },
          { operation: 'OP20', step_id: 502, label: 'Deburr edges', serials: [] },
        ],
        truncated: false,
      },
    });

    renderDetail();
    await submitWorkOrderCompletion();

    // Clear notice with the server count + step labels — the action SUCCEEDED
    // by design (audited override), so info tone, never an error alert.
    const notice = await screen.findByText('Completed with 3 step records bypassed: Torque check, Deburr edges');
    expect(notice.closest('[role="status"]')).not.toBeNull();
    expect(notice.closest('[role="alert"]')).toBeNull();

    // Normal success path otherwise: modal closed, WO reloaded.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(1);
  });

  it('a null steps_bypassed keeps today\'s behavior: no notice, modal closes, WO reloads', async () => {
    mockedApi.completeWorkOrder.mockResolvedValue({
      message: 'Work order completed',
      quality_exceptions: [],
      steps_bypassed: null,
    });

    renderDetail();
    await submitWorkOrderCompletion();

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.queryByText(/bypassed/i)).not.toBeInTheDocument();
    expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(1);
  });
});

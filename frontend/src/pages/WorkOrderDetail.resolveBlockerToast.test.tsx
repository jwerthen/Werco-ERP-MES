/**
 * WorkOrderDetail — resolve-blocker feedback.
 *
 * Resolving a blocker used to succeed silently (the list simply refreshed);
 * now success shows a toast naming the blocker, mirroring the page's other
 * mutation toasts. The error path (verbatim server detail in an error toast)
 * is pinned as existing behavior. The note capture goes through the shared
 * InputDialog (the native prompt() is gone), so the tests drive the dialog:
 * open via the page's Resolve button, then submit via the DIALOG's Resolve
 * button (scoped `within(dialog)` — the two buttons share a name). Dialog
 * mechanics themselves are covered in WorkOrderDetail.resolveBlockerDialog
 * .test.tsx; this file owns the toast feedback.
 *
 * Harness mirrors WorkOrderDetail.correctCount.test.tsx (side-channels
 * mocked), plus ToastProvider so the global toast text actually renders.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
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
    resolveWorkOrderBlocker: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    // MaterialTiesPanel loads on mount; an unmocked method is `undefined` and
    // surfaces as a silent <ErrorState role="alert"> instead of a red test.
    getMaterialAllocations: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
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

const BLOCKER = {
  id: 7,
  company_id: 1,
  work_order_id: 42,
  operation_id: null,
  material_part_id: null,
  category: 'material_missing',
  severity: 'high',
  status: 'open',
  title: 'Material missing',
  note: null,
  reported_at: '2026-01-01T00:00:00Z',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
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

// Opens the InputDialog via the page's Resolve button and submits it with the
// pre-filled default note ("Resolved") via the dialog's own Resolve button.
async function resolveViaDialog() {
  fireEvent.click(await screen.findByRole('button', { name: /^resolve$/i }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByLabelText(/resolution note/i)).toHaveValue('Resolved');
  // Async act so the submit's whole microtask chain (api settle -> toast ->
  // pending-cleared) flushes inside act — no unwrapped-update warnings.
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: /^resolve$/i }));
  });
  return dialog;
}

beforeEach(() => {
  jest.clearAllMocks();

  mockedApi.getWorkOrder.mockResolvedValue({ ...workOrderFixture });
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([BLOCKER]);
  mockedApi.resolveWorkOrderBlocker.mockResolvedValue({ ...BLOCKER, status: 'resolved' });
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
});

describe('WorkOrderDetail resolve-blocker toast', () => {
  it('shows a success toast naming the blocker after resolve + refetch', async () => {
    renderDetail();

    await resolveViaDialog();

    await waitFor(() =>
      expect(mockedApi.resolveWorkOrderBlocker).toHaveBeenCalledWith(7, 'Resolved')
    );
    // Toast fires after the non-optimistic refetch completes (and the dialog closes).
    expect(await screen.findByText('Resolved blocker "Material missing"')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the server detail in an error toast when the resolve is refused (existing behavior)', async () => {
    const refusal = 'Blocker already resolved by someone else';
    mockedApi.resolveWorkOrderBlocker.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    renderDetail();

    const dialog = await resolveViaDialog();

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText(/resolved blocker/i)).not.toBeInTheDocument();
    // Wait for the rejection's `finally` to clear the pending state (the
    // dialog's Resolve re-enables) so the update lands inside act().
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /^resolve$/i })).toBeEnabled()
    );
  });
});

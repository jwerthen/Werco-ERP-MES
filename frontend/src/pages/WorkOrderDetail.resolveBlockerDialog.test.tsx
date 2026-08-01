/**
 * WorkOrderDetail — resolving a blocker now captures its note through the
 * shared InputDialog instead of the native prompt().
 *
 * Covers: the Resolve button opens the dialog (default note "Resolved",
 * message naming the blocker), submit resolves with the entered trimmed note
 * and refetches the work order non-optimistically, cancel resolves nothing,
 * and a server refusal surfaces the verbatim detail while the dialog stays
 * open for retry. Harness mirrors WorkOrderDetail.correctCount.test.tsx
 * (side-channels mocked) plus ToastProvider so toast text actually renders.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

async function openResolveDialog() {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: /^resolve$/i }));
  return await screen.findByRole('dialog');
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

describe('WorkOrderDetail resolve-blocker InputDialog', () => {
  it('opens the dialog naming the blocker with the default "Resolved" note', async () => {
    await openResolveDialog();

    expect(screen.getByText('Resolve Blocker')).toBeInTheDocument();
    expect(screen.getByText('Resolve blocker "Material missing"?')).toBeInTheDocument();
    expect(screen.getByLabelText(/resolution note/i)).toHaveValue('Resolved');
    expect(mockedApi.resolveWorkOrderBlocker).not.toHaveBeenCalled();
  });

  it('submit resolves with the entered trimmed note, refetches, and closes', async () => {
    const dialog = await openResolveDialog();

    fireEvent.change(screen.getByLabelText(/resolution note/i), {
      target: { value: '  Vendor delivered this morning  ' },
    });
    // Scoped to the dialog — the page's own Resolve button shares the name.
    fireEvent.click(within(dialog).getByRole('button', { name: /^resolve$/i }));

    await waitFor(() =>
      expect(mockedApi.resolveWorkOrderBlocker).toHaveBeenCalledWith(7, 'Vendor delivered this morning')
    );
    // Non-optimistic: the work order is refetched (initial load + post-resolve).
    await waitFor(() => expect(mockedApi.getWorkOrder).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancel closes the dialog and resolves nothing', async () => {
    await openResolveDialog();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.resolveWorkOrderBlocker).not.toHaveBeenCalled();
  });

  it('a server refusal surfaces the verbatim detail and keeps the dialog open for retry', async () => {
    const refusal = 'Blocker already resolved by someone else';
    mockedApi.resolveWorkOrderBlocker.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    const dialog = await openResolveDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: /^resolve$/i }));

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

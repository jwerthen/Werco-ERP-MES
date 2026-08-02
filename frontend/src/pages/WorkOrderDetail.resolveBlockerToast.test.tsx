/**
 * WorkOrderDetail — resolve-blocker feedback.
 *
 * Resolving a blocker used to succeed silently (the list simply refreshed);
 * now success shows a toast naming the blocker, mirroring the page's other
 * mutation toasts. The error path (verbatim server detail in an error toast)
 * is pinned as existing behavior. The native prompt() note capture is
 * deliberately untouched here — a separate UX PR replaces it.
 *
 * Harness mirrors WorkOrderDetail.correctCount.test.tsx (side-channels
 * mocked), plus ToastProvider so the global toast text actually renders.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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

let promptSpy: jest.SpyInstance;

beforeEach(() => {
  jest.clearAllMocks();
  promptSpy = jest.spyOn(window, 'prompt').mockReturnValue('Resolved');

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

afterEach(() => {
  promptSpy.mockRestore();
});

describe('WorkOrderDetail resolve-blocker toast', () => {
  it('shows a success toast naming the blocker after resolve + refetch', async () => {
    renderDetail();

    fireEvent.click(await screen.findByRole('button', { name: /^resolve$/i }));

    expect(promptSpy).toHaveBeenCalledWith('Resolve blocker "Material missing"?', 'Resolved');
    await waitFor(() =>
      expect(mockedApi.resolveWorkOrderBlocker).toHaveBeenCalledWith(7, 'Resolved')
    );
    // Toast fires after the non-optimistic refetch completes.
    expect(await screen.findByText('Resolved blocker "Material missing"')).toBeInTheDocument();
  });

  it('shows the server detail in an error toast when the resolve is refused (existing behavior)', async () => {
    const refusal = 'Blocker already resolved by someone else';
    mockedApi.resolveWorkOrderBlocker.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    renderDetail();

    fireEvent.click(await screen.findByRole('button', { name: /^resolve$/i }));

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText(/resolved blocker/i)).not.toBeInTheDocument();
  });
});

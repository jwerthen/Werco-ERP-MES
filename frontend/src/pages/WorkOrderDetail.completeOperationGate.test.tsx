/**
 * WorkOrderDetail — the office "Complete" operation action is ROLE-GATED.
 *
 * This button was ungated: any authenticated user who could load the page could
 * complete an operation. That was already too loose, and it became load-bearing when
 * operation completion started depleting tied material — the button now moves stock
 * and writes ledger + tamper-evident hash-chain rows, so a Viewer must never see it.
 *
 * The client gate mirrors the server's exactly, which is the only thing that makes it
 * more than decoration: the backend is
 * `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` on
 * `POST /work-orders/operations/{id}/complete`, and `require_role` additionally admits
 * PLATFORM_ADMIN and superusers. The client resolves the same set via
 * `hasPermission(role, 'work_orders:edit')` (platform_admin/admin/manager/supervisor)
 * `|| is_superuser || role === 'quality'`.
 *
 * QUALITY is the case worth naming: it is NOT in `work_orders:edit`, so the obvious
 * expression (reusing `canCorrectCount`) silently drops it — and a Quality user can
 * complete an entire work order, so refusing them a single operation would be
 * incoherent. The backend-side omission of QUALITY was caught by a test rather than by
 * inspection; this is the client half of that lesson.
 *
 * A hidden button is not the enforcement — the server is. What this pins is that the
 * UI does not offer an action the server will refuse, and does not hide one it allows.
 *
 * Harness mirrors WorkOrderDetail.correctCount.test.tsx (side-channels mocked).
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
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
    getMaterialAllocations: jest.fn(),
    completeWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    reduceWOOperationProduction: jest.fn(),
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

const OPEN_OP = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: 'OP10',
  name: 'Laser Cut',
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
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations: [OPEN_OP],
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

/**
 * The PER-OPERATION action, selected by its `title` rather than its accessible name.
 * The page header carries its own work-order-level button whose accessible name is
 * also exactly "Complete" (see WorkOrderDetail.completeGuard.test.tsx), so a
 * name-based query would count both and every "hidden" assertion below would be
 * measuring the wrong button.
 */
const completeOperationButtons = () => screen.queryAllByTitle('Complete Operation');

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
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.completeWOOperation.mockResolvedValue({});
});

describe('WorkOrderDetail office operation Complete — role gate', () => {
  // The server's allowed set, plus the two identities require_role admits implicitly.
  it.each([
    ['admin', false],
    ['manager', false],
    ['supervisor', false],
    // NOT in work_orders:edit — the case the obvious expression drops.
    ['quality', false],
    // require_role returns early for PLATFORM_ADMIN and for any superuser.
    ['platform_admin', false],
    ['viewer', true],
  ])('offers the action to %s (is_superuser=%s)', async (role, isSuperuser) => {
    mockUser = { id: 2, role, is_superuser: isSuperuser as boolean };
    renderDetail();

    await screen.findByText('Laser Cut');
    expect(completeOperationButtons()).toHaveLength(1);
  });

  it.each(['operator', 'shipping', 'viewer'])('hides the action from %s', async (role) => {
    mockUser = { id: 3, role, is_superuser: false };
    renderDetail();

    // Wait for the operations table to render before asserting absence, so this
    // cannot pass merely because nothing had loaded yet.
    await screen.findByText('Laser Cut');
    expect(completeOperationButtons()).toHaveLength(0);
  });

  it('still opens the completion dialog when an allowed role clicks it', async () => {
    // The gate must not be hiding a button that no longer works: a "hidden for
    // everyone" regression would satisfy every negative assertion above. Opening the
    // dialog is the right depth here — the button only sets the completion target, and
    // the modal's own submit/refusal behavior is covered by
    // WorkOrderDetail.completeGuard.test.tsx.
    mockUser = { id: 4, role: 'quality', is_superuser: false };
    renderDetail();

    await screen.findByText('Laser Cut');
    fireEvent.click(completeOperationButtons()[0]);

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

/**
 * WorkOrderDetail — the per-operation "Material" action is gated on work_orders:edit,
 * deliberately NOT on the larger set that may complete an operation.
 *
 * The two gates sitting side by side in the same Actions cell is exactly why this
 * needs pinning. `canCompleteOperation` is `work_orders:edit || quality`, because
 * QUALITY can complete an entire work order and refusing it a single operation
 * would be incoherent. Deciding what stock an operation EATS is a different
 * question: a tie is a planning decision that makes material leave inventory and
 * writes the lots it drew onto the as-built record, and the backend's tie
 * endpoints are `require_role([ADMIN, MANAGER, SUPERVISOR])` — no QUALITY.
 *
 * So the honest expression is `canEditMaterialTies`, and the failure mode this
 * test exists to catch is the tempting one: reusing the gate belonging to the
 * button immediately below it, which would silently hand a Quality user a
 * material-planning verb the server will refuse.
 *
 * A hidden button is not the enforcement — the server is. What this pins is that
 * the UI does not offer an action the server will refuse, and does not hide one
 * it allows.
 *
 * Harness mirrors WorkOrderDetail.completeOperationGate.test.tsx (side-channels
 * mocked), with the tie dialog's own client methods added to the api mock — this
 * page now mounts that dialog on every render.
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
    // PR 4.5: the per-operation tie editor and the backflush dry run are both
    // mounted by this page. Neither fetches on the closed / unopened path, but a
    // hand-written mock still has to carry them the moment either one is used.
    getMaterials: jest.fn(),
    createMaterialAllocation: jest.fn(),
    updateMaterialAllocation: jest.fn(),
    getWorkOrderBackflushPreview: jest.fn(),
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

/** Selected by `title`, like its neighbour — the label alone is not unique on the page. */
const materialButtons = () => screen.queryAllByTitle('Tie stock material to this operation');
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
  mockedApi.getMaterials.mockResolvedValue([]);
});

describe('WorkOrderDetail per-operation Material action — role gate', () => {
  it.each([
    ['admin', false],
    ['manager', false],
    ['supervisor', false],
    ['platform_admin', false],
    // require_role returns early for any superuser, whatever the nominal role.
    ['viewer', true],
  ])('offers the action to %s (is_superuser=%s)', async (role, isSuperuser) => {
    mockUser = { id: 2, role, is_superuser: isSuperuser as boolean };
    renderDetail();

    await screen.findByText('Laser Cut');
    expect(materialButtons()).toHaveLength(1);
  });

  it.each(['operator', 'shipping', 'viewer'])('hides the action from %s', async (role) => {
    mockUser = { id: 3, role, is_superuser: false };
    renderDetail();

    // Wait for the operations table before asserting absence, so this cannot
    // pass merely because nothing had loaded yet.
    await screen.findByText('Laser Cut');
    expect(materialButtons()).toHaveLength(0);
  });

  it('hides it from QUALITY, who may still complete the very same operation', async () => {
    // THE case. Both buttons live in one Actions cell, one line apart, and the
    // sets differ by exactly this role. Completing an operation records what
    // happened; tying material to it decides what leaves inventory, and the
    // backend's tie endpoints do not admit QUALITY.
    mockUser = { id: 4, role: 'quality', is_superuser: false };
    renderDetail();

    await screen.findByText('Laser Cut');
    expect(materialButtons()).toHaveLength(0);
    expect(completeOperationButtons()).toHaveLength(1);
  });

  it('opens the operation-scoped tie dialog when an allowed role clicks it', async () => {
    // The gate must not be hiding a button that no longer works: a
    // "hidden for everyone" regression would satisfy every negative assertion
    // above. The dialog's own create/edit behaviour is covered by
    // OperationMaterialTieModal.test.tsx.
    mockUser = { id: 5, role: 'supervisor', is_superuser: false };
    renderDetail();

    await screen.findByText('Laser Cut');
    fireEvent.click(materialButtons()[0]);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/Tie material to this operation/i);
    // The dialog names the operation it is scoped to, because operation scope is
    // the whole point of this door.
    //
    // The fixture stores `operation_number: 'OP10'` (free text the office types).
    // This used to assert /Op OP10/ — the DOUBLED prefix, because the dialog title
    // hard-coded its own `Op ` around the stored value. It now renders through the
    // shared `utils/operationLabel` helper, so the one operation reads `Op 10` here
    // exactly as it does on the kiosk, the shop floor and the dispatch board.
    expect(dialog).toHaveTextContent(/Op 10/);
    expect(dialog.textContent).not.toMatch(/Op\s+OP/i);
  });
});

describe('WorkOrderDetail backflush preview panel — mounted, but silent', () => {
  it('renders unloaded and fires no preview request on page load', async () => {
    // WorkOrderDetail is opened constantly and for every other reason. The dry
    // run is an extra read across the BOM, the routing and the stock ledger, so
    // it must stay opt-in — and a request here would also break every sibling
    // suite whose hand-written api mock predates this method.
    renderDetail();

    await screen.findByText('Laser Cut');
    expect(screen.getByText('Backflush Preview')).toBeInTheDocument();
    expect(mockedApi.getWorkOrderBackflushPreview).not.toHaveBeenCalled();
  });
});

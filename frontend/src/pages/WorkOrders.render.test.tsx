/**
 * FEPERF-5 — WorkOrders list render-correctness regression.
 *
 * The desktop list now renders through the shared <DataTable> primitive
 * (Batch 4), with WorkOrderMobileList still handling the small-screen layout.
 * This guards the thing that matters across that refactor: the rows still
 * render the same content and controls (WO#, part, status, actions) as before.
 *
 * The desktop table (`hidden lg:block`) and the mobile list (`lg:hidden`) BOTH
 * mount in jsdom (CSS visibility classes don't prune the DOM), so each work
 * order renders twice. Assertions are scoped to the desktop <table> to stay
 * deterministic.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import type { UserRole } from '../types';

// Only `useNavigate` is stubbed; MemoryRouter and the rest stay real.
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    // The Duplicate row action mounts DuplicateWorkOrderModal, which resolves
    // nest-ness with its own read because a WorkOrderSummary carries no operations.
    getWorkOrder: jest.fn(),
    duplicateWorkOrder: jest.fn(),
  },
}));

// Mutable so the RBAC block can re-render the page as a different role. Defaults
// to admin + superuser, which is what every pre-existing test in this file assumes.
const mockDefaultUser = { id: 1, role: 'admin' as UserRole, is_superuser: true };
let mockAuthUser: { id: number; role: UserRole; is_superuser: boolean } = { ...mockDefaultUser };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockAuthUser,
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

const draftWorkOrder = {
  id: 1,
  work_order_number: 'WO-1001',
  part_id: 10,
  work_order_type: 'production',
  part_number: 'PN-AAA',
  part_name: 'Bracket Assembly',
  part_type: 'manufactured',
  status: 'draft' as const,
  priority: 2,
  quantity_ordered: 50,
  quantity_complete: 0,
  customer_name: 'Acme Aero',
};

const inProgressWorkOrder = {
  id: 2,
  work_order_number: 'WO-1002',
  part_id: 20,
  work_order_type: 'production',
  part_number: 'PN-BBB',
  part_name: 'Housing',
  part_type: 'manufactured',
  status: 'in_progress' as const,
  priority: 3,
  quantity_ordered: 20,
  quantity_complete: 10,
  customer_name: 'Beta Defense',
};

function renderWorkOrders() {
  return render(
    <MemoryRouter>
      <WorkOrders />
    </MemoryRouter>
  );
}

/**
 * Wait for the loaded data table, then scope queries to it.
 *
 * While `loading` is true the page renders a SkeletonTable (also a <table>), so
 * we first wait for real row content (a WO-#### link), then return the closest
 * enclosing <table> — the desktop list — avoiding both the skeleton and the
 * duplicate mobile-card list.
 */
async function getDesktopTable(): Promise<HTMLElement> {
  // The WO number renders in BOTH the desktop table and a mobile card, so
  // findAllByRole returns two links; pick the one inside a <table>.
  const woLinks = await screen.findAllByRole('link', { name: 'WO-1001' });
  const tableLink = woLinks.find((el) => el.closest('table'));
  const table = tableLink?.closest('table');
  if (!table) throw new Error('expected a WO link inside the desktop <table>');
  return table as HTMLElement;
}

describe('FEPERF-5: WorkOrders list renders rows correctly after memo refactor', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAuthUser = { ...mockDefaultUser };
    mockedApi.getWorkOrders.mockResolvedValue([draftWorkOrder, inProgressWorkOrder]);
    mockedApi.releaseWorkOrder.mockResolvedValue({});
    mockedApi.deleteWorkOrder.mockResolvedValue({});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders a row per work order with number, part, status, and detail link', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    // Both work-order numbers render as links to their detail page.
    const link1001 = within(table).getByRole('link', { name: 'WO-1001' });
    const link1002 = within(table).getByRole('link', { name: 'WO-1002' });
    expect(link1001).toHaveAttribute('href', '/work-orders/1');
    expect(link1002).toHaveAttribute('href', '/work-orders/2');

    // Part numbers/names render.
    expect(within(table).getByText('PN-AAA')).toBeInTheDocument();
    expect(within(table).getByText('Bracket Assembly')).toBeInTheDocument();
    expect(within(table).getByText('PN-BBB')).toBeInTheDocument();

    // Status labels render (formatStatusLabel lowercases + de-underscores;
    // visual capitalization is CSS-only and not reflected in text content).
    expect(within(table).getByText('draft')).toBeInTheDocument();
    expect(within(table).getByText('in progress')).toBeInTheDocument();

    // One <tbody> row per work order.
    const dataRows = within(table).getAllByRole('row').filter((row) =>
      within(row).queryByRole('link', { name: /^WO-/ })
    );
    expect(dataRows).toHaveLength(2);
  });

  it('shows a Release control only on the draft row and wires it to releaseWorkOrder', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    const releaseButtons = within(table).getAllByTitle('Release');
    // Only the draft work order is releasable.
    expect(releaseButtons).toHaveLength(1);

    fireEvent.click(releaseButtons[0]);
    await waitFor(() => {
      expect(mockedApi.releaseWorkOrder).toHaveBeenCalledWith(1);
    });
  });

  it('shows a Delete control on each row and wires it to deleteWorkOrder via the confirm dialog', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    const deleteButtons = within(table).getAllByTitle('Delete');
    expect(deleteButtons).toHaveLength(2);

    // The row control opens the shared ConfirmDialog (no native window.confirm);
    // the API fires only from the dialog's Delete button.
    fireEvent.click(deleteButtons[1]);
    expect(mockedApi.deleteWorkOrder).not.toHaveBeenCalled();

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => {
      expect(mockedApi.deleteWorkOrder).toHaveBeenCalledWith(2);
    });
  });
});

/**
 * Duplicate is `require_role([ADMIN, MANAGER, SUPERVISOR])` on the server — the trio
 * `work_orders:edit` maps to. A hidden control and a refused call have to agree in
 * BOTH directions: a supervisor who cannot see the button loses a feature they are
 * entitled to, and an operator who can see it gets a 403 for their trouble. The
 * server stays the enforcement; this is the half that keeps the UI honest about it.
 */
describe('WorkOrders row actions: Duplicate is gated on work_orders:edit', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAuthUser = { ...mockDefaultUser };
    mockedApi.getWorkOrders.mockResolvedValue([draftWorkOrder, inProgressWorkOrder]);
    mockedApi.releaseWorkOrder.mockResolvedValue({});
    mockedApi.deleteWorkOrder.mockResolvedValue({});
    mockedApi.getWorkOrder.mockResolvedValue({
      id: 1,
      work_order_number: 'WO-1001',
      quantity_ordered: 50,
      operations: [],
    });
  });

  it.each([
    ['admin', true],
    ['manager', true],
    ['supervisor', true],
  ] as const)('shows Duplicate on every row for a %s', async (role, _allowed) => {
    mockAuthUser = { id: 1, role: role as UserRole, is_superuser: false };
    renderWorkOrders();
    const table = await getDesktopTable();

    expect(within(table).getAllByTitle('Duplicate')).toHaveLength(2);
  });

  it.each([
    ['operator'],
    ['viewer'],
    ['quality'],
    ['shipping'],
  ] as const)('hides Duplicate from a %s', async (role) => {
    mockAuthUser = { id: 1, role: role as UserRole, is_superuser: false };
    renderWorkOrders();
    const table = await getDesktopTable();

    // Gone, not disabled — and the row itself still renders, so the absence is
    // the gate rather than a blank page.
    expect(within(table).queryAllByTitle('Duplicate')).toHaveLength(0);
    expect(within(table).getByRole('link', { name: 'WO-1001' })).toBeInTheDocument();
  });

  it('labels each Duplicate control with its own work order number', async () => {
    mockAuthUser = { id: 1, role: 'supervisor' as UserRole, is_superuser: false };
    renderWorkOrders();
    const table = await getDesktopTable();

    // Icon-only control, so the accessible name has to carry the row identity —
    // otherwise a screen-reader user hears "Duplicate" twice with no way to tell
    // which job they are about to copy.
    expect(within(table).getByLabelText('Duplicate WO-1001')).toBeInTheDocument();
    expect(within(table).getByLabelText('Duplicate WO-1002')).toBeInTheDocument();
  });

  it('opens the duplicate dialog for the clicked row and copies nothing until confirmed', async () => {
    mockAuthUser = { id: 1, role: 'supervisor' as UserRole, is_superuser: false };
    renderWorkOrders();
    const table = await getDesktopTable();

    fireEvent.click(within(table).getByLabelText('Duplicate WO-1002'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Duplicate work order — WO-1002/)).toBeInTheDocument();
    // Server-gated write: nothing is sent, and nothing is added to this list,
    // until the planner confirms.
    expect(mockedApi.duplicateWorkOrder).not.toHaveBeenCalled();
  });

  it('mounts no duplicate dialog at all for a role that cannot duplicate', async () => {
    mockAuthUser = { id: 1, role: 'operator' as UserRole, is_superuser: false };
    renderWorkOrders();
    await getDesktopTable();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
  });

  it('navigates to the NEW work order via the envelope once the copy lands', async () => {
    // The response is an envelope, so the id lives at `result.work_order.id`.
    // Reading it off the envelope itself is what produced /work-orders/undefined.
    mockAuthUser = { id: 1, role: 'supervisor' as UserRole, is_superuser: false };
    mockedApi.duplicateWorkOrder.mockResolvedValue({
      work_order: {
        id: 501,
        version: 1,
        work_order_number: 'WO-20260805-007',
        part_id: 10,
        work_order_type: 'production',
        quantity_ordered: 50,
        quantity_complete: 0,
        quantity_scrapped: 0,
        status: 'draft',
        priority: 2,
        estimated_hours: 0,
        actual_hours: 0,
        created_at: '2026-08-05T12:00:00Z',
        updated_at: '2026-08-05T12:00:00Z',
        operations: [],
      },
      skipped_operations: [],
      skipped_material_allocations: [],
    });
    renderWorkOrders();
    const table = await getDesktopTable();

    fireEvent.click(within(table).getByLabelText('Duplicate WO-1001'));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByLabelText(/Quantity/i)).toBeEnabled());

    fireEvent.click(within(dialog).getByRole('button', { name: /Duplicat/i }));

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/work-orders/501'));
    expect(mockNavigate).not.toHaveBeenCalledWith('/work-orders/undefined');
  });

  it('a row click opens that work order (list click-through)', async () => {
    /**
     * The POSITIVE direction, which no test asserted before.
     *
     * DataTable.test.tsx proves the primitive fires onRowClick, and
     * WorkOrders.dueDateQuickEdit.test.tsx proves the due-date editor does NOT
     * navigate — but nothing asserted that an ordinary row click on THIS page still
     * reaches the detail route. Breaking it (dropping onRowClick, or widening an
     * in-row stopPropagation to a whole cell) would have shipped green.
     *
     * That gap mattered: the only thing covering it was a Playwright test that was
     * itself resolving against a loading skeleton, so it could not have caught a
     * real break either.
     */
    renderWorkOrders();
    const table = await getDesktopTable();

    // Scoped to the desktop <table> — the mobile card list also mounts in jsdom.
    const row = within(table).getByText('PN-BBB').closest('tr');
    expect(row).not.toBeNull();

    fireEvent.click(row!);
    expect(mockNavigate).toHaveBeenCalledWith('/work-orders/2');
  });
});

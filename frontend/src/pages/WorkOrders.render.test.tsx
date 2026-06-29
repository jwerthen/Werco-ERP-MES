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

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    // admin + superuser so delete controls render
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
    mockedApi.getWorkOrders.mockResolvedValue([draftWorkOrder, inProgressWorkOrder]);
    mockedApi.releaseWorkOrder.mockResolvedValue({});
    mockedApi.deleteWorkOrder.mockResolvedValue({});
    jest.spyOn(window, 'confirm').mockReturnValue(true);
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

  it('shows a Delete control on each row and wires it to deleteWorkOrder', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    const deleteButtons = within(table).getAllByTitle('Delete');
    expect(deleteButtons).toHaveLength(2);

    fireEvent.click(deleteButtons[1]);
    await waitFor(() => {
      expect(mockedApi.deleteWorkOrder).toHaveBeenCalledWith(2);
    });
  });
});

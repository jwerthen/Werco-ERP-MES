/**
 * Batch 10 — perceived performance: WorkOrders optimistic delete.
 *
 * The list delete is now optimistic (via useOptimisticMutation): the row is
 * dropped from the table synchronously, and only the rare server refusal rolls
 * it back. This file locks the compliance-relevant invariants of that change —
 * the user must never see a delete that did not actually happen, and a refusal
 * must restore the exact row in its original place:
 *
 *   1. SUCCESS — the row disappears immediately (while the API call is still in
 *      flight, before it resolves), and STAYS gone after it resolves. The page
 *      does NOT refetch the list to confirm (no extra api.getWorkOrders call);
 *      the optimistic removal is the source of truth on success.
 *   2. FAILURE — the row is RESTORED at its original position (a rejected delete
 *      of the first of two rows brings it back ahead of the second), and the
 *      server's verbatim `response.data.detail` surfaces as an error toast.
 *      Crucially, NO success toast is ever shown for the failed delete.
 *
 * WorkOrders mounts a websocket + visibility/focus/interval refresh loop; those
 * are mocked/inert so the only api.getWorkOrders calls are the explicit mount
 * load (and any the code under test makes), keeping the call-count assertions
 * deterministic. Assertions are scoped to the desktop <table> (the mobile card
 * list also mounts in jsdom).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { ToastProvider } from '../components/ui/Toast';

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

// Silence the realtime refresh loop so api.getWorkOrders is only called by the
// explicit mount load — nothing else perturbs the counts.
jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const firstWorkOrder = {
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

const secondWorkOrder = {
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

/**
 * Wait for the loaded desktop table (the WO number renders in both the desktop
 * <table> and a mobile card; pick the one inside a <table>, after the loading
 * skeleton clears).
 */
async function getDesktopTable(): Promise<HTMLElement> {
  const woLinks = await screen.findAllByRole('link', { name: 'WO-1001' });
  const tableLink = woLinks.find((el) => el.closest('table'));
  const table = tableLink?.closest('table');
  if (!table) throw new Error('expected a WO link inside the desktop <table>');
  return table as HTMLElement;
}

/** The WO-#### links of the desktop table's data rows, in DOM (visual) order. */
function tableRowOrder(table: HTMLElement): string[] {
  return within(table)
    .getAllByRole('row')
    .map((row) => within(row).queryByRole('link', { name: /^WO-/ })?.textContent ?? null)
    .filter((label): label is string => label != null);
}

function renderWorkOrders() {
  return render(
    <ToastProvider>
      <MemoryRouter>
        <WorkOrders />
      </MemoryRouter>
    </ToastProvider>
  );
}

describe('WorkOrders optimistic delete (Batch 10)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkOrders.mockResolvedValue([firstWorkOrder, secondWorkOrder]);
    mockedApi.releaseWorkOrder.mockResolvedValue({});
    // Confirm the destructive action so handleDelete proceeds to run().
    jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('drops the row immediately on a successful delete and never refetches to confirm', async () => {
    // A delete that stays in flight through the first assertion window: the row
    // must vanish from the OPTIMISTIC update alone, before the API resolves.
    let resolveDelete: (value: unknown) => void = () => {};
    mockedApi.deleteWorkOrder.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = resolve;
      }) as ReturnType<typeof api.deleteWorkOrder>
    );

    renderWorkOrders();
    const table = await getDesktopTable();
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1); // mount load only

    // Delete the first row.
    const deleteFirst = within(table).getAllByTitle('Delete')[0];
    fireEvent.click(deleteFirst);

    // Gone immediately, while the delete is still pending — the sibling stays.
    await waitFor(() =>
      expect(within(table).queryByRole('link', { name: 'WO-1001' })).not.toBeInTheDocument()
    );
    expect(mockedApi.deleteWorkOrder).toHaveBeenCalledWith(1);
    expect(within(table).getByRole('link', { name: 'WO-1002' })).toBeInTheDocument();

    // Resolve the server call; flush the trailing pending-state update.
    await act(async () => {
      resolveDelete({});
    });

    // Stays gone — the optimistic removal is kept, and the page does NOT refetch
    // the list to confirm the delete (no second api.getWorkOrders call).
    expect(within(table).queryByRole('link', { name: 'WO-1001' })).not.toBeInTheDocument();
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);
    // No error toast on the happy path.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('restores the row at its original position and toasts the verbatim detail when the delete fails', async () => {
    mockedApi.deleteWorkOrder.mockRejectedValueOnce({
      response: { data: { detail: 'Cannot delete a released work order' } },
    });

    renderWorkOrders();
    const table = await getDesktopTable();
    // Baseline order: WO-1001 then WO-1002.
    expect(tableRowOrder(table)).toEqual(['WO-1001', 'WO-1002']);

    // Delete the FIRST row (the harder restoration case — it must come back
    // ahead of WO-1002, not appended at the end).
    fireEvent.click(within(table).getAllByTitle('Delete')[0]);

    // Optimistically removed first…
    await waitFor(() =>
      expect(within(table).queryByRole('link', { name: 'WO-1001' })).not.toBeInTheDocument()
    );
    expect(mockedApi.deleteWorkOrder).toHaveBeenCalledWith(1);

    // …then the rejection rolls it back to its ORIGINAL index (before WO-1002).
    await waitFor(() =>
      expect(within(table).getByRole('link', { name: 'WO-1001' })).toBeInTheDocument()
    );
    expect(tableRowOrder(table)).toEqual(['WO-1001', 'WO-1002']);

    // The server's verbatim detail surfaces as an error toast. The Toast UI gives
    // error toasts role="alert" and success/info toasts role="status".
    const errorToast = await screen.findByText('Cannot delete a released work order');
    expect(errorToast.closest('[role="alert"]')).toBeInTheDocument();

    // And NO success toast was shown for the delete that did not happen — a
    // success toast would render with role="status"; none exists.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    // The row was never confirmed-removed by a refetch either.
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);
  });
});

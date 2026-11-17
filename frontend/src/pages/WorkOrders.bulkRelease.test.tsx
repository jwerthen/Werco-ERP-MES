/**
 * WorkOrders — bulk release of DRAFT work orders.
 *
 * `POST /work-orders/bulk-release` releases each id on its own and answers HTTP 200
 * even when some or all were refused, so the page has to read the counts. This file
 * pins the list-side contract:
 *
 *   - Only DRAFT rows get a checkbox, join select-all, or are sent.
 *   - Release is server-GATED, so it is NON-optimistic: a ConfirmDialog holds
 *     `pending` (double submit is inert) and the list moves only after the answer.
 *   - The pending dialog names the batch it SENT, frozen at the confirm click, so a
 *     background reload that empties the selection mid-request cannot re-word it to
 *     "Release 0"; an idle dialog whose selection is drained closes instead.
 *   - all released -> success toast; some refused -> warning naming each refused
 *     work order with the server's text; none released -> error toast; a thrown
 *     error -> error toast, dialog closes, selection kept.
 *   - The list is re-read afterwards and the selection cleared.
 *   - The grouped view shares ONE selection across its per-group tables.
 *   - No selection UI (and no row Release) for a role the endpoint would refuse.
 *
 * Fixture names are fictional (public repo).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { ToastProvider } from '../components/ui/Toast';
import { workOrderBrowseFixture } from '../testUtils/workOrderBrowseFixture';
import type { WorkOrderBulkReleaseResponse, WorkOrderSummary } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    browseWorkOrders: jest.fn(),
    releaseWorkOrder: jest.fn(),
    bulkReleaseWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 1, role: 'manager', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

function wo(
  id: number,
  status: WorkOrderSummary['status'],
  customer: string,
  priority = 3
): WorkOrderSummary {
  return {
    id,
    work_order_number: `WO-20${String(id).padStart(2, '0')}`,
    part_id: 100 + id,
    part_number: `PN-${id}`,
    part_name: `Panel ${id}`,
    part_type: 'manufactured',
    work_order_type: 'production',
    status,
    priority,
    quantity_ordered: 10,
    quantity_complete: 0,
    customer_name: customer,
  };
}

// Two fictional customers so the grouped view has two tables, each with drafts.
const NORTH = 'Northwind Fabrication';
const CONTOSO = 'Contoso Aero';
let rows: WorkOrderSummary[];

function resetRows() {
  rows = [
    wo(1, 'draft', NORTH, 1),
    wo(2, 'draft', NORTH, 2),
    wo(3, 'in_progress', NORTH, 3),
    wo(4, 'draft', CONTOSO, 4),
    wo(5, 'released', CONTOSO, 5),
  ];
}

function renderAt(url = '/work-orders') {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[url]}>
        <WorkOrders />
      </MemoryRouter>
    </ToastProvider>
  );
}

function okResponse(ids: number[]): WorkOrderBulkReleaseResponse {
  return {
    results: ids.map((id) => ({
      work_order_id: id,
      work_order_number: `WO-20${String(id).padStart(2, '0')}`,
      status: 'released',
      detail: null,
    })),
    released_count: ids.length,
    error_count: 0,
  };
}

const rowCheckbox = (number: string) => screen.queryByRole('checkbox', { name: `Select ${number}` });

async function waitForTable() {
  await screen.findByRole('checkbox', { name: 'Select WO-2001' });
}

function bulkBar() {
  return screen.getByTestId('bulk-release-bar');
}

async function openBulkDialog(count: number) {
  fireEvent.click(within(bulkBar()).getByRole('button', { name: `Release ${count}` }));
  return screen.findByRole('dialog');
}

beforeEach(() => {
  jest.resetAllMocks();
  mockUser = { id: 1, role: 'manager', is_superuser: false };
  window.innerWidth = 1440;
  resetRows();
  mockedApi.browseWorkOrders.mockImplementation(async (params) => workOrderBrowseFixture(rows, params));
});

describe('WorkOrders bulk release — flat view', () => {
  it('gives only draft rows a checkbox', async () => {
    renderAt();
    await waitForTable();
    expect(rowCheckbox('WO-2001')).toBeInTheDocument();
    expect(rowCheckbox('WO-2002')).toBeInTheDocument();
    expect(rowCheckbox('WO-2004')).toBeInTheDocument();
    expect(rowCheckbox('WO-2003')).not.toBeInTheDocument(); // in progress
    expect(rowCheckbox('WO-2005')).not.toBeInTheDocument(); // released
    // No bar until something is ticked.
    expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument();
  });

  it('select-all picks the drafts on the page only and says so', async () => {
    renderAt();
    await waitForTable();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all draft work orders on this page' }));
    expect(within(bulkBar()).getByText('3 selected')).toBeInTheDocument();
    expect(within(bulkBar()).getByText(/select all covers the work orders loaded on this page/i)).toBeInTheDocument();
    expect(within(bulkBar()).getByRole('button', { name: 'Release 3' })).toBeInTheDocument();
    ['WO-2001', 'WO-2002', 'WO-2004'].forEach((n) => expect(rowCheckbox(n)).toBeChecked());
  });

  it('confirms, blocks a double submit while pending, sends only draft ids, toasts, refreshes and clears', async () => {
    let resolveRelease: (value: WorkOrderBulkReleaseResponse) => void = () => {};
    mockedApi.bulkReleaseWorkOrders.mockReturnValue(
      new Promise<WorkOrderBulkReleaseResponse>((resolve) => {
        resolveRelease = resolve;
      })
    );
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    fireEvent.click(rowCheckbox('WO-2004')!);
    expect(within(bulkBar()).getByText('2 selected')).toBeInTheDocument();

    const dialog = await openBulkDialog(2);
    // Opening the dialog sends nothing.
    expect(mockedApi.bulkReleaseWorkOrders).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/Release 2 draft work orders to production\?/)).toBeInTheDocument();
    expect(within(dialog).getByText(/must have at least one operation/)).toBeInTheDocument();
    expect(within(dialog).getByText(/WO-2001, WO-2004/)).toBeInTheDocument();

    const confirm = within(dialog).getByRole('button', { name: 'Release 2' });
    fireEvent.click(confirm);
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledTimes(1);
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledWith([1, 4]);

    // Pending: confirm disabled (second click inert), cancel disabled, still open.
    await waitFor(() => expect(confirm).toBeDisabled());
    fireEvent.click(confirm);
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Non-optimistic: nothing on the list moved yet.
    expect(rowCheckbox('WO-2001')).toBeChecked();

    const loadsBefore = mockedApi.browseWorkOrders.mock.calls.length;
    rows = rows.map((row) => (row.id === 1 || row.id === 4 ? { ...row, status: 'released' } : row));
    await act(async () => {
      resolveRelease(okResponse([1, 4]));
    });

    const toast = await screen.findByText('Released 2 work orders');
    expect(toast.closest('[role="status"]')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // The list is re-read, and the released rows lose their checkbox.
    await waitFor(() => expect(mockedApi.browseWorkOrders.mock.calls.length).toBeGreaterThan(loadsBefore));
    await waitFor(() => expect(rowCheckbox('WO-2001')).not.toBeInTheDocument());
    expect(rowCheckbox('WO-2002')).not.toBeChecked();
    expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument();
  });

  it('keeps naming the batch it sent while a reload empties the selection mid-request', async () => {
    let resolveRelease: (value: WorkOrderBulkReleaseResponse) => void = () => {};
    mockedApi.bulkReleaseWorkOrders.mockReturnValue(
      new Promise<WorkOrderBulkReleaseResponse>((resolve) => {
        resolveRelease = resolve;
      })
    );
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    fireEvent.click(rowCheckbox('WO-2004')!);
    const dialog = await openBulkDialog(2);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 2' }));
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledWith([1, 4]);

    // The server has already released both; a background reload (here a focus refresh)
    // lands BEFORE its answer and prunes the live selection to nothing.
    rows = rows.map((row) => (row.id === 1 || row.id === 4 ? { ...row, status: 'released' } : row));
    const loadsBefore = mockedApi.browseWorkOrders.mock.calls.length;
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(mockedApi.browseWorkOrders.mock.calls.length).toBeGreaterThan(loadsBefore));
    await waitFor(() => expect(rowCheckbox('WO-2001')).not.toBeInTheDocument());
    expect(rowCheckbox('WO-2004')).not.toBeInTheDocument();
    // The live selection really is empty: the bar it drives is gone...
    expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument();

    // ...yet the pending dialog still names the batch on the wire, not "Release 0".
    const pending = screen.getByRole('dialog');
    expect(within(pending).getByText(/Release 2 draft work orders to production\?/)).toBeInTheDocument();
    expect(within(pending).getByText(/WO-2001, WO-2004/)).toBeInTheDocument();
    const confirm = within(pending).getByRole('button', { name: /Release 2$/ });
    expect(confirm).toBeDisabled();
    expect(within(pending).queryByText(/Release 0/)).not.toBeInTheDocument();
    expect(within(pending).getByRole('button', { name: 'Cancel' })).toBeDisabled();

    await act(async () => {
      resolveRelease(okResponse([1, 4]));
    });
    await screen.findByText('Released 2 work orders');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledTimes(1);
  });

  it('closes an idle dialog when a reload drains the selection, sending nothing', async () => {
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    await openBulkDialog(1);

    // Someone else releases the only ticked draft while the dialog sits unconfirmed.
    rows = rows.map((row) => (row.id === 1 ? { ...row, status: 'released' } : row));
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.bulkReleaseWorkOrders).not.toHaveBeenCalled();
  });

  it('names each refused work order with the server detail in a warning on a partial release', async () => {
    mockedApi.bulkReleaseWorkOrders.mockResolvedValue({
      results: [
        { work_order_id: 1, work_order_number: 'WO-2001', status: 'released', detail: null },
        {
          work_order_id: 2,
          work_order_number: 'WO-2002',
          status: 'error',
          detail: 'Work order must have at least one operation',
        },
        {
          work_order_id: 4,
          work_order_number: null,
          status: 'error',
          detail: 'Only draft work orders can be released',
        },
      ],
      released_count: 1,
      error_count: 2,
    });
    renderAt();
    await waitForTable();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all draft work orders on this page' }));
    const dialog = await openBulkDialog(3);
    const loadsBefore = mockedApi.browseWorkOrders.mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 3' }));

    const toast = await screen.findByText(/Released 1 work order; 2 work orders not released/);
    const alert = toast.closest('[role="alert"]') as HTMLElement;
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveClass('bg-amber-600'); // the warning variant
    expect(alert).toHaveTextContent('WO-2002: Work order must have at least one operation');
    // A null work_order_number falls back to the number the page already showed.
    expect(alert).toHaveTextContent('WO-2004: Only draft work orders can be released');

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(mockedApi.browseWorkOrders.mock.calls.length).toBeGreaterThan(loadsBefore));
    // Selection cleared even though two are still drafts.
    expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument();
    expect(rowCheckbox('WO-2002')).not.toBeChecked();
  });

  it('reports an all-refused batch as an error with each detail', async () => {
    mockedApi.bulkReleaseWorkOrders.mockResolvedValue({
      results: [
        {
          work_order_id: 2,
          work_order_number: 'WO-2002',
          status: 'error',
          detail: 'Work order must have at least one operation',
        },
      ],
      released_count: 0,
      error_count: 1,
    });
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2002')!);
    const dialog = await openBulkDialog(1);
    expect(within(dialog).getByText(/Release 1 draft work order to production\?/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 1' }));

    const toast = await screen.findByText(/No work orders released/);
    const alert = toast.closest('[role="alert"]') as HTMLElement;
    expect(alert).toHaveClass('bg-red-600');
    expect(alert).toHaveTextContent('WO-2002: Work order must have at least one operation');
    expect(screen.queryByText(/^Released \d+ work order/)).not.toBeInTheDocument();
  });

  it('on a thrown error: error toast with the server detail, dialog closes, selection kept', async () => {
    mockedApi.bulkReleaseWorkOrders.mockRejectedValue({
      response: { status: 403, data: { detail: 'Insufficient permissions' } },
    });
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    fireEvent.click(rowCheckbox('WO-2002')!);
    const dialog = await openBulkDialog(2);
    const loadsBefore = mockedApi.browseWorkOrders.mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 2' }));

    const toast = await screen.findByText('Insufficient permissions');
    expect(toast.closest('[role="alert"]')).toHaveClass('bg-red-600');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // Kept so a retry is one click; no refresh was needed.
    expect(within(bulkBar()).getByText('2 selected')).toBeInTheDocument();
    expect(rowCheckbox('WO-2001')).toBeChecked();
    expect(mockedApi.browseWorkOrders.mock.calls.length).toBe(loadsBefore);
    // The selection-count live region is NOT a role="status": that role is the toast
    // stack's, and its absence is how "no success toast" is asserted elsewhere.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('drops a ticked work order that is no longer a draft after a reload', async () => {
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    fireEvent.click(rowCheckbox('WO-2002')!);
    expect(within(bulkBar()).getByText('2 selected')).toBeInTheDocument();

    // Someone else releases WO-2001; a focus refresh brings it back released.
    rows = rows.map((row) => (row.id === 1 ? { ...row, status: 'released' } : row));
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(rowCheckbox('WO-2001')).not.toBeInTheDocument());
    expect(within(bulkBar()).getByText('1 selected')).toBeInTheDocument();

    mockedApi.bulkReleaseWorkOrders.mockResolvedValue(okResponse([2]));
    const dialog = await openBulkDialog(1);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 1' }));
    await screen.findByText('Released 1 work order');
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledWith([2]);
  });

  it('clears the selection when a filter changes', async () => {
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2001')!);
    expect(bulkBar()).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'draft' } });
    await waitFor(() => expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument());
    await waitForTable();
    expect(rowCheckbox('WO-2001')).not.toBeChecked();
  });

  it('cancel closes the dialog without a request and keeps the selection', async () => {
    renderAt();
    await waitForTable();
    fireEvent.click(rowCheckbox('WO-2004')!);
    const dialog = await openBulkDialog(1);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.bulkReleaseWorkOrders).not.toHaveBeenCalled();
    expect(rowCheckbox('WO-2004')).toBeChecked();
  });

  it('lists at most ten work order numbers in the dialog', async () => {
    rows = Array.from({ length: 13 }, (_, i) => wo(i + 1, 'draft', NORTH, 1));
    renderAt();
    await waitForTable();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all draft work orders on this page' }));
    const dialog = await openBulkDialog(13);
    const listed = within(dialog).getByText(/WO-2001, /);
    expect(listed.textContent).toContain('WO-2010, and 3 more');
    expect(listed.textContent).not.toContain('WO-2011');
  });
});

describe('WorkOrders bulk release — grouped view', () => {
  it('shares one selection across group tables and sends every ticked draft', async () => {
    mockedApi.bulkReleaseWorkOrders.mockResolvedValue(okResponse([4, 1, 2]));
    renderAt('/work-orders?group=customer');
    await waitForTable();
    // Each group table has its own select-all scoped to its drafts.
    fireEvent.click(rowCheckbox('WO-2004')!);
    fireEvent.click(screen.getByRole('checkbox', { name: `Select all draft work orders in ${NORTH}` }));
    // The NORTH select-all did not drop the CONTOSO tick.
    expect(rowCheckbox('WO-2004')).toBeChecked();
    expect(rowCheckbox('WO-2001')).toBeChecked();
    expect(rowCheckbox('WO-2002')).toBeChecked();
    expect(rowCheckbox('WO-2003')).not.toBeInTheDocument();
    // One bar for the page, not one per group.
    expect(screen.getAllByTestId('bulk-release-bar')).toHaveLength(1);
    expect(within(bulkBar()).getByText('3 selected')).toBeInTheDocument();

    // Unticking via the NORTH select-all leaves the CONTOSO tick alone.
    fireEvent.click(screen.getByRole('checkbox', { name: `Select all draft work orders in ${NORTH}` }));
    expect(rowCheckbox('WO-2001')).not.toBeChecked();
    expect(rowCheckbox('WO-2004')).toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: `Select all draft work orders in ${NORTH}` }));

    const dialog = await openBulkDialog(3);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release 3' }));
    await screen.findByText('Released 3 work orders');
    // Request order follows the loaded list; the set is what matters.
    expect(mockedApi.bulkReleaseWorkOrders).toHaveBeenCalledTimes(1);
    expect([...mockedApi.bulkReleaseWorkOrders.mock.calls[0][0]].sort()).toEqual([1, 2, 4]);
  });
});

describe('WorkOrders bulk release — permissions', () => {
  it.each(['operator', 'quality', 'viewer', 'shipping'])(
    'offers %s no selection and no row Release',
    async (role) => {
      mockUser = { id: 2, role, is_superuser: false };
      renderAt();
      await screen.findAllByRole('link', { name: 'WO-2001' });
      expect(screen.queryAllByRole('checkbox', { name: /^Select / })).toHaveLength(0);
      expect(screen.queryByRole('button', { name: 'Release WO-2001' })).not.toBeInTheDocument();
      expect(screen.queryByTestId('bulk-release-bar')).not.toBeInTheDocument();
    }
  );

  it.each(['admin', 'manager', 'supervisor'])('offers %s the selection and row Release', async (role) => {
    mockUser = { id: 3, role, is_superuser: false };
    renderAt();
    await waitForTable();
    expect(screen.getByRole('button', { name: 'Release WO-2001' })).toBeInTheDocument();
  });

  it('renders no selection column when the page holds no draft', async () => {
    rows = rows.filter((row) => row.status !== 'draft');
    renderAt();
    await screen.findAllByRole('link', { name: 'WO-2003' });
    expect(screen.queryAllByRole('checkbox', { name: /^Select / })).toHaveLength(0);
  });
});

/**
 * Inline due-date quick edit on the work-order list.
 *
 * A due date is a promise date feeding OTD (`must_ship_by || due_date`), so the
 * things worth locking here are the ones that stop a reschedule being lost or
 * fabricated:
 *
 *   1. The PUT carries the row's `version` — the optimistic lock. A list row that
 *      shipped without one would 409 on every edit.
 *   2. NON-optimistic: the cell keeps showing the SERVER's date until the refetch
 *      confirms the new one, and a refused save shows the server's verbatim
 *      `detail` and never a success toast.
 *   3. The lost-update guard: if the row's due date changed underneath the open
 *      editor (this list refetches on a timer, on focus, and on every broadcast,
 *      so `version` silently refreshes and its 409 never fires), Save must ASK
 *      before overwriting — and only write once confirmed.
 *   4. Role gating: the pencil matches the endpoint's ADMIN/MANAGER/SUPERVISOR
 *      gate, so a hidden control and a refused call agree.
 *   5. A finished job (complete/closed/cancelled) shows no pencil — editing a
 *      recorded delivery result is a detail-page decision, not a row-level one.
 *
 * Assertions are scoped to the desktop <table>; the mobile card list also mounts
 * in jsdom and deliberately carries no editor.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { ToastProvider } from '../components/ui/Toast';
import { formatCentralDate } from '../utils/centralTime';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    updateWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
  },
}));

let mockRole = 'manager';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: mockRole, is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const openWorkOrder = {
  id: 1,
  work_order_number: 'WO-1001',
  version: 7,
  part_id: 10,
  work_order_type: 'production',
  part_number: 'PN-AAA',
  part_name: 'Bracket Assembly',
  part_type: 'manufactured',
  status: 'in_progress' as const,
  priority: 2,
  quantity_ordered: 50,
  quantity_complete: 0,
  due_date: '2099-03-04',
  customer_name: 'Acme Aero',
};

const finishedWorkOrder = {
  ...openWorkOrder,
  id: 2,
  work_order_number: 'WO-1002',
  version: 3,
  status: 'complete' as const,
  due_date: '2099-03-09',
  // A DIFFERENT customer, so the client-side customer filter can exclude WO-1001
  // while still rendering a list — that is the scenario the cleanup effect covers.
  customer_name: 'Beta Defense',
};

async function getDesktopTable(): Promise<HTMLElement> {
  const links = await screen.findAllByRole('link', { name: 'WO-1001' });
  const table = links.find((el) => el.closest('table'))?.closest('table');
  if (!table) throw new Error('expected a WO link inside the desktop <table>');
  return table as HTMLElement;
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

/** Open the inline editor on WO-1001 and return the desktop table. */
async function openEditor(): Promise<HTMLElement> {
  const table = await getDesktopTable();
  fireEvent.click(within(table).getByRole('button', { name: 'Edit due date for WO-1001' }));
  return table;
}

describe('WorkOrders inline due-date quick edit', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockNavigate.mockClear();
    mockRole = 'manager';
    mockedApi.getWorkOrders.mockResolvedValue([openWorkOrder, finishedWorkOrder]);
    mockedApi.updateWorkOrder.mockResolvedValue({});
  });

  it('saves the new date with the row version and refetches (non-optimistic)', async () => {
    renderWorkOrders();
    const table = await openEditor();

    const input = within(table).getByLabelText('Due date for WO-1001') as HTMLInputElement;
    expect(input.value).toBe('2099-03-04');

    fireEvent.change(input, { target: { value: '2099-03-11' } });
    fireEvent.click(within(table).getByRole('button', { name: 'Save due date for WO-1001' }));

    await waitFor(() => expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1));
    // The version is what makes the optimistic lock real.
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(1, {
      due_date: '2099-03-11',
      version: 7,
    });

    // Non-optimistic: the change is confirmed by a refetch, not painted locally.
    await waitFor(() => expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/WO-1001 due/)).toBeInTheDocument();
  });

  it('clears the due date when the field is emptied', async () => {
    renderWorkOrders();
    const table = await openEditor();

    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), { target: { value: '' } });
    fireEvent.click(within(table).getByRole('button', { name: 'Save due date for WO-1001' }));

    await waitFor(() =>
      expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(1, { due_date: null, version: 7 })
    );
    expect(await screen.findByText(/Due date cleared on WO-1001/)).toBeInTheDocument();
  });

  it('surfaces the server detail on a refused save and shows no success toast', async () => {
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Work order was modified by someone else.' } },
    });

    renderWorkOrders();
    const table = await openEditor();

    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), {
      target: { value: '2099-03-11' },
    });
    fireEvent.click(within(table).getByRole('button', { name: 'Save due date for WO-1001' }));

    expect(await screen.findByText('Work order was modified by someone else.')).toBeInTheDocument();
    expect(screen.queryByText(/WO-1001 due/)).not.toBeInTheDocument();
    // The editor stays open holding the draft so a retry costs no re-typing.
    expect(within(await getDesktopTable()).getByLabelText('Due date for WO-1001')).toBeInTheDocument();
  });

  it('asks before overwriting a due date that changed under the open editor', async () => {
    renderWorkOrders();
    const table = await openEditor();

    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), {
      target: { value: '2099-03-11' },
    });

    // Someone else reschedules the job; the list's refresh loop picks it up while
    // the editor is open — which also refreshes `version`, so the server-side lock
    // would NOT catch this.
    mockedApi.getWorkOrders.mockResolvedValue([
      { ...openWorkOrder, version: 8, due_date: '2099-04-01' },
      finishedWorkOrder,
    ]);
    fireEvent.focus(window);
    await waitFor(() => expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2));

    fireEvent.click(
      within(await getDesktopTable()).getByRole('button', { name: 'Save due date for WO-1001' })
    );

    // Refused pending a decision — nothing written yet.
    expect(await screen.findByText('Due date changed by someone else')).toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));

    // Confirmed: the draft wins, carrying the REFRESHED version.
    await waitFor(() =>
      expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(1, {
        due_date: '2099-03-11',
        version: 8,
      })
    );
  });

  it('keeps the concurrent date when the conflict is declined', async () => {
    renderWorkOrders();
    const table = await openEditor();

    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), {
      target: { value: '2099-03-11' },
    });

    mockedApi.getWorkOrders.mockResolvedValue([
      { ...openWorkOrder, version: 8, due_date: '2099-04-01' },
      finishedWorkOrder,
    ]);
    fireEvent.focus(window);
    await waitFor(() => expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2));

    fireEvent.click(
      within(await getDesktopTable()).getByRole('button', { name: 'Save due date for WO-1001' })
    );
    expect(await screen.findByText('Due date changed by someone else')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Keep theirs' }));

    await waitFor(() =>
      expect(screen.queryByText('Due date changed by someone else')).not.toBeInTheDocument()
    );
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('Escape closes the editor without writing', async () => {
    renderWorkOrders();
    const table = await openEditor();

    const input = within(table).getByLabelText('Due date for WO-1001');
    fireEvent.change(input, { target: { value: '2099-03-11' } });
    fireEvent.keyDown(input, { key: 'Escape' });

    await waitFor(() =>
      expect(within(table).queryByLabelText('Due date for WO-1001')).not.toBeInTheDocument()
    );
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('shows no pencil on a finished work order', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    expect(within(table).getByRole('button', { name: 'Edit due date for WO-1001' })).toBeInTheDocument();
    expect(
      within(table).queryByRole('button', { name: 'Edit due date for WO-1002' })
    ).not.toBeInTheDocument();
  });

  it('shows no pencil to a role the endpoint would refuse', async () => {
    mockRole = 'operator';
    renderWorkOrders();
    const table = await getDesktopTable();

    expect(
      within(table).queryByRole('button', { name: 'Edit due date for WO-1001' })
    ).not.toBeInTheDocument();
  });
  // --- Guards that a passing suite would otherwise not cover ----------------
  // Each of these was verified to FAIL when its guard is removed; without them
  // the stopPropagation calls and the cleanup effect could be deleted silently.

  it('does not navigate to the detail page while interacting with the editor', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    // Opening the editor must not trigger the row's click-through.
    fireEvent.click(within(table).getByRole('button', { name: 'Edit due date for WO-1001' }));
    expect(mockNavigate).not.toHaveBeenCalled();

    // Nor must picking a date, saving, or cancelling.
    const input = within(table).getByLabelText('Due date for WO-1001');
    fireEvent.click(input);
    fireEvent.change(input, { target: { value: '2099-03-11' } });
    expect(mockNavigate).not.toHaveBeenCalled();

    fireEvent.click(within(table).getByRole('button', { name: 'Cancel due date edit for WO-1001' }));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('closes the editor and warns when a client-side filter drops the edited row', async () => {
    renderWorkOrders();
    const table = await openEditor();
    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), {
      target: { value: '2099-03-11' },
    });

    // The customer filter is CLIENT-side and triggers no refetch — the row simply
    // stops being rendered. Watching the unfiltered list missed this entirely.
    fireEvent.change(screen.getByLabelText('Customer filter'), { target: { value: 'Beta Defense' } });

    await waitFor(() =>
      expect(screen.queryAllByRole('link', { name: 'WO-1001' })).toHaveLength(0)
    );
    expect(screen.queryByLabelText('Due date for WO-1001')).not.toBeInTheDocument();
    expect(await screen.findByText(/left the list/)).toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('dismisses a standing conflict dialog when the row leaves the list', async () => {
    renderWorkOrders();
    const table = await openEditor();
    fireEvent.change(within(table).getByLabelText('Due date for WO-1001'), {
      target: { value: '2099-03-11' },
    });

    mockedApi.getWorkOrders.mockResolvedValue([
      { ...openWorkOrder, version: 8, due_date: '2099-04-01' },
      finishedWorkOrder,
    ]);
    fireEvent.focus(window);
    await waitFor(() => expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2));
    fireEvent.click(
      within(await getDesktopTable()).getByRole('button', { name: 'Save due date for WO-1001' })
    );
    expect(await screen.findByText('Due date changed by someone else')).toBeInTheDocument();

    // "Replace with mine" must not stay clickable for a row the list no longer shows.
    fireEvent.change(screen.getByLabelText('Customer filter'), { target: { value: 'Beta Defense' } });

    await waitFor(() =>
      expect(screen.queryByText('Due date changed by someone else')).not.toBeInTheDocument()
    );
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('Enter does not clear a due date emptied mid-retype', async () => {
    renderWorkOrders();
    const table = await openEditor();

    // A native date input reports '' while a complete value is being retyped.
    const input = within(table).getByLabelText('Due date for WO-1001');
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    // The explicit checkmark still clears, deliberately.
    fireEvent.click(within(table).getByRole('button', { name: 'Save due date for WO-1001' }));
    await waitFor(() =>
      expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(1, { due_date: null, version: 7 })
    );
  });

  it('Enter still saves a real date', async () => {
    renderWorkOrders();
    const table = await openEditor();

    const input = within(table).getByLabelText('Due date for WO-1001');
    fireEvent.change(input, { target: { value: '2099-03-11' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() =>
      expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(1, {
        due_date: '2099-03-11',
        version: 7,
      })
    );
  });

  it('a click in the due-date cell still opens the row; only the pencil is inert', async () => {
    /**
     * The INVERSE of the guard above, and the half a passing suite was missing.
     *
     * The swallow must be scoped to the PENCIL, not to the cell or the column.
     * Widening it — a stopPropagation on the wrapper span, or on the column's
     * render — would leave the editor working perfectly while silently killing
     * click-through on one of the widest cells in the row. Every existing test
     * would still pass.
     */
    renderWorkOrders();
    const table = await getDesktopTable();

    const pencil = within(table).getByRole('button', { name: 'Edit due date for WO-1001' });
    const cell = pencil.closest('td');
    expect(cell).not.toBeNull();

    // Click the innermost CONTENT — the rendered date — not the <td>. That
    // distinction is the whole test: a real click lands on the deepest element and
    // BUBBLES up through the cell's wrapper span to the row. Dispatching straight
    // at the <td> skips every child, so it cannot observe a stopPropagation added
    // to one of them — an earlier version of this test did exactly that and passed
    // against the widened swallow it was written to catch.
    const dateText = within(cell!).getByText(formatCentralDate('2099-03-04'));
    fireEvent.click(dateText);
    expect(mockNavigate).toHaveBeenCalledWith('/work-orders/1');

    // The button itself does not.
    mockNavigate.mockClear();
    fireEvent.click(pencil);
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

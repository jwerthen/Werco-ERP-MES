/**
 * WorkOrderDetail — the inline "Customer PO" editor.
 *
 * A customer PO routinely arrives AFTER the work order was raised (work started on a
 * verbal or a blanket order), so the field has to be editable rather than create-only.
 * It is the fourth editor sharing the page's one `fieldConflict` lost-update dialog,
 * guarded on the FIELD's server value for the same reason as unit # and the due date:
 * this page refetches on every work_order broadcast, so `version` alone would let a
 * concurrent edit be overwritten with a clean 200.
 *
 * Pinned here: the save sends `{ customer_po, version }` and nothing else; an emptied
 * field sends `null`; a concurrent PO change raises the dialog titled for THIS editor;
 * the pencil is offered on a COMPLETE work order (no status gate server-side) and
 * hidden from a role that cannot edit; and the PO's "Track all work orders" link
 * survives as its own link now that the tile holds a button.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { useWebSocket } from '../hooks/useWebSocket';
import { WorkOrder } from '../types';

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
    // CommentsPanel (mounted on the page) loads its own thread and watch state.
    listComments: jest.fn().mockResolvedValue([]),
    getWatchState: jest.fn().mockResolvedValue({ watching: false, watcher_count: 0 }),
    getMaterialAllocations: jest.fn(),
    getWorkCenters: jest.fn(),
    updateWorkOrder: jest.fn(),
    updateOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
    getCustomerPOSuggestions: jest.fn(),
  },
}));

// Mutable so the role-gate test can demote the viewer (jest allows lazily referenced
// `mock*` variables inside the hoisted factory).
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

const PO = 'PO-1001'; // what the server holds when the editor opens
const MY_DRAFT = 'PO-1002'; // what this planner types
const THEIR_PO = 'PO-1099'; // what a concurrent planner sets meanwhile

function makeWorkOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-0042',
    part_id: 100,
    work_order_type: 'production',
    quantity_ordered: 1,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 8,
    actual_hours: 0,
    customer_name: 'A&B Aero',
    customer_po: PO,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    operations: [],
    ...overrides,
  };
}

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

/**
 * Lands a concurrent server-side change the way production does: through the page's own
 * `onMessage` handler (captured off the mocked useWebSocket), which debounces 500ms in
 * `scheduleRealtimeRefresh` before refetching — hence the fake timers.
 *
 * `next` is the WHOLE work order the server now holds, not a delta, so a caller whose
 * guard is keyed on a field must RESTATE that field when it is meant to be unchanged.
 */
async function pushConcurrentChange(next: Partial<WorkOrder>) {
  mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder(next));
  const options = jest.mocked(useWebSocket).mock.calls.at(-1)?.[0];
  expect(options?.onMessage).toBeDefined();

  await act(async () => {
    options?.onMessage?.({ type: 'work_order_update', data: { work_order_id: 42 } }, new MessageEvent('message'));
    await jest.advanceTimersByTimeAsync(500);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  mockUser = { id: 1, role: 'admin', is_superuser: false };

  mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder());
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.updateWorkOrder.mockResolvedValue({});
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({ items: [] } as never);
});

afterEach(() => {
  jest.useRealTimers();
});

async function openCustomerPoEditor(name: string = 'Edit customer PO') {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name }));
  return screen.getByLabelText('Customer PO') as HTMLInputElement;
}

async function clickSave() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Save customer PO' }));
  });
}

describe('WorkOrderDetail inline customer-PO edit', () => {
  it('opens seeded with the server value and saves { customer_po, version }', async () => {
    const input = await openCustomerPoEditor();
    expect(input).toHaveValue(PO);

    fireEvent.change(input, { target: { value: `  ${MY_DRAFT} ` } });
    await clickSave();

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: MY_DRAFT, version: 3 });
    expect(await screen.findByText(`Customer PO set to ${MY_DRAFT}`)).toBeInTheDocument();
  });

  it('saves on Enter and cancels on Escape', async () => {
    let input = await openCustomerPoEditor();
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: MY_DRAFT } });
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' });
    });
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: MY_DRAFT, version: 3 });

    mockedApi.updateWorkOrder.mockClear();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit customer PO' }));
    input = screen.getByLabelText('Customer PO') as HTMLInputElement;
    // First Escape closes the (focus-opened) suggestion list; the next one cancels.
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.keyDown(screen.getByLabelText('Customer PO'), { key: 'Escape' });
    expect(screen.queryByLabelText('Customer PO')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('offers "Add customer PO" on a work order that has none', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ customer_po: undefined }));
    const input = await openCustomerPoEditor('Add customer PO');
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: MY_DRAFT } });
    await clickSave();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: MY_DRAFT, version: 3 });
  });

  it('clears the PO with null (not "") when the field is emptied', async () => {
    const input = await openCustomerPoEditor();
    fireEvent.change(input, { target: { value: '   ' } });
    await clickSave();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: null, version: 3 });
    expect(await screen.findByText('Customer PO cleared')).toBeInTheDocument();
  });

  it('refuses the save and opens the dialog when the PO changed under the editor', async () => {
    const input = await openCustomerPoEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, customer_po: THEIR_PO });
    await clickSave();

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Customer PO changed by someone else')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: MY_DRAFT, version: 7 });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('does not fire on an unrelated concurrent change (version bumped, PO untouched)', async () => {
    const input = await openCustomerPoEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 9, customer_po: PO, status: 'in_progress' });
    await clickSave();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_po: MY_DRAFT, version: 9 });
  });

  it('surfaces the server detail verbatim and keeps the editor open on a refusal', async () => {
    const input = await openCustomerPoEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Work order was modified by someone else.' } },
    });

    await clickSave();

    expect(await screen.findByText('Work order was modified by someone else.')).toBeInTheDocument();
    expect(screen.getByLabelText('Customer PO')).toHaveValue(MY_DRAFT);
  });

  it('stays editable on a completed work order', async () => {
    // The server applies no status gate to customer_po, so the pencil must not either:
    // paperwork for a shipped job is exactly when a late PO gets entered.
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ status: 'complete' }));
    renderDetail();
    expect(await screen.findByRole('button', { name: 'Edit customer PO' })).toBeInTheDocument();
  });

  it('hides the pencil from a role that cannot edit the work order', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    expect(await screen.findAllByText('WO-0042')).not.toHaveLength(0);
    expect(screen.queryByRole('button', { name: /customer PO/i })).not.toBeInTheDocument();
    expect(screen.getByText(PO)).toBeInTheDocument();
  });

  it('links "Track all work orders" to the exact customer and PO', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ customer_po: 'PO/100 #2' }));
    renderDetail();
    const link = await screen.findByRole('link', { name: /Track all work orders/ });
    const url = new URL(link.getAttribute('href')!, 'http://localhost');
    expect(url.pathname).toBe('/work-orders/customer-pos');
    expect(url.searchParams.get('customer_name')).toBe('A&B Aero');
    expect(url.searchParams.get('customer_po')).toBe('PO/100 #2');
  });
});

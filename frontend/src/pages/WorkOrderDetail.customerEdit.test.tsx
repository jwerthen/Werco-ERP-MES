/**
 * WorkOrderDetail — the inline "Customer" editor.
 *
 * The customer is often settled after the work order is raised, and the Customer POs
 * page groups work orders by (customer, PO) — so a job raised with a PO but no
 * customer sits in its own "No customer assigned" row until its customer is set.
 *
 * Pinned here: the picker lists the customer master (never free text); the save sends
 * `{ customer_name, version }` only; "No customer" clears to null; the current value
 * stays selectable when it is not in the master list; a concurrent change raises the
 * shared dialog titled for THIS editor; the pencil shows at any status and is hidden
 * from a role that cannot edit.
 */

import React from 'react';
import { act, render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { useWebSocket } from '../hooks/useWebSocket';
import { WorkOrder } from '../types';
import { typeInComboBox } from '../test-utils/comboBox';

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
    getCustomerNames: jest.fn(),
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

const PO = 'PO-2001';
const CUSTOMER = 'A&B Aero';

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
    customer_name: CUSTOMER,
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
  mockedApi.getCustomerNames.mockResolvedValue([
    { id: 1, name: 'A&B Aero' },
    { id: 2, name: 'Northwind Fab' },
  ]);
});

afterEach(() => {
  jest.useRealTimers();
});

async function openCustomerEditor(name: string = 'Edit customer') {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name }));
  return (await screen.findByRole('combobox', { name: 'Customer' })) as HTMLInputElement;
}

async function pick(input: HTMLElement, query: string, option: string) {
  const list = typeInComboBox(input, query);
  fireEvent.click(within(list).getByRole('option', { name: option }));
}

async function clickSave() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Save customer' }));
  });
}

describe('WorkOrderDetail inline customer edit', () => {
  it('picks from the customer master and saves { customer_name, version }', async () => {
    const input = await openCustomerEditor();
    expect(mockedApi.getCustomerNames).toHaveBeenCalledTimes(1);

    await pick(input, 'north', 'Northwind Fab');
    await clickSave();

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_name: 'Northwind Fab', version: 3 });
    expect(await screen.findByText('Customer set to Northwind Fab')).toBeInTheDocument();
  });

  it('offers "Add customer" on a work order that has none', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ customer_name: undefined }));
    const input = await openCustomerEditor('Add customer');
    await pick(input, 'north', 'Northwind Fab');
    await clickSave();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_name: 'Northwind Fab', version: 3 });
  });

  it('clears the customer with null when "No customer" is picked', async () => {
    const input = await openCustomerEditor();
    await pick(input, '', 'No customer');
    await clickSave();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_name: null, version: 3 });
    expect(await screen.findByText('Customer cleared')).toBeInTheDocument();
  });

  it('does not accept a name that is not in the customer master', async () => {
    const input = await openCustomerEditor();
    const list = typeInComboBox(input, 'Nobody Inc');
    expect(within(list).queryAllByRole('option')).toHaveLength(0);
    expect(within(list).getByText(/Add it on the Customers page first/)).toBeInTheDocument();
  });

  it('keeps a legacy customer name selectable when it is not in the master list', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ customer_name: 'Old Free Text Co' }));
    const input = await openCustomerEditor();
    const list = typeInComboBox(input, 'old');
    expect(within(list).getByRole('option', { name: 'Old Free Text Co' })).toBeInTheDocument();
  });

  it('refuses the save and opens the dialog when the customer changed under the editor', async () => {
    const input = await openCustomerEditor();
    await pick(input, 'north', 'Northwind Fab');

    await pushConcurrentChange({ version: 7, customer_name: 'TechCo' });
    await clickSave();

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Customer changed by someone else')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, { customer_name: 'Northwind Fab', version: 7 });
  });

  it('shows an error instead of the picker when the customer list cannot load', async () => {
    mockedApi.getCustomerNames.mockRejectedValue(new Error('boom'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit customer' }));
    expect(await screen.findByText('Could not load customers.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save customer' })).toBeDisabled();
  });

  it('stays editable on a completed work order', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ status: 'complete' }));
    renderDetail();
    expect(await screen.findByRole('button', { name: 'Edit customer' })).toBeInTheDocument();
  });

  it('hides the pencil from a role that cannot edit the work order', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();
    expect(await screen.findAllByText('WO-0042')).not.toHaveLength(0);
    expect(screen.queryByRole('button', { name: /^(Edit|Add) customer$/ })).not.toBeInTheDocument();
    expect(screen.getByText(CUSTOMER)).toBeInTheDocument();
  });
});

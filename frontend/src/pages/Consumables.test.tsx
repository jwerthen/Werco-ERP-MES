import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, MemoryRouter, Outlet, RouterProvider } from 'react-router-dom';
import Consumables from './Consumables';
import consumables from '../services/consumables';
import { openComboBox, selectComboBoxOption } from '../test-utils/comboBox';
import { UnsavedChangesProvider } from '../context/UnsavedChangesContext';

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 7, company_id: 1, role: 'manager', first_name: 'Alex', last_name: 'Rivera' } }),
}));
jest.mock('../services/consumables', () => ({
  __esModule: true,
  default: {
    metadata: jest.fn(), stock: jest.fn(), checkouts: jest.fn(), checkout: jest.fn(), exportCsv: jest.fn(),
  },
}));
const mocked = consumables as jest.Mocked<typeof consumables>;
const metadata: Awaited<ReturnType<typeof consumables.metadata>> = {
  can_manage: true,
  can_export: true,
  people: [
    { id: 8, name: 'Jordan Smith', department: 'Welding', is_active: true },
    { id: 9, name: 'Former Employee', department: 'Welding', is_active: false },
  ],
};
const stock: Awaited<ReturnType<typeof consumables.stock>> = {
  items: [
    {
      inventory_item_id: 101, part_id: 51, part_number: 'CON-001', part_name: '4.5 inch grinding disc',
      unit_of_measure: 'each', location: 'CRIB-A1', lot_number: 'LOT-01', quantity_on_hand: 20,
      quantity_available: 15, part_quantity_on_hand: 20, reorder_point: 10, is_low_stock: false,
      can_checkout: true, unavailable_reason: null,
    },
    {
      inventory_item_id: 102, part_id: 52, part_number: 'CON-002', part_name: 'Welding gloves',
      unit_of_measure: 'each', location: 'CRIB-A2', lot_number: null, quantity_on_hand: 0,
      quantity_available: 0, part_quantity_on_hand: 0, reorder_point: 5, is_low_stock: true,
      can_checkout: false, unavailable_reason: 'No available stock',
    },
  ],
  total: 2,
};
const checkout: Awaited<ReturnType<typeof consumables.checkout>> = {
  id: 42, inventory_item_id: 101, part_id: 51, part_number: 'CON-001', part_name: '4.5 inch grinding disc',
  unit_of_measure: 'each', location: 'CRIB-A1', lot_number: 'LOT-01', employee_id: 8,
  employee_name: 'Jordan Smith', department: 'Welding',
  issued_by: 7, issued_by_name: 'Alex Rivera', issued_at: '2026-10-27T19:15:00Z', quantity: 3,
  notes: 'Welding bay 2', request_id: '84678230-89cf-4576-b732-9b94873c5df8', replayed: false,
};

// jsdom does not provide Fetch Request; the real data-router below uses no loaders.
class RouterRequest {
  url: string;
  signal?: AbortSignal;
  method: string;
  constructor(url: string, init: RequestInit = {}) {
    this.url = url;
    this.signal = init.signal ?? undefined;
    this.method = init.method ?? 'GET';
  }
}
const originalRequest = global.Request;
beforeAll(() => {
  if (!global.Request) global.Request = RouterRequest as unknown as typeof Request;
});
afterAll(() => { global.Request = originalRequest; });

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  mocked.metadata.mockResolvedValue(metadata);
  mocked.stock.mockResolvedValue(stock);
  mocked.checkouts.mockResolvedValue({ items: [], total: 0 });
  mocked.checkout.mockResolvedValue(checkout);
  mocked.exportCsv.mockResolvedValue('Employee,Quantity\nJordan Smith,3\n');
});

function renderPage(path = '/consumables') {
  return render(<MemoryRouter initialEntries={[path]}><Consumables /></MemoryRouter>);
}

async function fillCheckout() {
  const person = await screen.findByRole('combobox', { name: /^Employee receiving supplies/ });
  selectComboBoxOption(person, /Jordan Smith/);
  fireEvent.click(await screen.findByRole('button', { name: 'Select 4.5 inch grinding disc at CRIB-A1' }));
  fireEvent.change(screen.getByRole('spinbutton', { name: /^Quantity/ }), { target: { value: '3' } });
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Notes (optional)'), { target: { value: 'Welding bay 2' } });
  });
}

it('records recipient, item and quantity, then refreshes stock and timestamped attribution', async () => {
  renderPage();
  await fillCheckout();
  const stockReads = mocked.stock.mock.calls.length;
  const historyReads = mocked.checkouts.mock.calls.length;
  mocked.stock.mockResolvedValue({ ...stock, items: [{ ...stock.items[0], quantity_on_hand: 17, quantity_available: 12 }] });
  mocked.checkouts.mockResolvedValue({ items: [checkout], total: 1 });
  fireEvent.click(screen.getByRole('button', { name: 'Record checkout' }));
  await waitFor(() => expect(mocked.checkout).toHaveBeenCalledWith(expect.objectContaining({
    inventory_item_id: 101, employee_id: 8, quantity: 3, notes: 'Welding bay 2', request_id: expect.any(String),
  })));
  expect(mocked.checkout.mock.calls[0][0]).not.toHaveProperty('issued_at');
  expect(mocked.checkout.mock.calls[0][0]).not.toHaveProperty('issued_by');
  await waitFor(() => {
    expect(mocked.stock.mock.calls.length).toBeGreaterThan(stockReads);
    expect(mocked.checkouts.mock.calls.length).toBeGreaterThan(historyReads);
  });
  expect((await screen.findAllByText('Jordan Smith')).length).toBeGreaterThan(0);
  expect(screen.getAllByText('Alex Rivera').length).toBeGreaterThan(0);
  expect(screen.getByText('Checkout recorded · CC-000042')).toBeInTheDocument();
  expect(screen.getAllByText(/Oct 27, 2026, 2:15(?::00)? PM CDT/).length).toBeGreaterThan(0);
  expect(screen.getByRole('button', { name: 'Select 4.5 inch grinding disc at CRIB-A1' })).toHaveTextContent('12');
});

it('prevents a rapid duplicate submission while a checkout is pending', async () => {
  let finish!: (value: typeof checkout) => void;
  mocked.checkout.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  renderPage();
  await fillCheckout();
  const form = screen.getByRole('button', { name: 'Record checkout' }).closest('form')!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  await waitFor(() => expect(mocked.checkout).toHaveBeenCalledTimes(1));
  expect(screen.getByRole('spinbutton', { name: /^Quantity/ })).toBeDisabled();
  await act(async () => finish(checkout));
});

it('preserves an uncertain checkout and retries the identical request without permitting edits', async () => {
  mocked.checkout.mockRejectedValueOnce(new Error('Network connection lost'));
  renderPage();
  await fillCheckout();
  fireEvent.click(screen.getByRole('button', { name: 'Record checkout' }));
  const retry = await screen.findByRole('button', { name: 'Retry checkout' });
  const original = mocked.checkout.mock.calls[0][0];
  expect(screen.getByRole('spinbutton', { name: /^Quantity/ })).toHaveValue(3);
  expect(screen.getByRole('spinbutton', { name: /^Quantity/ })).toBeDisabled();
  expect(screen.getByLabelText('Notes (optional)')).toHaveValue('Welding bay 2');
  expect(screen.getByRole('combobox', { name: /^Employee receiving supplies/ })).toBeDisabled();
  fireEvent.click(retry);
  await waitFor(() => expect(mocked.checkout).toHaveBeenCalledTimes(2));
  expect(mocked.checkout.mock.calls[1][0]).toEqual(original);
});

it('retains a rejected checkout draft and allows correcting the quantity', async () => {
  mocked.checkout.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'Only 2 each are available' } } });
  renderPage();
  await fillCheckout();
  fireEvent.click(screen.getByRole('button', { name: 'Record checkout' }));
  expect(await screen.findByText(/Only 2 each are available/)).toBeInTheDocument();
  const first = mocked.checkout.mock.calls[0][0];
  expect(screen.getByRole('spinbutton', { name: /^Quantity/ })).toBeEnabled();
  expect(screen.getByLabelText('Notes (optional)')).toHaveValue('Welding bay 2');
  fireEvent.change(screen.getByRole('spinbutton', { name: /^Quantity/ }), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record checkout' }));
  await waitFor(() => expect(mocked.checkout).toHaveBeenCalledTimes(2));
  expect(mocked.checkout.mock.calls[1][0]).toMatchObject({ employee_id: 8, inventory_item_id: 101, quantity: 2 });
  expect(mocked.checkout.mock.calls[1][0].request_id).not.toEqual(first.request_id);
});

it.each(['viewer', 'company read-only'])('exposes history without checkout actions to a %s session', async mode => {
  if (mode === 'viewer') mocked.metadata.mockResolvedValue({ ...metadata, can_manage: false, can_export: false });
  else sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '7', cid: 1, ro: true }))}.signature`);
  mocked.checkouts.mockResolvedValue({ items: [checkout], total: 1 });
  renderPage();
  expect((await screen.findAllByText('Jordan Smith')).length).toBeGreaterThan(0);
  expect(screen.queryByRole('button', { name: 'Record checkout' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry checkout' })).not.toBeInTheDocument();
  if (mode === 'viewer') expect(screen.queryByRole('button', { name: 'Export CSV' })).not.toBeInTheDocument();
  expect(mocked.checkout).not.toHaveBeenCalled();
});

it('sends employee, consumable and inclusive date filters to history and export', async () => {
  renderPage('/consumables?employee_id=8&part_id=51&date_from=2026-10-01&date_to=2026-10-27&q=disc');
  await waitFor(() => expect(mocked.checkouts).toHaveBeenCalledWith(expect.objectContaining({
    employee_id: 8, part_id: 51, date_from: '2026-10-01', date_to: '2026-10-27', q: 'disc',
  })));
  expect(screen.getByLabelText('From date')).toHaveValue('2026-10-01');
  expect(screen.getByLabelText('Through date')).toHaveValue('2026-10-27');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Export CSV' })).toBeEnabled());
  const anchor = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
  await waitFor(() => expect(mocked.exportCsv).toHaveBeenCalledWith(expect.objectContaining({
    employee_id: 8, part_id: 51, date_from: '2026-10-01', date_to: '2026-10-27', q: 'disc',
  })));
  anchor.mockRestore();
});

it('shows unavailable consumables but does not allow selecting them for checkout', async () => {
  renderPage();
  const unavailable = await screen.findByRole('button', { name: 'Select Welding gloves at CRIB-A2' });
  expect(unavailable).toBeDisabled();
  expect(within(unavailable).getByText('No available stock')).toBeInTheDocument();
});

it('keeps a committed receipt successful when the subsequent stock refresh fails', async () => {
  renderPage();
  await fillCheckout();
  mocked.stock.mockRejectedValueOnce(new Error('Refresh unavailable'));
  fireEvent.click(screen.getByRole('button', { name: 'Record checkout' }));
  expect(await screen.findByText(/^Checkout recorded/)).toBeInTheDocument();
  expect(await screen.findByText('Stock could not be refreshed')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry checkout' })).not.toBeInTheDocument();
  expect(mocked.checkout).toHaveBeenCalledTimes(1);
});

it('keeps inactive employees in history filters while excluding them from new issues', async () => {
  renderPage();
  const recipient = await screen.findByRole('combobox', { name: /^Employee receiving supplies/ });
  const recipients = openComboBox(recipient);
  expect(within(recipients).queryByRole('option', { name: /Former Employee/ })).not.toBeInTheDocument();
  fireEvent.keyDown(recipient, { key: 'Escape' });
  const historyPeople = openComboBox(screen.getByRole('combobox', { name: 'Filter by employee' }));
  expect(within(historyPeople).getByRole('option', { name: /Former Employee.*inactive/ })).toBeInTheDocument();
});

it('allows an authorized supply attendant to issue without exposing the manager-only export', async () => {
  mocked.metadata.mockResolvedValue({ ...metadata, can_manage: true, can_export: false });
  renderPage();
  await fillCheckout();
  expect(screen.getByRole('button', { name: 'Record checkout' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: 'Export CSV' })).not.toBeInTheDocument();
  expect(mocked.exportCsv).not.toHaveBeenCalled();
});

it('uses employee names and departments without displaying legacy sign-in badge codes', async () => {
  const legacyPerson = { ...metadata.people[0], employee_id: 'PRIVATE-LOGIN-SECRET' };
  const legacyReceipt = { ...checkout, employee_code: 'PRIVATE-LOGIN-SECRET' };
  mocked.metadata.mockResolvedValue({ ...metadata, people: [legacyPerson] });
  mocked.checkouts.mockResolvedValue({ items: [legacyReceipt], total: 1 });
  renderPage();
  const people = openComboBox(await screen.findByRole('combobox', { name: /^Employee receiving supplies/ }));
  expect(within(people).getByRole('option', { name: /Jordan Smith.*Welding/ })).toBeInTheDocument();
  expect(screen.queryByText(/PRIVATE-LOGIN-SECRET/)).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/badge/i)).not.toBeInTheDocument();
});

it('changes stock and history filters without discarding a draft, while leaving the page still asks', async () => {
  const userActions = userEvent.setup();
  const router = createMemoryRouter([
    {
      element: <UnsavedChangesProvider><Outlet /></UnsavedChangesProvider>,
      children: [
        { path: '/consumables', element: <Consumables /> },
        { path: '/materials', element: <p>Materials destination</p> },
      ],
    },
  ], { initialEntries: ['/consumables'] });
  render(<RouterProvider router={router} />);
  await fillCheckout();

  fireEvent.change(screen.getByLabelText('Search consumables'), { target: { value: 'grinding' } });
  await waitFor(() => expect(mocked.stock).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'grinding' })));
  expect(screen.queryByRole('dialog', { name: 'Leave with unsaved changes?' })).not.toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: /^Employee receiving supplies/ })).toHaveValue('Jordan Smith');
  expect(screen.getByRole('spinbutton', { name: /^Quantity/ })).toHaveValue(3);

  fireEvent.change(screen.getByLabelText('Search checkout history'), { target: { value: 'Jordan' } });
  await waitFor(() => expect(mocked.checkouts).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'Jordan' })));
  expect(screen.queryByRole('dialog', { name: 'Leave with unsaved changes?' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Notes (optional)')).toHaveValue('Welding bay 2');

  await userActions.click(screen.getByRole('link', { name: 'Manage consumables' }));
  const dialog = await screen.findByRole('dialog', { name: 'Leave with unsaved changes?' });
  expect(router.state.location.pathname).toBe('/consumables');
  await userActions.click(within(dialog).getByRole('button', { name: 'Stay and keep editing' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByRole('combobox', { name: /^Employee receiving supplies/ })).toHaveValue('Jordan Smith');
  expect(screen.getByLabelText('Notes (optional)')).toHaveValue('Welding bay 2');

  await userActions.click(screen.getByRole('link', { name: 'Manage consumables' }));
  await userActions.click(within(await screen.findByRole('dialog', { name: 'Leave with unsaved changes?' })).getByRole('button', { name: 'Leave and discard' }));
  expect(await screen.findByText('Materials destination')).toBeInTheDocument();
});

it('maps the history pager to correct backend offsets in both directions', async () => {
  const rows = Array.from({ length: 26 }, (_, index) => ({ ...checkout, id: 42 + index }));
  mocked.checkouts.mockImplementation(async params => ({
    items: rows.slice(params?.skip || 0, (params?.skip || 0) + 25), total: rows.length,
  }));
  renderPage();
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Next page' })[0]).toBeEnabled());
  expect(screen.getAllByRole('button', { name: 'Previous page' })[0]).toBeDisabled();
  expect(screen.getAllByText('1–25')[0]).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);
  await waitFor(() => expect(mocked.checkouts).toHaveBeenLastCalledWith(expect.objectContaining({ skip: 25, limit: 25 })));
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Previous page' })[0]).toBeEnabled());
  expect(screen.getAllByText('26–26')[0]).toBeInTheDocument();
  expect(screen.getAllByRole('button', { name: 'Next page' })[0]).toBeDisabled();
  fireEvent.click(screen.getAllByRole('button', { name: 'Previous page' })[0]);
  await waitFor(() => expect(mocked.checkouts).toHaveBeenLastCalledWith(expect.objectContaining({ skip: 0, limit: 25 })));
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Next page' })[0]).toBeEnabled());
  expect(screen.getAllByText('1–25')[0]).toBeInTheDocument();
});

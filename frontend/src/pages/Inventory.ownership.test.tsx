/**
 * Inventory page — customer- and supplier-owned stock (AS9100D 8.5.3).
 *
 * jsdom renders BOTH the desktop table and the mobile cards, so row assertions are scoped
 * to `within(screen.getByRole('table'))`.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import InventoryPage from './Inventory';
import { selectComboBoxOption } from '../test-utils/comboBox';
import { ToastProvider } from '../components/ui';
import type { InventoryOwnershipChangeResult, OwnershipInfo } from '../types/ownership';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getInventory: jest.fn(),
    getInventorySummary: jest.fn(),
    getInventoryLocations: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getParts: jest.fn(),
    receiveInventory: jest.fn(),
    changeInventoryOwnership: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));
let mockUser = { id: 1, role: 'supervisor', is_superuser: false };
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
const mockedApi = jest.mocked(api);

const part = { id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'purchased' };
interface StockRowFields extends OwnershipInfo {
  id: number;
  location: string;
  lot_number: string;
  quantity_on_hand: number;
  quantity_available: number;
}
const row = (overrides: StockRowFields) => ({
  part_id: 7,
  part,
  warehouse: 'MAIN',
  quantity_allocated: 0,
  status: 'available',
  unit_cost: 1.5,
  ownership: 'company',
  owner_customer_id: null,
  owner_vendor_id: null,
  owner_name: null,
  ...overrides,
});
const stock = [
  row({ id: 21, location: 'STORES', lot_number: 'LOT-CO', quantity_on_hand: 40, quantity_available: 40 }),
  row({
    id: 22,
    location: 'CAGE',
    lot_number: 'LOT-CU',
    quantity_on_hand: 4,
    quantity_available: 4,
    ownership: 'customer',
    owner_customer_id: 4,
    owner_name: 'Invented Aero Works',
  }),
  row({
    id: 23,
    location: 'CONSIGN',
    lot_number: 'LOT-SU',
    quantity_on_hand: 2,
    quantity_available: 2,
    ownership: 'supplier',
    owner_vendor_id: 8,
    owner_name: 'Invented Plate Supply',
  }),
];
/** What POST /inventory/items/{id}/ownership answers: the updated row plus both disclosures. */
const changeResult = (overrides: Partial<InventoryOwnershipChangeResult> = {}): InventoryOwnershipChangeResult => ({
  id: 22,
  part_id: 7,
  location: 'CAGE',
  warehouse: 'MAIN',
  quantity_on_hand: 4,
  quantity_allocated: 0,
  quantity_available: 4,
  lot_number: 'LOT-CU',
  serial_number: null,
  status: 'available',
  is_active: true,
  received_date: '2026-11-02T15:04:05Z',
  expiration_date: null,
  created_at: '2026-11-02T15:04:05Z',
  updated_at: '2026-11-19T16:00:00Z',
  ownership: 'company',
  owner_customer_id: null,
  owner_vendor_id: null,
  owner_name: null,
  open_pinned_ties: [],
  unpinned_demand: null,
  ...overrides,
});
const summary = [
  {
    part_id: 7,
    part_number: 'PN-700',
    part_name: 'Bracket',
    total_on_hand: 46,
    total_allocated: 0,
    available: 46,
    owned_by_others_on_hand: 6,
    locations: stock.map(item => ({
      location: item.location,
      quantity: item.quantity_on_hand,
      lot_number: item.lot_number,
    })),
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getInventory.mockResolvedValue(stock as never);
  mockedApi.getInventorySummary.mockResolvedValue(summary as never);
  mockedApi.getParts.mockResolvedValue([part] as never);
  mockedApi.getInventoryLocations.mockResolvedValue([
    { id: 1, code: 'STORES', name: 'Stores', warehouse: 'MAIN' },
  ] as never);
  mockedApi.getLowStockAlerts.mockResolvedValue([]);
  mockedApi.receiveInventory.mockResolvedValue({} as never);
  mockedApi.changeInventoryOwnership.mockResolvedValue(changeResult());
  mockedApi.getCustomerNames.mockResolvedValue([{ id: 4, name: 'Invented Aero Works' }]);
  mockedApi.getVendors.mockResolvedValue([{ id: 8, name: 'Invented Plate Supply', code: 'IPS', is_active: true }]);
});

function mount(path = '/inventory', { withToasts = false } = {}) {
  const page = (
    <MemoryRouter initialEntries={[path]}>
      <InventoryPage />
    </MemoryRouter>
  );
  return render(withToasts ? <ToastProvider>{page}</ToastProvider> : page);
}

async function openDetails() {
  fireEvent.change(await screen.findByRole('combobox', { name: 'Inventory view' }), { target: { value: 'details' } });
  return screen.getByRole('table');
}

const lotsIn = (table: HTMLElement) =>
  ['LOT-CO', 'LOT-CU', 'LOT-SU'].filter(lot => within(table).queryByText(lot) !== null);

it('labels only the stock that is not the company’s, and loads no owner list on page load', async () => {
  mount();
  const table = await openDetails();

  const ownedRow = within(table).getByText('LOT-CU').closest('tr')!;
  const consignedRow = within(table).getByText('LOT-SU').closest('tr')!;
  const companyRow = within(table).getByText('LOT-CO').closest('tr')!;
  expect(within(ownedRow).getByText('Customer: Invented Aero Works')).toBeInTheDocument();
  expect(within(consignedRow).getByText('Supplier: Invented Plate Supply')).toBeInTheDocument();
  expect(within(companyRow).queryByText(/^(Customer|Supplier):/)).not.toBeInTheDocument();
  expect(within(table).getByRole('columnheader', { name: /Ownership/ })).toBeInTheDocument();
  expect(mockedApi.getCustomerNames).not.toHaveBeenCalled();
  expect(mockedApi.getVendors).not.toHaveBeenCalled();
});

it('notes how much of a part’s physical on hand belongs to someone else', async () => {
  mount();

  const table = await screen.findByRole('table');
  expect(within(table).getByText('6 customer/supplier-owned')).toBeInTheDocument();
});

it('filters the detail rows by ownership without another request, and reads the filter from the URL', async () => {
  const view = mount();
  const table = await openDetails();
  expect(lotsIn(table)).toEqual(['LOT-CO', 'LOT-CU', 'LOT-SU']);

  const filter = screen.getByRole('combobox', { name: 'Filter by ownership' });
  expect(
    within(filter)
      .getAllByRole('option')
      .map(option => option.textContent)
  ).toEqual(['All owners', 'Company-owned', 'Customer-owned', 'Supplier-owned']);
  fireEvent.change(filter, { target: { value: 'customer' } });
  expect(lotsIn(screen.getByRole('table'))).toEqual(['LOT-CU']);
  fireEvent.change(filter, { target: { value: 'company' } });
  expect(lotsIn(screen.getByRole('table'))).toEqual(['LOT-CO']);
  fireEvent.change(filter, { target: { value: 'all' } });
  expect(lotsIn(screen.getByRole('table'))).toEqual(['LOT-CO', 'LOT-CU', 'LOT-SU']);
  expect(mockedApi.getInventory).toHaveBeenCalledTimes(1);
  expect(mockedApi.getInventory).toHaveBeenCalledWith();
  view.unmount();

  mount('/inventory?inventory_tab=details&ownership=supplier');
  await waitFor(() => expect(lotsIn(screen.getByRole('table'))).toEqual(['LOT-SU']));
  expect(screen.getByRole('combobox', { name: 'Filter by ownership' })).toHaveValue('supplier');
});

async function openReceive() {
  fireEvent.click(await screen.findByRole('button', { name: 'Receive Inventory' }));
  const dialog = screen.getByRole('dialog', { name: 'Receive Inventory' });
  selectComboBoxOption(within(dialog).getByRole('combobox', { name: 'Part' }), /PN-700/);
  fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Quantity' }), { target: { value: '5' } });
  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Location' }), { target: { value: 'STORES' } });
  return dialog;
}

it('receives company stock by default, sending the explicit company triple', async () => {
  mount();
  const dialog = await openReceive();

  expect(within(dialog).getByRole('combobox', { name: 'Ownership' })).toHaveValue('company');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Receive' }));

  await waitFor(() => expect(mockedApi.receiveInventory).toHaveBeenCalledTimes(1));
  expect(mockedApi.receiveInventory).toHaveBeenCalledWith(
    expect.objectContaining({
      part_id: 7,
      quantity: 5,
      location_code: 'STORES',
      ownership: 'company',
      owner_customer_id: null,
      owner_vendor_id: null,
    })
  );
  expect(mockedApi.getCustomerNames).not.toHaveBeenCalled();
});

it('receives customer-owned stock naming the customer', async () => {
  mount();
  const dialog = await openReceive();

  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const customer = await within(dialog).findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(customer).toBeEnabled());
  selectComboBoxOption(customer, 'Invented Aero Works');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Receive' }));

  await waitFor(() =>
    expect(mockedApi.receiveInventory).toHaveBeenCalledWith(
      expect.objectContaining({ part_id: 7, ownership: 'customer', owner_customer_id: 4, owner_vendor_id: null })
    )
  );
});

it('will not send a customer-owned receipt without its customer', async () => {
  mount();
  const dialog = await openReceive();

  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Receive' }));

  expect(await within(dialog).findByText('Choose the customer that owns this stock.')).toBeInTheDocument();
  expect(mockedApi.receiveInventory).not.toHaveBeenCalled();
});

it('sends heat and cert trimmed on a manual receive, and omits them when blank', async () => {
  mount();
  let dialog = await openReceive();
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Heat/ }), { target: { value: '  H-9931 ' } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Cert/ }), { target: { value: 'MTR-55' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Receive' }));
  await waitFor(() =>
    expect(mockedApi.receiveInventory).toHaveBeenCalledWith(
      expect.objectContaining({ part_id: 7, heat_lot: 'H-9931', cert_number: 'MTR-55' })
    )
  );

  dialog = await openReceive();
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Heat/ }), { target: { value: '   ' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Receive' }));
  await waitFor(() => expect(mockedApi.receiveInventory).toHaveBeenCalledTimes(2));
  const second = mockedApi.receiveInventory.mock.calls[1][0];
  expect(second.heat_lot).toBeUndefined();
  expect(second.cert_number).toBeUndefined();
});

it('caps heat and cert at the column width and tells the user a merge keeps the lot’s own values', async () => {
  mount();
  const dialog = await openReceive();
  expect(within(dialog).getByRole('textbox', { name: /^Heat/ })).toHaveAttribute('maxLength', '100');
  expect(within(dialog).getByRole('textbox', { name: /^Cert/ })).toHaveAttribute('maxLength', '100');
  expect(within(dialog).getByText(/keeps\s+that record.s own heat and cert/)).toBeInTheDocument();
});

it('shows each stock row’s heat in the detail table', async () => {
  mockedApi.getInventory.mockResolvedValue([
    { ...stock[0], heat_lot: 'H-9931', cert_number: 'MTR-55' },
    stock[1],
  ] as never);
  mount();
  const table = await openDetails();

  expect(within(table).getByRole('columnheader', { name: /Heat/ })).toBeInTheDocument();
  const heatRow = within(table).getByText('LOT-CO').closest('tr')!;
  expect(within(heatRow).getByText('H-9931')).toBeInTheDocument();
});

it('changes a row’s ownership through the reasoned dialog and re-reads the page from the server', async () => {
  mount();
  const table = await openDetails();

  const ownedRow = within(table).getByText('LOT-CU').closest('tr')!;
  fireEvent.click(within(ownedRow).getByRole('button', { name: 'Change ownership' }));
  const dialog = screen.getByRole('dialog', { name: 'Change ownership' });
  expect(dialog).toHaveTextContent('Lot LOT-CU');
  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'company' } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Reason/ }), { target: { value: 'Bought outright' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Change ownership' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Change ownership' })).not.toBeInTheDocument());
  expect(mockedApi.changeInventoryOwnership).toHaveBeenCalledWith(22, {
    ownership: 'company',
    owner_customer_id: null,
    owner_vendor_id: null,
    reason: 'Bought outright',
  });
  await waitFor(() => expect(mockedApi.getInventory).toHaveBeenCalledTimes(2));
});

/** Re-own the company row (LOT-CO) to a customer through the dialog. */
async function reownCompanyRowToCustomer() {
  const table = await openDetails();
  const companyRow = within(table).getByText('LOT-CO').closest('tr')!;
  fireEvent.click(within(companyRow).getByRole('button', { name: 'Change ownership' }));
  const dialog = screen.getByRole('dialog', { name: 'Change ownership' });
  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const picker = await within(dialog).findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(picker).toBeEnabled());
  selectComboBoxOption(picker, 'Invented Aero Works');
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Reason/ }), {
    target: { value: 'Customer-furnished after all' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Change ownership' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Change ownership' })).not.toBeInTheDocument());
}

it('warns, naming the work orders, when open jobs now depend on the re-owned stock or are short', async () => {
  mockedApi.changeInventoryOwnership.mockResolvedValue(
    changeResult({
      id: 21,
      location: 'STORES',
      lot_number: 'LOT-CO',
      ownership: 'customer',
      owner_customer_id: 4,
      owner_name: 'Invented Aero Works',
      open_pinned_ties: [
        { allocation_id: 1, work_order_id: 501, work_order_number: 'WO-INV-501', outstanding_quantity: 3 },
        { allocation_id: 2, work_order_id: 502, work_order_number: 'WO-INV-502', outstanding_quantity: 1 },
      ],
      unpinned_demand: {
        open_unpinned_demand: 4,
        company_stock_remaining: 0,
        short_by: 4,
        work_order_numbers: ['WO-INV-503'],
      },
    })
  );
  mount('/inventory', { withToasts: true });

  await reownCompanyRowToCustomer();

  // A warning (role="alert", stays until dismissed) — not the plain success.
  const toast = await screen.findByRole('alert');
  expect(toast).toHaveTextContent(
    'Ownership changed. 2 open jobs are pinned to this lot (WO-INV-501, WO-INV-502) and will still consume it at ' +
      'completion; company stock is now short by 4 for WO-INV-503.'
  );
  expect(screen.queryByText('Ownership changed.', { exact: true })).not.toBeInTheDocument();
  await waitFor(() => expect(mockedApi.getInventory).toHaveBeenCalledTimes(2));
});

it('keeps the plain success when no open job is affected', async () => {
  mockedApi.changeInventoryOwnership.mockResolvedValue(
    changeResult({
      id: 21,
      location: 'STORES',
      lot_number: 'LOT-CO',
      ownership: 'customer',
      owner_customer_id: 4,
      owner_name: 'Invented Aero Works',
      unpinned_demand: { open_unpinned_demand: 0, company_stock_remaining: 0, short_by: 0, work_order_numbers: [] },
    })
  );
  mount('/inventory', { withToasts: true });

  await reownCompanyRowToCustomer();

  expect(await screen.findByText('Ownership changed.', { exact: true })).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('offers no change-of-ownership action to a role that cannot adjust stock', async () => {
  mockUser = { id: 2, role: 'operator', is_superuser: false };
  mount();
  const table = await openDetails();

  expect(within(table).getByText('Customer: Invented Aero Works')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Change ownership' })).not.toBeInTheDocument();
});

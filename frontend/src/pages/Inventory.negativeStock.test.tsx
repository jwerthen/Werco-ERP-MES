import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import InventoryPage from './Inventory';

jest.mock('../services/api', () => ({ __esModule: true, default: {
  getInventory: jest.fn(), getInventorySummary: jest.fn(), getInventoryLocations: jest.fn(),
  getLowStockAlerts: jest.fn(), getParts: jest.fn(), adjustInventory: jest.fn(),
} }));
let mockUser = { id: 1, role: 'supervisor', is_superuser: false };
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
const mockedApi = jest.mocked(api);
const part = { id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'manufactured' };
const stock = [
  { id: 21, part_id: 7, part, location: 'FINISHED-GOODS', warehouse: 'MAIN', lot_number: 'LOT-A', quantity_on_hand: -16, quantity_allocated: 0, quantity_available: -16, status: 'available', unit_cost: 1.5 },
  { id: 22, part_id: 7, part, location: 'STORES', warehouse: 'MAIN', lot_number: 'LOT-B', quantity_on_hand: 40, quantity_allocated: 0, quantity_available: 40, status: 'available', unit_cost: 1.5 },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getInventory.mockResolvedValue(stock as any);
  mockedApi.getInventorySummary.mockResolvedValue([{ part_id: 7, part_number: 'PN-700', part_name: 'Bracket', total_on_hand: 24, total_allocated: 0, available: 24, locations: stock.map(item => ({ location: item.location, quantity: item.quantity_on_hand, lot_number: item.lot_number })) }] as any);
  mockedApi.getParts.mockResolvedValue([part] as any);
  mockedApi.getInventoryLocations.mockResolvedValue([]);
  mockedApi.getLowStockAlerts.mockResolvedValue([]);
  mockedApi.adjustInventory.mockResolvedValue({});
});

function mount() { return render(<MemoryRouter><InventoryPage /></MemoryRouter>); }

it('flags negative summary locations on desktop and mobile and opens the exact lot for an audited adjustment', async () => {
  mount();
  const negativeLabels = await screen.findAllByText('(-16) Negative stock');
  expect(negativeLabels).toHaveLength(2);
  negativeLabels.forEach(label => expect(label).toHaveClass('text-red-300'));
  screen.getAllByText('(40)').forEach(label => expect(label).toHaveClass('text-slate-400'));
  expect(screen.getAllByRole('button', { name: 'Resolve adjustment' })).toHaveLength(2);
  fireEvent.click(within(screen.getByRole('table')).getByRole('button', { name: 'Resolve adjustment' }));
  const dialog = screen.getByRole('dialog', { name: 'Resolve negative inventory' });
  expect(dialog).toHaveTextContent('PN-700 · Record #21');
  expect(dialog).toHaveTextContent('MAIN / FINISHED-GOODS · Lot: LOT-A');
  expect(dialog).toHaveTextContent('Current on hand: -16');
  expect(within(dialog).getByRole('spinbutton', { name: 'Verified on-hand quantity' })).toHaveValue(null);
  expect(within(dialog).getByRole('button', { name: 'Save adjustment' })).toBeDisabled();
  expect(mockedApi.adjustInventory).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Verified on-hand quantity' }), { target: { value: '5' } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Adjustment reason' }), { target: { value: 'Physical count verified' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save adjustment' }));
  await waitFor(() => expect(mockedApi.adjustInventory).toHaveBeenCalledWith({ inventory_item_id: 21, new_quantity: 5, reason_code: 'Physical count verified', notes: '' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(mockedApi.getInventory).toHaveBeenCalledTimes(2);
});

it('flags negative detail quantities and makes the same resolve action available on both layouts', async () => {
  mount();
  fireEvent.change(await screen.findByRole('combobox', { name: 'Inventory view' }), { target: { value: 'details' } });
  const row = within(screen.getByRole('table')).getByText('FINISHED-GOODS').closest('tr')!;
  expect(within(row).getAllByText('Negative stock')).toHaveLength(2);
  expect(within(row).getAllByText('Negative stock')[0].parentElement).toHaveClass('text-red-300');
  expect(screen.getAllByRole('button', { name: 'Resolve adjustment' })).toHaveLength(2);
  fireEvent.click(screen.getAllByRole('button', { name: 'Resolve adjustment' })[1]);
  expect(screen.getByRole('dialog', { name: 'Resolve negative inventory' })).toHaveTextContent('Record #21');
});

it('keeps negative stock visible to a view-only role without exposing adjustment controls', async () => {
  mockUser.role = 'operator';
  mount();
  expect(await screen.findAllByText('(-16) Negative stock')).toHaveLength(2);
  expect(screen.queryByRole('button', { name: 'Resolve adjustment' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole('combobox', { name: 'Inventory view' }), { target: { value: 'details' } });
  expect(screen.getAllByText('Negative stock')).toHaveLength(4);
  expect(screen.queryByRole('button', { name: 'Resolve adjustment' })).not.toBeInTheDocument();
  expect(mockedApi.adjustInventory).not.toHaveBeenCalled();
});

it('requires selection when the summary location matches multiple stock records', async () => {
  mockedApi.getInventory.mockResolvedValue([...stock, { ...stock[0], id: 23, serial_number: 'S-2' }] as any);
  mount();
  fireEvent.click((await screen.findAllByRole('button', { name: 'Resolve adjustment' }))[0]);
  const dialog = screen.getByRole('dialog', { name: 'Resolve negative inventory' });
  const record = within(dialog).getByRole('combobox', { name: 'Stock record' });
  expect(record).toHaveValue('');
  expect(within(dialog).getByRole('button', { name: 'Save adjustment' })).toBeDisabled();
  fireEvent.change(record, { target: { value: '23' } });
  expect(dialog).toHaveTextContent('PN-700 · Record #23');
  expect(dialog).toHaveTextContent('Serial: S-2');
});

it('preserves entered correction and shows the API error if the adjustment fails', async () => {
  mockedApi.adjustInventory.mockRejectedValue({ response: { data: { detail: 'Adjustment unavailable' } } });
  mount();
  fireEvent.click((await screen.findAllByRole('button', { name: 'Resolve adjustment' }))[0]);
  const dialog = screen.getByRole('dialog', { name: 'Resolve negative inventory' });
  fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Verified on-hand quantity' }), { target: { value: '0' } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Adjustment reason' }), { target: { value: 'Count verified' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save adjustment' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Adjustment unavailable');
  expect(within(dialog).getByRole('spinbutton', { name: 'Verified on-hand quantity' })).toHaveValue(0);
  expect(mockedApi.getInventory).toHaveBeenCalledTimes(1);
});

it('groups quantities in the KPI strip, primary cells and locations without rounding row quantities', async () => {
  mockedApi.getInventory.mockResolvedValue([{ ...stock[1], quantity_on_hand: 74092.1254, quantity_available: 74092.1254 }] as any);
  mockedApi.getInventorySummary.mockResolvedValue([{ part_id: 7, part_number: 'PN-700', part_name: 'Bracket', total_on_hand: 74092.1254, total_allocated: 0, available: 74092.1254, locations: [{ location: 'STORES', quantity: 74092.1254 }] }] as any);
  mount();
  expect(await screen.findAllByText('74,092')).toHaveLength(2);
  const table = screen.getByRole('table');
  expect(within(table).getAllByText('74,092.1254')).toHaveLength(2);
  expect(within(table).getByText('(74,092.1254)')).toBeInTheDocument();
  fireEvent.change(screen.getByRole('combobox', { name: 'Inventory view' }), { target: { value: 'details' } });
  expect(within(screen.getByRole('table')).getAllByText('74,092.1254')).toHaveLength(2);
});

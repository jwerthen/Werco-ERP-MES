/**
 * ToolManagement — customer- and supplier-owned tooling (AS9100D 8.5.3).
 *
 * A customer-furnished fixture or a supplier's gauge sits in the crib beside the shop's own
 * tools. The list labels it, filters by it, and the create form records it. jsdom renders the
 * desktop table AND the mobile cards, so row assertions are scoped to the table.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ToolManagement from './ToolManagement';
import { selectComboBoxOption } from '../test-utils/comboBox';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getToolDashboard: jest.fn(),
    getTools: jest.fn(),
    getToolsCheckedOut: jest.fn(),
    getToolsReplacementDue: jest.fn(),
    getToolsInspectionDue: jest.fn(),
    getToolHistory: jest.fn(),
    createTool: jest.fn(),
    checkoutTool: jest.fn(),
    checkinTool: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));
const mockedApi = jest.mocked(api);

const baseTool = {
  description: '',
  location: 'Cage A',
  current_uses: 0,
  current_life_hours: 0,
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  tool_type: 'fixture',
  status: 'available',
  ownership: 'company',
  owner_customer_id: null,
  owner_vendor_id: null,
  owner_name: null,
};
const tools = [
  { ...baseTool, id: 1, tool_number: 'TL-100', name: 'Shop end mill' },
  {
    ...baseTool,
    id: 2,
    tool_number: 'TL-200',
    name: 'Customer weld fixture',
    ownership: 'customer',
    owner_customer_id: 4,
    owner_name: 'Invented Aero Works',
  },
  {
    ...baseTool,
    id: 3,
    tool_number: 'TL-300',
    name: 'Consigned bore gauge',
    ownership: 'supplier',
    owner_vendor_id: 8,
    owner_name: 'Invented Plate Supply',
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getToolDashboard.mockResolvedValue({
    total_tools: 3,
    checked_out: 0,
    replacement_due: 0,
    inspection_due: 0,
    by_status: {},
    by_type: {},
  });
  mockedApi.getTools.mockResolvedValue(tools as never);
  mockedApi.getToolsCheckedOut.mockResolvedValue([]);
  mockedApi.getToolsReplacementDue.mockResolvedValue([]);
  mockedApi.getToolsInspectionDue.mockResolvedValue([]);
  mockedApi.getToolHistory.mockResolvedValue([] as never);
  mockedApi.createTool.mockResolvedValue({ id: 9 } as never);
  mockedApi.getCustomerNames.mockResolvedValue([{ id: 4, name: 'Invented Aero Works' }]);
  mockedApi.getVendors.mockResolvedValue([{ id: 8, name: 'Invented Plate Supply', code: 'IPS', is_active: true }]);
});

function renderPage() {
  return render(
    <MemoryRouter>
      <ToolManagement />
    </MemoryRouter>
  );
}

const toolNumbersIn = (table: HTMLElement) =>
  ['TL-100', 'TL-200', 'TL-300'].filter(number => within(table).queryByText(number) !== null);

it('labels owned tooling in the list and filters by ownership', async () => {
  renderPage();
  await screen.findAllByText('TL-200');
  const table = screen.getByRole('table');

  expect(
    within(within(table).getByText('TL-200').closest('tr')!).getByText('Customer: Invented Aero Works')
  ).toBeInTheDocument();
  expect(
    within(within(table).getByText('TL-300').closest('tr')!).getByText('Supplier: Invented Plate Supply')
  ).toBeInTheDocument();
  expect(
    within(within(table).getByText('TL-100').closest('tr')!).queryByText(/^(Customer|Supplier):/)
  ).not.toBeInTheDocument();

  const filter = screen.getByRole('combobox', { name: 'Filter by ownership' });
  fireEvent.change(filter, { target: { value: 'customer' } });
  expect(toolNumbersIn(screen.getByRole('table'))).toEqual(['TL-200']);
  fireEvent.change(filter, { target: { value: 'supplier' } });
  expect(toolNumbersIn(screen.getByRole('table'))).toEqual(['TL-300']);
  fireEvent.change(filter, { target: { value: 'company' } });
  expect(toolNumbersIn(screen.getByRole('table'))).toEqual(['TL-100']);
  fireEvent.change(filter, { target: { value: '' } });
  expect(toolNumbersIn(screen.getByRole('table'))).toEqual(['TL-100', 'TL-200', 'TL-300']);
  expect(mockedApi.getCustomerNames).not.toHaveBeenCalled();
  expect(mockedApi.getVendors).not.toHaveBeenCalled();
});

it('says no tools match when the ownership filter empties the list', async () => {
  mockedApi.getTools.mockResolvedValue([tools[0]] as never);
  renderPage();
  await screen.findAllByText('TL-100');

  fireEvent.change(screen.getByRole('combobox', { name: 'Filter by ownership' }), { target: { value: 'supplier' } });

  // The empty state renders once per layout (desktop and mobile).
  expect((await screen.findAllByText('No tools found')).length).toBeGreaterThan(0);
  expect(screen.getAllByText('No tools match the current filters.').length).toBeGreaterThan(0);
  expect(screen.queryByText('No tools yet')).not.toBeInTheDocument();
});

async function openCreate() {
  await screen.findAllByText('TL-100');
  fireEvent.click(screen.getAllByRole('button', { name: /New Tool/ })[0]);
  const dialog = screen.getByRole('dialog', { name: 'New Tool' });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Tool Number/ }), { target: { value: 'FIX-900' } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /^Name/ }), { target: { value: 'Invented drill jig' } });
  return dialog;
}

it('creates a company tool by default, sending the explicit company triple', async () => {
  renderPage();
  const dialog = await openCreate();

  expect(within(dialog).getByRole('combobox', { name: 'Ownership' })).toHaveValue('company');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

  await waitFor(() =>
    expect(mockedApi.createTool).toHaveBeenCalledWith(
      expect.objectContaining({
        tool_number: 'FIX-900',
        name: 'Invented drill jig',
        ownership: 'company',
        owner_customer_id: null,
        owner_vendor_id: null,
      })
    )
  );
});

it('creates a customer-furnished tool only once its customer is chosen', async () => {
  renderPage();
  const dialog = await openCreate();

  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const create = within(dialog).getByRole('button', { name: 'Create' });
  expect(create).toBeDisabled();

  const customer = await within(dialog).findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(customer).toBeEnabled());
  selectComboBoxOption(customer, 'Invented Aero Works');
  expect(create).toBeEnabled();
  fireEvent.click(create);

  await waitFor(() =>
    expect(mockedApi.createTool).toHaveBeenCalledWith(
      expect.objectContaining({
        tool_number: 'FIX-900',
        ownership: 'customer',
        owner_customer_id: 4,
        owner_vendor_id: null,
      })
    )
  );
});

it('creates a supplier-owned tool naming the vendor, and shows a server refusal verbatim', async () => {
  mockedApi.createTool.mockRejectedValueOnce({
    response: { status: 404, data: { detail: 'Owning vendor not found' } },
  });
  renderPage();
  const dialog = await openCreate();

  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Ownership' }), { target: { value: 'supplier' } });
  const supplier = await within(dialog).findByRole('combobox', { name: /^Owning supplier/ });
  await waitFor(() => expect(within(supplier).getAllByRole('option')).toHaveLength(2));
  fireEvent.change(supplier, { target: { value: '8' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

  expect(await within(dialog).findByText('Owning vendor not found')).toBeInTheDocument();
  expect(mockedApi.createTool).toHaveBeenCalledWith(
    expect.objectContaining({ ownership: 'supplier', owner_customer_id: null, owner_vendor_id: 8 })
  );
  expect(screen.getByRole('dialog', { name: 'New Tool' })).toBeInTheDocument();
});

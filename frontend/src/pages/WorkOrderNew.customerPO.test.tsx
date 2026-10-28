import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import type { Part } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getParts: jest.fn(),
    getBOMs: jest.fn(),
    getWorkCenters: jest.fn(),
    getCustomerNames: jest.fn(),
    getCustomerPOSuggestions: jest.fn(),
    getPartReadiness: jest.fn(),
    getRoutingByPart: jest.fn(),
    previewWorkOrderOperations: jest.fn(),
    createWorkOrder: jest.fn(),
    createCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;
const part: Part = {
  id: 1,
  version: 1,
  part_number: 'PN-7731',
  revision: 'A',
  name: 'Weldment, frame',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};
const po = 'PO/100 #2+%';

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  mockedApi.getParts.mockResolvedValue([part]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getCustomerNames.mockResolvedValue([
    { id: 10, name: 'Acme & Co' },
    { id: 11, name: 'Beta Aerospace' },
  ]);
  mockedApi.getPartReadiness.mockResolvedValue({ ready: true, blockers: [], warnings: [], checks: {} });
  mockedApi.getRoutingByPart.mockResolvedValue(null);
  mockedApi.createWorkOrder.mockResolvedValue({ id: 42 });
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({
    items: [
      { customer_name: 'Acme & Co', customer_po: po },
      { customer_name: 'Beta Aerospace', customer_po: po },
    ],
    has_more: false,
  });
});

async function renderPage() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/new']}>
        <WorkOrderNew />
      </MemoryRouter>
    </ToastProvider>
  );
  await screen.findByRole('combobox', { name: 'Customer PO #' });
  fireEvent.change(screen.getByRole('combobox', { name: /^Part/ }), { target: { value: part.part_number } });
  fireEvent.mouseDown(await screen.findByRole('option', { name: /PN-7731/ }));
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(part.id));
}

function save() {
  fireEvent.click(screen.getByRole('button', { name: /create work order/i }));
}

it('selects a labeled customer/PO pair with the keyboard and saves it only after Create', async () => {
  await renderPage();
  const input = screen.getByRole('combobox', { name: 'Customer PO #' });
  fireEvent.change(input, { target: { value: 'PO/1' } });
  await screen.findByRole('option', { name: `${po} Beta Aerospace` });
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenCalledWith(
    { search: 'PO/1', limit: 10 },
    expect.any(AbortSignal)
  );
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(input).toHaveValue(po);
  expect(screen.getByLabelText('Customer Name')).toHaveValue('Beta Aerospace');
  expect(screen.queryByRole('listbox', { name: 'Existing customer purchase orders' })).not.toBeInTheDocument();
  expect(mockedApi.createWorkOrder).not.toHaveBeenCalled();
  save();
  await waitFor(() =>
    expect(mockedApi.createWorkOrder).toHaveBeenCalledWith(
      expect.objectContaining({
        part_id: 1,
        customer_po: po,
        customer_name: 'Beta Aerospace',
      })
    )
  );
  expect(mockedApi.createCustomer).not.toHaveBeenCalled();
});

it('limits suggestions to the entered customer and retains that customer in the saved job', async () => {
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({
    items: [{ customer_name: 'Acme & Co', customer_po: po }],
    has_more: false,
  });
  await renderPage();
  fireEvent.change(screen.getByLabelText('Customer Name'), { target: { value: 'Acme & Co' } });
  fireEvent.change(screen.getByRole('combobox', { name: 'Customer PO #' }), { target: { value: 'PO/' } });
  fireEvent.click(await screen.findByRole('option', { name: `${po} Acme & Co` }));
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenCalledWith(
    { search: 'PO/', customer_name: 'Acme & Co', limit: 10 },
    expect.any(AbortSignal)
  );
  save();
  await waitFor(() =>
    expect(mockedApi.createWorkOrder).toHaveBeenCalledWith(
      expect.objectContaining({ customer_po: po, customer_name: 'Acme & Co' })
    )
  );
});

it('keeps a new typed PO number valid when no existing PO matches', async () => {
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({ items: [], has_more: false });
  await renderPage();
  fireEvent.change(screen.getByRole('combobox', { name: 'Customer PO #' }), { target: { value: 'NEW-PO-2044' } });
  await screen.findByText('No matching POs. You can use this as a new PO number.');
  save();
  await waitFor(() =>
    expect(mockedApi.createWorkOrder).toHaveBeenCalledWith(
      expect.objectContaining({ customer_po: 'NEW-PO-2044', customer_name: '' })
    )
  );
});

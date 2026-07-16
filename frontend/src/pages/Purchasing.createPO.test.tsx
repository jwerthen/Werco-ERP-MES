/**
 * Purchasing — manual PO creation validation + RBAC gating.
 *
 * Covers the go-live PO-creation fixes:
 *  - handleCreatePO blocks submit client-side until a vendor is selected,
 *    at least one line exists, and every line has a part — instead of letting
 *    the server 422/500
 *  - a cleared quantity input becomes 0 (NaN guard) and blocks submit
 *  - a blank Required Date is OMITTED from the payload (an empty string 422s
 *    server-side)
 *  - the New PO / New Vendor / Send actions are hidden from roles the backend
 *    would 403 (purchasing:create / purchasing:approve parity)
 *
 * The api service + AuthContext are mocked at the module boundary — no real
 * network (same pattern as the sibling Purchasing tests).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Purchasing from './Purchasing';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    createPurchaseOrder: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const vendors = [{ id: 1, code: 'VND-001', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 }];

const purchaseOrders = [
  {
    id: 10,
    po_number: 'PO-1001',
    vendor_id: 1,
    vendor_name: 'Acme Aerospace',
    status: 'draft',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 1250.5,
    line_count: 3,
  },
];

const parts = [{ id: 9, part_number: 'PN-9', name: 'Widget' }];

const renderPurchasing = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <Purchasing />
      </ToastProvider>
    </MemoryRouter>
  );

/** Load the page and open the Create PO modal; returns the modal form. */
async function openCreatePOForm(): Promise<HTMLFormElement> {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
  fireEvent.click(screen.getByRole('button', { name: /new po/i }));
  await screen.findByRole('heading', { name: 'Create Purchase Order' });
  return screen.getByLabelText(/vendor/i).closest('form') as HTMLFormElement;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders as any);
  mockedApi.getParts.mockResolvedValue(parts as any);
  mockedApi.createPurchaseOrder.mockResolvedValue({ id: 99 } as any);
});

describe('Purchasing — create PO validation', () => {
  test('blocks submit until vendor, line, and part are valid, then omits the blank required_date', async () => {
    const form = await openCreatePOForm();

    // No vendor selected → blocked client-side with a message, no request.
    fireEvent.submit(form);
    expect(mockedApi.createPurchaseOrder).not.toHaveBeenCalled();
    expect(await screen.findByText('Please select a vendor')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });

    // Vendor but no lines → blocked with a message.
    fireEvent.submit(form);
    expect(mockedApi.createPurchaseOrder).not.toHaveBeenCalled();
    expect(await screen.findByText('Please add at least one line item')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    // Line exists but no part selected → blocked with a message.
    fireEvent.submit(form);
    expect(mockedApi.createPurchaseOrder).not.toHaveBeenCalled();
    expect(await screen.findByText('Line 1: please select a part')).toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue('Select part...'), { target: { value: '9' } });
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createPurchaseOrder).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createPurchaseOrder.mock.calls[0][0];
    expect(payload.vendor_id).toBe(1);
    // The date input was left blank — '' 422s server-side, so it must be omitted.
    expect(payload.required_date).toBeUndefined();
    expect(payload.lines).toEqual([{ part_id: 9, quantity_ordered: 1, unit_price: 0 }]);
  });

  test('a cleared quantity input becomes 0 (NaN guard) and blocks submit', async () => {
    const form = await openCreatePOForm();

    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));
    fireEvent.change(screen.getByDisplayValue('Select part...'), { target: { value: '9' } });

    const qty = screen.getByLabelText('Quantity ordered');
    fireEvent.change(qty, { target: { value: '' } });
    expect(qty).toHaveValue(0); // NaN never reaches state

    fireEvent.submit(form);
    expect(mockedApi.createPurchaseOrder).not.toHaveBeenCalled();
  });
});

describe('Purchasing — RBAC action gating', () => {
  test('viewer sees no New PO / New Vendor buttons and no Send action', async () => {
    mockAuthUser = { id: 2, role: 'viewer' };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    expect(screen.queryByRole('button', { name: /new po/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /new vendor/i })).toBeNull();

    // Draft PO row renders without the Send action (send is admin/manager only).
    const table = screen.getByTestId('data-table');
    expect(within(table).getByText('PO-1001')).toBeInTheDocument();
    expect(within(table).queryByRole('button', { name: /^send$/i })).toBeNull();
  });

  test('supervisor sees New PO (purchasing:create) but not New Vendor or Send', async () => {
    mockAuthUser = { id: 3, role: 'supervisor' };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    expect(screen.getByRole('button', { name: /new po/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /new vendor/i })).toBeNull();

    const table = screen.getByTestId('data-table');
    expect(within(table).queryByRole('button', { name: /^send$/i })).toBeNull();
  });

  test('manager sees the Send action on a draft PO', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    const table = screen.getByTestId('data-table');
    expect(within(table).getByRole('button', { name: /^send$/i })).toBeInTheDocument();
  });
});

/**
 * Purchasing — PO / vendor soft-delete controls.
 *
 * Covers the delete feature wired into the Purchasing page:
 *  - a PO Delete action opens the confirm dialog and, on confirm, calls
 *    api.deletePurchaseOrder with the PO id;
 *  - a server 400 (received material) leaves the row in place and surfaces the
 *    verbatim `detail` in an error toast (no success toast, no dead state);
 *  - a Vendor Delete action calls api.deleteVendor with the vendor id;
 *  - the Delete controls are hidden for a role below the [admin, manager] gate
 *    (RBAC parity with the backend require_role), so no button 403s.
 *
 * The api service + AuthContext are mocked at the module boundary; the real
 * ToastProvider wraps the page so the error toast text is assertable (same
 * pattern as the sibling Purchasing tests).
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
    deletePurchaseOrder: jest.fn(),
    deleteVendor: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const vendors = [
  { id: 5, code: 'VND-005', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 },
];

const purchaseOrders = [
  {
    id: 10,
    po_number: 'PO-2001',
    vendor_id: 5,
    vendor_name: 'Acme Aerospace',
    status: 'sent',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 1250.5,
    line_count: 3,
  },
];

const renderPurchasing = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <Purchasing />
      </ToastProvider>
    </MemoryRouter>,
  );

// The confirm dialog's "Delete" button (portaled to document.body as role=dialog),
// distinct from the row-level "Delete" trigger.
const confirmDialogDeleteButton = () =>
  within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' });

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders as any);
  mockedApi.getParts.mockResolvedValue([] as any);
});

describe('Purchasing — PO delete', () => {
  test('confirm dialog calls api.deletePurchaseOrder with the PO id', async () => {
    mockedApi.deletePurchaseOrder.mockResolvedValueOnce({ message: 'deleted', can_restore: true });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Row-level Delete (the desktop DataTable + mobile card both render one; either
    // opens the same confirm dialog). Click the first, then confirm in the dialog.
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deletePurchaseOrder).toHaveBeenCalledWith(10));
  });

  test('a 400 (received material) surfaces the verbatim detail toast and keeps the row', async () => {
    const detail = 'Cannot delete purchase order PO-2001: it has received material. Void the receipt(s) first, then delete.';
    mockedApi.deletePurchaseOrder.mockRejectedValueOnce(http(400, detail));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deletePurchaseOrder).toHaveBeenCalledWith(10));
    // Verbatim server detail in an error toast; the PO row is still present.
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getAllByText('PO-2001').length).toBeGreaterThan(0);
  });
});

describe('Purchasing — vendor delete', () => {
  test('confirm dialog calls api.deleteVendor with the vendor id', async () => {
    mockedApi.deleteVendor.mockResolvedValueOnce({ message: 'deleted', can_restore: true });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Switch to the Vendors tab, then delete the vendor.
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
    await screen.findByText('VND-005');
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deleteVendor).toHaveBeenCalledWith(5));
  });
});

describe('Purchasing — RBAC gating', () => {
  test('Delete controls are hidden for a supervisor (below the admin/manager gate)', async () => {
    mockAuthUser = { id: 9, role: 'supervisor' };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // No PO Delete trigger on the orders tab.
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();

    // ...and none on the vendors tab either.
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
    await screen.findByText('VND-005');
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(mockedApi.deletePurchaseOrder).not.toHaveBeenCalled();
    expect(mockedApi.deleteVendor).not.toHaveBeenCalled();
  });
});

/**
 * Purchasing — `?po=<id>` notification deep-link landing.
 *
 * `receipt.created` / `receipt.voided` / `receipt.corrected` / `po.sent` all now
 * emit `/purchasing?po=<id>` (backend app/services/notification_links.py). The
 * reported bug was that they emitted `/purchasing/<id>`, which is not a route
 * and rendered the app's 404 screen.
 *
 * The subtle half is the MISS. `list_purchase_orders` excludes CLOSED and
 * CANCELLED POs, so a deep link can point at a PO the loaded list does not
 * contain. Before this change the effect's `.find()` returned undefined and the
 * handler simply fell through — no fetch, no toast, no error. A silent no-op
 * looks like success, which is worse than a 404 because the user believes the
 * page is showing them the record. These tests pin the by-id fallback and,
 * critically, that it fires EXACTLY ONCE: `purchaseOrders` is in the effect's
 * dependency array and the fallback writes it, so without the once-per-id ref
 * latch this is an infinite fetch loop.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Purchasing from './Purchasing';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    getPurchaseOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const vendors = [{ id: 5, code: 'VND-005', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 }];

/** The flat summary shape `GET /purchasing/purchase-orders` returns. */
const listedPO = {
  id: 10,
  po_number: 'PO-2001',
  vendor_id: 5,
  vendor_name: 'Acme Aerospace',
  status: 'sent',
  order_date: '2026-06-20',
  required_date: '2026-07-01',
  total: 1250.5,
  line_count: 3,
};

/**
 * `GET /purchasing/purchase-orders/{id}` returns POResponse — a NESTED `vendor`
 * object and a full `lines` array, not the list's flat `vendor_name` /
 * `line_count`. If the page's mapping is wrong, the row renders blank.
 */
const detailPO = {
  id: 77,
  po_number: 'PO-2077',
  vendor_id: 5,
  vendor: { id: 5, code: 'VND-005', name: 'Acme Aerospace' },
  status: 'closed',
  order_date: '2026-05-01',
  required_date: '2026-05-20',
  total: 999.25,
  lines: [{ id: 1 }, { id: 2 }],
};

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <Routes>
          <Route path="/purchasing" element={<Purchasing />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue([listedPO] as any);
  mockedApi.getParts.mockResolvedValue([] as any);
  mockedApi.getPurchaseOrder.mockResolvedValue(detailPO as any);
});

const searchBox = () => screen.getByLabelText('Search purchase orders') as HTMLInputElement;

/**
 * DataTable renders the desktop <table> AND the responsive mobile cards into the
 * same jsdom tree, so every PO number appears twice. Scope to the table.
 */
const tableRowFor = async (poNumber: string): Promise<HTMLElement> => {
  const table = await screen.findByTestId('data-table');
  const cell = await within(table).findByText(poNumber);
  return cell.closest('tr') as HTMLElement;
};

describe('?po= hits the loaded list', () => {
  test('selects the Orders tab and filters to that PO without an extra fetch', async () => {
    renderAt('/purchasing?po=10');
    await waitFor(() => expect(searchBox().value).toBe('PO-2001'));
    expect(await tableRowFor('PO-2001')).toBeInTheDocument();
    // Already present, so no by-id call.
    expect(mockedApi.getPurchaseOrder).not.toHaveBeenCalled();
  });
});

describe('?po= misses the loaded list (CLOSED / CANCELLED / outside the window)', () => {
  test('falls back to a by-id fetch and makes the PO visible', async () => {
    renderAt('/purchasing?po=77');
    await waitFor(() => expect(mockedApi.getPurchaseOrder).toHaveBeenCalledWith(77));
    expect(await tableRowFor('PO-2077')).toBeInTheDocument();
    await waitFor(() => expect(searchBox().value).toBe('PO-2077'));
  });

  test('the nested POResponse shape is mapped onto the flat list row', async () => {
    // Guards the field mapping: POResponse has `vendor.name` and `lines`, the
    // list row wants `vendor_name` and `line_count`.
    renderAt('/purchasing?po=77');
    const row = await tableRowFor('PO-2077');
    expect(row).toHaveTextContent('Acme Aerospace');
    expect(row).toHaveTextContent('2');
  });

  test('the fallback fetch fires EXACTLY ONCE (no infinite loop)', async () => {
    // The regression this exists for: the effect depends on `purchaseOrders`
    // and the fallback writes `purchaseOrders`. Without the ref latch this
    // re-fires forever and hammers the API.
    renderAt('/purchasing?po=77');
    await waitFor(() => expect(mockedApi.getPurchaseOrder).toHaveBeenCalledWith(77));
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(mockedApi.getPurchaseOrder).toHaveBeenCalledTimes(1);
  });

  test('a rejected fetch shows an error toast instead of a silent no-op, and does not retry', async () => {
    mockedApi.getPurchaseOrder.mockRejectedValue(new Error('404'));
    renderAt('/purchasing?po=77');

    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument();
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(mockedApi.getPurchaseOrder).toHaveBeenCalledTimes(1);
  });

  test('the fallback still runs when the list came back EMPTY', async () => {
    // A guard of `purchaseOrders.length > 0` would skip the fallback in exactly
    // the case a deep link most needs it.
    mockedApi.getPurchaseOrders.mockResolvedValue([] as any);
    renderAt('/purchasing?po=77');
    await waitFor(() => expect(mockedApi.getPurchaseOrder).toHaveBeenCalledWith(77));
    expect(await tableRowFor('PO-2077')).toBeInTheDocument();
  });
});

describe('no ?po= param', () => {
  test('nothing is fetched by id and the search box stays empty', async () => {
    renderAt('/purchasing');
    await tableRowFor('PO-2001');
    expect(mockedApi.getPurchaseOrder).not.toHaveBeenCalled();
    expect(searchBox().value).toBe('');
  });
});

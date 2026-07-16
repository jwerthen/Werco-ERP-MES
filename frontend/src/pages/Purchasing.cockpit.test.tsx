/**
 * Purchasing cockpit — instrument-panel overhaul regression.
 *
 * The page gained a MiniStat summary strip ("Open POs" / "Approved Vendors")
 * and tab badges carrying the same counts, sitting above the Purchase Orders
 * and Vendors tables. This locks that the KPI strip renders its derived counts,
 * the tab badges carry their counts, and the tables mount with their data after
 * the initial load.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Purchasing from './Purchasing';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
  },
}));

// The page gates its create/send actions by role (useAuth), so tests need an
// authenticated user; manager sees every action.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Two vendors, one approved; two purchase orders.
const vendors = [
  {
    id: 1,
    code: 'VND-001',
    name: 'Acme Aerospace',
    contact_name: 'Pat Lee',
    email: 'pat@acme.test',
    is_approved: true,
    is_as9100_certified: true,
    is_iso9001_certified: false,
    is_active: true,
    payment_terms: 'NET 30',
    version: 0,
  },
  {
    id: 2,
    code: 'VND-002',
    name: 'Bravo Metals',
    contact_name: 'Sam Roe',
    email: 'sam@bravo.test',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    payment_terms: '',
    version: 0,
  },
];

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
  {
    id: 11,
    po_number: 'PO-1002',
    vendor_id: 1,
    vendor_name: 'Acme Aerospace',
    status: 'sent',
    order_date: '2026-06-22',
    required_date: '2026-07-05',
    total: 980,
    line_count: 1,
  },
];

const renderPurchasing = () => render(<MemoryRouter><Purchasing /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders as any);
  mockedApi.getParts.mockResolvedValue([] as any);
});

test('mounts with the heading and the Purchase Orders table after load', async () => {
  renderPurchasing();
  expect(await screen.findByRole('heading', { name: 'Purchasing & Receiving' })).toBeInTheDocument();
  // Orders tab is the default view — its rows render. The list renders both a
  // desktop <table> and a parallel mobile-card list (DataTable.mobileCards), so
  // each PO number appears twice in jsdom; scope to the desktop table.
  const ordersTable = within(screen.getByTestId('data-table'));
  expect(ordersTable.getByText('PO-1001')).toBeInTheDocument();
  expect(ordersTable.getByText('PO-1002')).toBeInTheDocument();
});

test('MiniStat summary strip renders its derived counts', async () => {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  // Open POs == purchaseOrders.length (2). The label sits in its own tile.
  const openPos = screen.getByText('Open POs').closest('div')!.parentElement!;
  expect(within(openPos).getByText('2')).toBeInTheDocument();

  // Approved Vendors == vendors with is_approved (1).
  const approved = screen.getByText('Approved Vendors').closest('div')!.parentElement!;
  expect(within(approved).getByText('1')).toBeInTheDocument();
});

test('tab badges carry the orders and vendors counts', async () => {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  const ordersTab = screen.getByRole('button', { name: /Purchase Orders/i });
  expect(within(ordersTab).getByText('2')).toBeInTheDocument();

  const vendorsTab = screen.getByRole('button', { name: /^Vendors/i });
  expect(within(vendorsTab).getByText('2')).toBeInTheDocument();
});

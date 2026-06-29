/**
 * Batch 4 — Purchasing PO table migrated to the shared DataTable.
 *
 * A light smoke test of the migration: the Purchase Orders table mounts through
 * DataTable (data-testid="data-table"), a sortable header re-orders the rows
 * client-side, and the CSV export control is wired up. Behavioral depth for the
 * sort engine itself lives in DataTable.test.tsx; this only proves Purchasing
 * is correctly driving it.
 */

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
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

const mockedApi = api as jest.Mocked<typeof api>;

// Distinct vendors so a sort on the Vendor column is observable. Default sort is
// po_number asc, so PO-1001 (Zulu) renders before PO-1002 (Acme) on mount.
const vendors = [
  { id: 1, code: 'VND-001', name: 'Zulu Works', is_approved: true, is_active: true, version: 0 },
  { id: 2, code: 'VND-002', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 },
];

const purchaseOrders = [
  {
    id: 10,
    po_number: 'PO-1001',
    vendor_id: 1,
    vendor_name: 'Zulu Works',
    status: 'draft',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 1250.5,
    line_count: 3,
  },
  {
    id: 11,
    po_number: 'PO-1002',
    vendor_id: 2,
    vendor_name: 'Acme Aerospace',
    status: 'sent',
    order_date: '2026-06-22',
    required_date: '2026-07-05',
    total: 980,
    line_count: 1,
  },
];

const renderPurchasing = () => render(<MemoryRouter><Purchasing /></MemoryRouter>);

// PO numbers in row order, read from the DataTable's first data column.
const poNumbersInOrder = (): string[] => {
  const table = screen.getByTestId('data-table');
  return within(table)
    .getAllByRole('row')
    .slice(1) // drop header
    .map((r) => within(r).getAllByRole('cell')[0].textContent?.trim() || '');
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders as any);
  mockedApi.getParts.mockResolvedValue([] as any);
});

test('Purchase Orders table renders through DataTable after load', async () => {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  expect(screen.getByTestId('data-table')).toBeInTheDocument();
  // Default sort is po_number asc.
  expect(poNumbersInOrder()).toEqual(['PO-1001', 'PO-1002']);
});

test('a sortable header reorders the rows client-side', async () => {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  // Scope to the table so the "Vendors" tab button isn't matched. The Vendor
  // column header is a sort button inside the DataTable.
  const table = screen.getByTestId('data-table');
  const vendorHeader = within(table).getByRole('button', { name: /Vendor/i });

  // Sort by Vendor: asc → Acme (PO-1002) before Zulu (PO-1001).
  fireEvent.click(vendorHeader);
  expect(poNumbersInOrder()).toEqual(['PO-1002', 'PO-1001']);

  // Toggle to desc → Zulu before Acme.
  fireEvent.click(vendorHeader);
  expect(poNumbersInOrder()).toEqual(['PO-1001', 'PO-1002']);
});

test('CSV export control is present on the migrated table', async () => {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  expect(screen.getByRole('button', { name: /Export CSV/i })).toBeInTheDocument();
});

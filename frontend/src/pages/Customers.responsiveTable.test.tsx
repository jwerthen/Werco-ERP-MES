/**
 * Customers — Batch 5 responsive-table migration coverage.
 *
 * The customer list was migrated onto the shared DataTable + mobileCards. This
 * locks the responsive contract the migration introduced:
 *
 *   1. the page renders via DataTable (data-testid="data-table"),
 *   2. a sortable header reorders the desktop rows,
 *   3. the CSV export control is present, and
 *   4. the md:hidden mobile-card layout renders the same row content.
 *
 * DataTable.mobileCards renders BOTH the desktop <table> and the mobile cards into
 * jsdom (CSS-hidden only), so each customer name appears twice. Row/sort lookups
 * are scoped to the desktop <table> (the only real <table>); the mobile assertion
 * is scoped to the md:hidden wrapper.
 */

import React from 'react';
import { render, screen, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Customers from './Customers';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getCustomers: jest.fn(),
    getCustomerStats: jest.fn(),
    createCustomer: jest.fn(),
    updateCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Deliberately NOT in code order so a Code-header sort visibly reorders rows.
const customers = [
  {
    id: 1, name: 'Beacon Aero', code: 'CUST-B', contact_name: 'Bo Reed', email: 'bo@beacon.test',
    city: 'Mesa', state: 'AZ', payment_terms: 'Net 30', requires_coc: true, requires_fai: false,
    is_active: true, created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2, name: 'Apex Mfg', code: 'CUST-A', contact_name: 'Ana Diaz', email: 'ana@apex.test',
    city: 'Reno', state: 'NV', payment_terms: 'Net 45', requires_coc: false, requires_fai: true,
    is_active: true, created_at: '2026-01-02T00:00:00Z',
  },
  {
    id: 3, name: 'Crest Systems', code: 'CUST-C', contact_name: 'Cal Howe', email: 'cal@crest.test',
    city: 'Tempe', state: 'AZ', payment_terms: 'Net 15', requires_coc: true, requires_fai: true,
    is_active: true, created_at: '2026-01-03T00:00:00Z',
  },
];

const CODES = ['CUST-A', 'CUST-B', 'CUST-C'];

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/customers']}>
      <Customers />
    </MemoryRouter>
  );
}

// First desktop body cell holds the (font-mono) customer code.
function getDesktopCodes(): string[] {
  const table = screen.getByTestId('data-table');
  const bodyRows = within(table).getAllByRole('row').slice(1); // drop header
  return bodyRows.map((r) => within(r).getAllByRole('cell')[0].textContent?.trim() || '');
}

describe('Customers — responsive DataTable (Batch 5)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getCustomers.mockResolvedValue(customers as any);
    mockedApi.getCustomerStats.mockResolvedValue({} as any);
  });

  it('renders the customer list through DataTable', async () => {
    renderPage();
    await screen.findAllByText('Apex Mfg');

    const table = screen.getByTestId('data-table');
    // All three customers present as desktop rows (header + 3 = 4 rows).
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    expect(within(table).getByText('Apex Mfg')).toBeInTheDocument();
    expect(within(table).getByText('Beacon Aero')).toBeInTheDocument();
    expect(within(table).getByText('Crest Systems')).toBeInTheDocument();
  });

  it('reorders rows when the sortable Code header is clicked', async () => {
    renderPage();
    await screen.findAllByText('Apex Mfg');

    const table = screen.getByTestId('data-table');
    const codeHeaderBtn = within(table).getByRole('button', { name: /^Code$/i });

    // Ascending by code.
    fireEvent.click(codeHeaderBtn);
    expect(getDesktopCodes()).toEqual([...CODES]);
    expect(codeHeaderBtn.closest('th')).toHaveAttribute('aria-sort', 'ascending');

    // Descending by code.
    fireEvent.click(codeHeaderBtn);
    expect(getDesktopCodes()).toEqual([...CODES].reverse());
    expect(codeHeaderBtn.closest('th')).toHaveAttribute('aria-sort', 'descending');
  });

  it('exposes the CSV export control', async () => {
    renderPage();
    await screen.findAllByText('Apex Mfg');

    expect(screen.getByRole('button', { name: /export csv/i })).toBeInTheDocument();
  });

  it('renders the md:hidden mobile-card layout with the same row content', async () => {
    const { container } = renderPage();
    await screen.findAllByText('Apex Mfg');

    const mobileWrapper = container.querySelector('.md\\:hidden');
    expect(mobileWrapper).not.toBeNull();
    const mobile = within(mobileWrapper as HTMLElement);

    // Each customer name renders as a mobile card title.
    expect(mobile.getByText('Apex Mfg')).toBeInTheDocument();
    expect(mobile.getByText('Beacon Aero')).toBeInTheDocument();
    expect(mobile.getByText('Crest Systems')).toBeInTheDocument();
    // The mobile layout is cards, not a table.
    expect(mobileWrapper!.querySelector('table')).toBeNull();
  });
});

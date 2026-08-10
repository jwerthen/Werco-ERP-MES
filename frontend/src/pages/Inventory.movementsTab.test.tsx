/**
 * Inventory → Stock Movements tab wiring.
 *
 * Two things are locked here, both of which are about the tab NOT lying:
 *
 *  1. The tab exists, and selecting it fetches the ledger (`GET
 *     /inventory/transactions`) rather than re-rendering the on-hand snapshot.
 *     Before this tab the endpoint had zero frontend callers, so an on-hand
 *     figure that moved had no visible cause anywhere in the app.
 *  2. The page-level quick-filter bar is HIDDEN on this tab. That bar is a
 *     client-side filter over the snapshot lists and its "Showing N of M items"
 *     counter is computed from `filteredSummary` / `filteredInventory` — neither
 *     of which has anything to do with the server-paged ledger rows on screen.
 *     Leaving it visible would show a count of one set above a table of another.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import api from '../services/api';
import InventoryPage from './Inventory';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getInventory: jest.fn(),
    getInventorySummary: jest.fn(),
    getInventoryLocations: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getParts: jest.fn(),
    getInventoryTransactions: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'manager', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const renderPage = () =>
  render(
    <MemoryRouter>
      <InventoryPage />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getInventory.mockResolvedValue([]);
  mockedApi.getInventorySummary.mockResolvedValue([
    {
      part_id: 500,
      part_number: 'SHT-0500',
      part_name: '0.125 CRS Sheet',
      total_on_hand: 40,
      total_allocated: 0,
      available: 40,
      locations: [{ location: 'A-1', quantity: 40 }],
    },
  ]);
  mockedApi.getInventoryLocations.mockResolvedValue([]);
  mockedApi.getLowStockAlerts.mockResolvedValue([]);
  // Full `Part`, not a partial — `getParts` is typed as `Part[]` and test files
  // are type-checked (tsconfig.test.json), so a short fixture is a compile error.
  mockedApi.getParts.mockResolvedValue([
    {
      id: 500,
      version: 1,
      part_number: 'SHT-0500',
      revision: 'A',
      name: '0.125 CRS Sheet',
      part_type: 'raw_material',
      unit_of_measure: 'each',
      standard_cost: 42.5,
      is_critical: false,
      requires_inspection: false,
      backflush_components: false,
      is_active: true,
      status: 'active',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]);
  mockedApi.getInventoryTransactions.mockResolvedValue([]);
});

describe('Inventory — Stock Movements tab', () => {
  it('loads the ledger when the tab is selected', async () => {
    renderPage();
    await screen.findByText('Stock Movements');

    expect(mockedApi.getInventoryTransactions).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText('Stock Movements'));

    await waitFor(() => expect(mockedApi.getInventoryTransactions).toHaveBeenCalledTimes(1));
    expect(mockedApi.getInventoryTransactions.mock.calls[0][0]).toMatchObject({ offset: 0 });
  });

  it('hides the snapshot quick-filter bar on the movements tab', async () => {
    renderPage();
    await screen.findByText('Stock Movements');

    // Present on the snapshot tabs...
    expect(screen.getByLabelText('Filter by part number or name')).toBeInTheDocument();
    expect(screen.getByText('All Inventory')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Stock Movements'));

    // ...and gone on the ledger tab, where it would filter nothing on screen.
    await waitFor(() =>
      expect(screen.queryByLabelText('Filter by part number or name')).not.toBeInTheDocument()
    );
    expect(screen.queryByText('All Inventory')).not.toBeInTheDocument();
    // The ledger's own filters take its place.
    expect(screen.getByText('Movement type')).toBeInTheDocument();
  });

  it('passes the loaded parts to the ledger part filter', async () => {
    renderPage();
    await screen.findByText('Stock Movements');
    fireEvent.click(screen.getByText('Stock Movements'));

    // The part filter only renders when the page actually handed a parts list
    // down — a regression that dropped the prop would silently lose the filter.
    await waitFor(() => expect(screen.getByText('Part')).toBeInTheDocument());
  });
});

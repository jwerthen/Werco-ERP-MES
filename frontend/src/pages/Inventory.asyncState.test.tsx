/**
 * Batch 3 — async-state standardization (Inventory).
 *
 * Locks the new load-failure / empty-result pattern on the Inventory page:
 *
 *   1. When the parallel data load rejects, the whole page renders the shared
 *      <ErrorState> (role="alert") instead of a blank screen, and clicking Retry
 *      re-runs loadData; on the retry's success the KPI strip / table render and
 *      the error block clears.
 *   2. When the load succeeds but the summary is empty, the Summary tab renders
 *      the shared <EmptyState> with its real title ("No inventory on hand"), not
 *      a bare "No inventory" string.
 *
 * Inventory pulls five endpoints in a single Promise.all (getInventory,
 * getInventorySummary, getParts, getInventoryLocations, getLowStockAlerts); a
 * rejection on any one trips loadError. The retry path resolves all five.
 * Inventory uses the default (no-op) Toast context, so no provider is needed
 * for these load-state assertions.
 */

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
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
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const summary = [
  {
    part_id: 7,
    part_number: 'PN-700',
    part_name: 'Bracket',
    total_on_hand: 40,
    total_allocated: 10,
    available: 30,
    locations: [{ location: 'A-1', quantity: 40 }],
  },
];

const parts = [{ id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'manufactured' }];

const renderPage = () => render(
  <MemoryRouter>
    <InventoryPage />
  </MemoryRouter>
);

/** Resolve all five load endpoints with the given summary payload. */
function mockLoadSuccess(summaryPayload: any[]) {
  mockedApi.getInventory.mockResolvedValue([] as any);
  mockedApi.getInventorySummary.mockResolvedValue(summaryPayload as any);
  mockedApi.getParts.mockResolvedValue(parts as any);
  mockedApi.getInventoryLocations.mockResolvedValue([] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue([] as any);
}

describe('Inventory async-state (Batch 3)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders ErrorState on load failure and recovers on Retry', async () => {
    // First load: the summary endpoint rejects → whole page shows ErrorState.
    mockedApi.getInventory.mockResolvedValue([] as any);
    mockedApi.getInventorySummary.mockRejectedValueOnce(new Error('boom'));
    mockedApi.getParts.mockResolvedValue(parts as any);
    mockedApi.getInventoryLocations.mockResolvedValue([] as any);
    mockedApi.getLowStockAlerts.mockResolvedValue([] as any);

    renderPage();

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Could not load inventory data.')).toBeInTheDocument();
    // The cockpit KPI strip is not on screen during the error state.
    expect(screen.queryByText('Unique Items')).not.toBeInTheDocument();

    // Retry: re-resolve every endpoint so loadData succeeds.
    mockLoadSuccess(summary);
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));

    // Content renders (KPI strip) and the error block clears.
    expect(await screen.findByText('Unique Items')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockedApi.getInventorySummary).toHaveBeenCalledTimes(2);
  });

  it('renders EmptyState with its title when the summary is empty', async () => {
    mockLoadSuccess([]);

    renderPage();

    // The Summary tab is the default; with no rows it shows the shared EmptyState.
    const empty = await screen.findByTestId('empty-state');
    expect(within(empty).getByText('No inventory on hand')).toBeInTheDocument();
    // It offers the Receive Inventory CTA.
    expect(
      within(empty).getByRole('button', { name: 'Receive Inventory' })
    ).toBeInTheDocument();
    // A successful empty load is not an error.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

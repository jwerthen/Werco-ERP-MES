/** Inventory owns the tab boundary; the actual register owns its server paging. */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import InventoryPage from './Inventory';
import { STOCK_PIECE_ADVISORY } from '../types/stockPiece';
import type { StockPiecePage, StockPieceSummary } from '../types/stockPiece';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getInventory: jest.fn(),
    getInventorySummary: jest.fn(),
    getInventoryLocations: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getParts: jest.fn(),
    getInventoryTransactions: jest.fn(),
    getStockPieces: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, company_id: 1, role: 'manager', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 1 } }) }));
const mockApi = api as jest.Mocked<typeof api>;
const summary = [
  {
    part_id: 7,
    part_number: 'SYNTHETIC-LEGACY-PART',
    part_name: 'Legacy aggregate stock',
    total_on_hand: 40,
    total_allocated: 10,
    available: 30,
    locations: [{ location: 'A1', quantity: 40 }],
  },
];
const observation: StockPieceSummary = {
  piece_id: 3,
  company_id: 1,
  label: 'SYNTHETIC-OBSERVATION',
  observation_number: 1,
  piece_version: 1,
  state: 'RECORDED',
  reason: 'Synthetic measured observation',
  observed_at: '2026-09-08T15:00:00Z',
  observer_name: 'Synthetic observer',
  created_at: '2026-09-08T16:00:00Z',
  created_by: 1,
  submitted_api_token_id: null,
  payload_schema_version: 1,
  payload_sha256: 'a'.repeat(64),
  payload_bytes: 500,
  source_inventory_item_id: 4,
  source_part_id: 7,
  source_sha256: 'b'.repeat(64),
  source_status: 'unchanged',
  current_source_sha256: 'b'.repeat(64),
  review_issues: [],
  advisory: STOCK_PIECE_ADVISORY,
};
function registerPage(page: number): StockPiecePage<StockPieceSummary> {
  return { company_id: 1, can_record: false, items: page === 1 ? [observation] : [], total: 21, page, per_page: 20 };
}
function LocationProbe() {
  return (
    <output aria-label="Current route">
      {useLocation().pathname}
      {useLocation().search}
    </output>
  );
}
function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/warehouse?tab=inventory&inventory_tab=observations']}>
      <InventoryPage />
      <LocationProbe />
    </MemoryRouter>
  );
}
function expectOnlyObservationContent() {
  expect(screen.getByRole('button', { name: 'Piece observations' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('region', { name: 'Piece observations' })).toBeInTheDocument();
  expect(screen.queryByText('Unique Items')).not.toBeInTheDocument();
  expect(screen.queryByText('Total On Hand')).not.toBeInTheDocument();
  expect(screen.queryByText('Total Available')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Filter by part number or name')).not.toBeInTheDocument();
  expect(screen.queryByText('All Inventory')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Receive Inventory/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Combine SKUs/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Transfer inventory' })).not.toBeInTheDocument();
  expect(screen.queryByText('Movement type')).not.toBeInTheDocument();
  expect(mockApi.getInventoryTransactions).not.toHaveBeenCalled();
}
beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getInventory.mockResolvedValue([]);
  mockApi.getInventorySummary.mockResolvedValue(summary);
  mockApi.getInventoryLocations.mockResolvedValue([]);
  mockApi.getLowStockAlerts.mockResolvedValue([]);
  mockApi.getParts.mockResolvedValue([]);
  mockApi.getInventoryTransactions.mockResolvedValue([]);
  mockApi.getStockPieces.mockImplementation(async page => registerPage(page ?? 1));
});

it('mounts the real server-paged observation register from the warehouse deep link while aggregate loading is pending', async () => {
  mockApi.getInventorySummary.mockImplementation(() => new Promise(() => undefined));
  renderPage();
  expect(await screen.findByRole('button', { name: observation.label })).toBeInTheDocument();
  expect(mockApi.getStockPieces).toHaveBeenCalledWith(1, expect.any(AbortSignal));
  expect(screen.queryByText('Loading inventory…')).not.toBeInTheDocument();
  expectOnlyObservationContent();
  const register = screen.getByRole('region', { name: 'Piece observations' });
  fireEvent.click(within(register).getByRole('button', { name: /Next page/i }));
  await waitFor(() => expect(mockApi.getStockPieces).toHaveBeenLastCalledWith(2, expect.any(AbortSignal)));
  expect(screen.getByLabelText('Current route')).toHaveTextContent('tab=inventory');
  expect(screen.getByLabelText('Current route')).toHaveTextContent('inventory_tab=observations');
  expect(screen.getByLabelText('Current route')).toHaveTextContent('piece_page=2');
});

it('keeps observations usable after aggregate fetching fails and preserves the existing summary error/retry behavior', async () => {
  mockApi.getInventorySummary
    .mockRejectedValueOnce(new Error('Synthetic aggregate failure'))
    .mockResolvedValue(summary);
  renderPage();
  expect(await screen.findByRole('button', { name: observation.label })).toBeInTheDocument();
  expectOnlyObservationContent();
  expect(screen.queryByText('Could not load inventory data.')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Summary by Part' }));
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent('Could not load inventory data.');
  expect(screen.queryByRole('region', { name: 'Piece observations' })).not.toBeInTheDocument();
  fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('Unique Items')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Summary by Part' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('button', { name: /Receive Inventory/ })).toBeInTheDocument();
  expect(screen.getByLabelText('Filter by part number or name')).toBeInTheDocument();
  expect(screen.getAllByText('SYNTHETIC-LEGACY-PART').length).toBeGreaterThan(0);
  expect(mockApi.getInventorySummary).toHaveBeenCalledTimes(2);
  expect(mockApi.getStockPieces).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('Current route')).toHaveTextContent('inventory_tab=summary');
});

it('shows the existing aggregate summary and write actions when switching back after a successful independent load', async () => {
  renderPage();
  await screen.findByRole('button', { name: observation.label });
  expectOnlyObservationContent();
  fireEvent.click(screen.getByRole('button', { name: 'Summary by Part' }));
  expect(await screen.findByText('Total On Hand')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Receive Inventory/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Combine SKUs/ })).toBeInTheDocument();
  expect(screen.getByLabelText('Filter by part number or name')).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Piece observations' })).not.toBeInTheDocument();
  expect(mockApi.getInventorySummary).toHaveBeenCalledTimes(1);
  expect(mockApi.getInventoryTransactions).not.toHaveBeenCalled();
});

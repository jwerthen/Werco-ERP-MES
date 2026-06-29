/**
 * Inventory cockpit — instrument-panel overhaul regression.
 *
 * The Inventory page was reworked to front a compact 4-up MiniStat strip
 * (Unique Items / Total On Hand / Total Available / Low Stock Alerts) and a
 * toolbar low-stock toggle. This locks that the KPI strip renders after the
 * initial data load and that flipping "Show Low Stock" swaps the toolbar
 * button for the live low-stock chip.
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

// Two manufactured parts on hand; part 7 is the one flagged low stock.
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
  {
    part_id: 8,
    part_number: 'PN-800',
    part_name: 'Plate',
    total_on_hand: 60,
    total_allocated: 0,
    available: 60,
    locations: [{ location: 'B-2', quantity: 60 }],
  },
];

const parts = [
  { id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'manufactured' },
  { id: 8, part_number: 'PN-800', name: 'Plate', part_type: 'manufactured' },
];

const lowStock = [{ part_id: 7, is_critical: true }];

const renderPage = () => render(<MemoryRouter><InventoryPage /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getInventory.mockResolvedValue([] as any);
  mockedApi.getInventorySummary.mockResolvedValue(summary as any);
  mockedApi.getInventoryLocations.mockResolvedValue([] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue(lowStock as any);
  mockedApi.getParts.mockResolvedValue(parts as any);
});

test('renders the 4-up MiniStat strip after the initial load', async () => {
  renderPage();

  // The KPI strip mounts only after loadData resolves.
  expect(await screen.findByText('Unique Items')).toBeInTheDocument();
  expect(screen.getByText('Total On Hand')).toBeInTheDocument();
  expect(screen.getByText('Total Available')).toBeInTheDocument();
  expect(screen.getByText('Low Stock Alerts')).toBeInTheDocument();

  // Values derived from the summary, asserted inside their own MiniStat tile so
  // unrelated occurrences of the same digits elsewhere on the page don't match.
  // 2 unique items, 100 on hand, 90 available, 1 low-stock alert.
  const tileFor = (label: string) => screen.getByText(label).closest('.card') as HTMLElement;
  expect(within(tileFor('Unique Items')).getByText('2')).toBeInTheDocument();
  expect(within(tileFor('Total On Hand')).getByText('100')).toBeInTheDocument();
  expect(within(tileFor('Total Available')).getByText('90')).toBeInTheDocument();
  expect(within(tileFor('Low Stock Alerts')).getByText('1')).toBeInTheDocument();
});

test('toggling Show Low Stock surfaces the low-stock chip in the toolbar', async () => {
  renderPage();

  const toggle = await screen.findByRole('button', { name: 'Show Low Stock' });
  // Before toggling, the live chip is not present.
  expect(screen.queryByText(/Showing 1 low stock/i)).toBeNull();

  fireEvent.click(toggle);

  // The toggle button is replaced by the amber low-stock chip…
  const chip = await screen.findByText(/Showing 1 low stock/i);
  expect(chip).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Show Low Stock' })).toBeNull();

  // …and the chip carries a clear-filter affordance.
  expect(screen.getByRole('button', { name: /Clear low stock filter/i })).toBeInTheDocument();
});

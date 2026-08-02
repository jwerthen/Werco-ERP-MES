/**
 * Inventory — breadcrumb mount behavior.
 *
 * The page serves three routes and one embed. This locks the mount contract:
 *   1. Bare /inventory (the hub) renders NO breadcrumb nav.
 *   2. /inventory/parts renders one, linking back up to /inventory.
 *   3. The Warehouse-tab embed (`embedded`) never renders one, even when the
 *      URL happens to be a sub-route — the embed doesn't own the URL.
 *
 * Mock scaffold mirrors Inventory.rbac.test.tsx.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
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

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'supervisor', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
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

const inventory = [
  {
    id: 21,
    part_id: 7,
    part: { id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'manufactured' },
    location: 'A-1',
    warehouse: 'MAIN',
    quantity_on_hand: 40,
    quantity_allocated: 10,
    quantity_available: 30,
    status: 'available',
    unit_cost: 1.5,
  },
];

const parts = [{ id: 7, part_number: 'PN-700', name: 'Bracket', part_type: 'manufactured' }];

const renderAt = (path: string, embedded = false) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <InventoryPage embedded={embedded || undefined} />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getInventory.mockResolvedValue(inventory as any);
  mockedApi.getInventorySummary.mockResolvedValue(summary as any);
  mockedApi.getInventoryLocations.mockResolvedValue([] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue([] as any);
  mockedApi.getParts.mockResolvedValue(parts as any);
});

test('the bare /inventory hub renders no breadcrumb nav', async () => {
  renderAt('/inventory');
  expect((await screen.findAllByText('PN-700')).length).toBeGreaterThan(0);

  expect(screen.queryByRole('navigation', { name: /breadcrumb/i })).toBeNull();
});

test('/inventory/parts renders one breadcrumb linking back to Inventory', async () => {
  renderAt('/inventory/parts');
  expect((await screen.findAllByText('PN-700')).length).toBeGreaterThan(0);

  const nav = screen.getByRole('navigation', { name: /breadcrumb/i });
  const parentLink = screen.getByRole('link', { name: 'Inventory' });
  expect(nav).toContainElement(parentLink);
  expect(parentLink).toHaveAttribute('href', '/inventory');
  expect(nav).toHaveTextContent('Part Inventory');
});

test('the embedded Warehouse-tab variant never renders a breadcrumb', async () => {
  renderAt('/inventory/parts', true);
  expect((await screen.findAllByText('PN-700')).length).toBeGreaterThan(0);

  expect(screen.queryByRole('navigation', { name: /breadcrumb/i })).toBeNull();
});

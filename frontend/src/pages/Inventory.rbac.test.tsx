/**
 * Inventory — RBAC gating of the stock-moving affordances.
 *
 * `POST /inventory/receive` and `POST /inventory/transfer` are Admin / Manager /
 * Supervisor per docs/RBAC_PERMISSIONS.md → Inventory. The page previously
 * rendered "Receive Inventory" and the per-row Transfer action unconditionally,
 * so every role holding `inventory:view` (operator / quality / shipping / viewer)
 * saw buttons it isn't allowed to use.
 *
 * This locks that:
 *   1. An authorized role (supervisor) still sees Receive + the Transfer row
 *      action and the "Actions" column that carries it.
 *   2. A view-only role (operator) sees neither, and the detail table drops the
 *      whole "Actions" column rather than rendering an empty one.
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

// Mutable mock user so each test can pick a role before rendering.
let mockUser: { id: number; role: string; is_superuser?: boolean } = {
  id: 1,
  role: 'supervisor',
  is_superuser: false,
};
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
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

const renderPage = () => render(<MemoryRouter><InventoryPage /></MemoryRouter>);

/** Switch to the "Detail by Location" tab, where the row action lives. */
const openDetailTab = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Detail by Location' }));
};

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getInventory.mockResolvedValue(inventory as any);
  mockedApi.getInventorySummary.mockResolvedValue(summary as any);
  mockedApi.getInventoryLocations.mockResolvedValue([] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue([] as any);
  mockedApi.getParts.mockResolvedValue(parts as any);
});

test('supervisor sees the Receive button and the per-row Transfer action', async () => {
  renderPage();

  expect(await screen.findByRole('button', { name: /Receive Inventory/i })).toBeInTheDocument();

  await openDetailTab();
  const table = screen.getByRole('table');
  expect(within(table).getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument();
  // Both surfaces carry the gated Transfer button and both render in jsdom (the
  // desktop table and the MobileDataCard), so assert the pair explicitly — the
  // negative case below is screen-level, and this keeps it from passing by
  // rendering neither surface.
  expect(within(table).getByRole('button', { name: 'Transfer inventory' })).toBeInTheDocument();
  expect(screen.getAllByRole('button', { name: 'Transfer inventory' })).toHaveLength(2);
});

test('operator sees neither Receive nor Transfer, and no empty Actions column', async () => {
  mockUser = { id: 2, role: 'operator', is_superuser: false };
  renderPage();

  // The page itself still loads for a view-only role (the part renders in both
  // the desktop table and the mobile card, hence findAllByText)…
  expect((await screen.findAllByText('PN-700')).length).toBeGreaterThan(0);
  // …but the stock-moving affordances are gone.
  expect(screen.queryByRole('button', { name: /Receive Inventory/i })).toBeNull();

  await openDetailTab();
  const table = screen.getByRole('table');
  expect(within(table).queryByRole('columnheader', { name: 'Actions' })).toBeNull();
  // Screen-level, not table-scoped: MobileDataCard renders its own gated Transfer
  // button, and the card renders alongside the table in jsdom (see above), so a
  // table-scoped query would miss a leak on the mobile surface.
  expect(screen.queryAllByRole('button', { name: 'Transfer inventory' })).toHaveLength(0);
});

test('superuser qualifies even on a role that lacks the permission', async () => {
  mockUser = { id: 3, role: 'operator', is_superuser: true };
  renderPage();

  expect(await screen.findByRole('button', { name: /Receive Inventory/i })).toBeInTheDocument();

  await openDetailTab();
  expect(
    within(screen.getByRole('table')).getByRole('button', { name: 'Transfer inventory' })
  ).toBeInTheDocument();
});

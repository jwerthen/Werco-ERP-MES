/**
 * Supplier Scorecards → Approved Supplier List is rebuilt on the VENDOR record: the tab and
 * its filter live in the URL (`?tab=asl&asl=overdue`, the Import Center card's target), and
 * the legacy approved_supplier_list table is no longer read.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import SupplierScorecards from './SupplierScorecards';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getSupplierScorecardsDashboard: jest.fn(),
    getSupplierScorecards: jest.fn(),
    getSupplierAudits: jest.fn(),
    getApprovedSuppliers: jest.fn(),
    getVendors: jest.fn(),
    getVendorsReevaluationDue: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: 'quality' } }) }));
const mockedApi = api as jest.Mocked<typeof api>;

let currentSearch = '';
function LocationProbe() {
  currentSearch = useLocation().search;
  return null;
}

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <SupplierScorecards />
      <LocationProbe />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getSupplierScorecardsDashboard.mockResolvedValue({});
  mockedApi.getSupplierScorecards.mockResolvedValue([]);
  mockedApi.getSupplierAudits.mockResolvedValue([]);
  mockedApi.getVendors.mockResolvedValue([
    { id: 1, code: 'VND-001', name: 'Cinder-Forge', is_approved: false, approval_status: 'conditional' },
  ]);
  mockedApi.getVendorsReevaluationDue.mockResolvedValue([
    {
      id: 1,
      code: 'VND-001',
      name: 'Cinder-Forge',
      is_approved: false,
      approval_status: 'conditional',
      reevaluation_due: '2024-12-10',
      days_overdue: 703,
    },
  ]);
});

test('deep-links to the ASL tab with the overdue filter and never reads the legacy table', async () => {
  renderAt('/supplier-scorecards?tab=asl&asl=overdue');
  const chip = await screen.findByRole('button', { name: /Re-evaluation overdue/ });
  expect(chip).toHaveAttribute('aria-pressed', 'true');
  expect(await screen.findAllByText('(703 d overdue)')).not.toHaveLength(0);
  expect(mockedApi.getApprovedSuppliers).not.toHaveBeenCalled();
});

test('the ASL filter and tab are written to the URL', async () => {
  renderAt('/supplier-scorecards');
  fireEvent.click(await screen.findByRole('button', { name: 'Approved Supplier List' }));
  fireEvent.click(await screen.findByRole('button', { name: /On the ASL/ }));
  await waitFor(() => expect(currentSearch).toBe('?tab=asl&asl=on'));
  fireEvent.click(screen.getByRole('button', { name: 'Audits' }));
  await waitFor(() => expect(currentSearch).toBe('?tab=audits'));
});

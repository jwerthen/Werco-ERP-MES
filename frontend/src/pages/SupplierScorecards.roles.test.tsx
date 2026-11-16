/**
 * Supplier Scorecards is reachable for the quality role (lead decision L2: the ASL's owner is
 * Purchasing / Quality). Quality reads every tab and keeps the ASL tab's own write controls, but
 * the scorecard and audit writes are Admin / Manager server-side
 * (`supplier_scorecards.py` require_role([ADMIN, MANAGER])), so it is offered no button that is a
 * guaranteed 403.
 */
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import SupplierScorecards from './SupplierScorecards';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getSupplierScorecardsDashboard: jest.fn(),
    getSupplierScorecards: jest.fn(),
    getSupplierAudits: jest.fn(),
    getVendors: jest.fn(),
    getVendorsReevaluationDue: jest.fn(),
  },
}));
const mockUser: { value: { id: number; role: string; is_superuser?: boolean } | null } = { value: null };
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser.value }) }));
const mockedApi = api as jest.Mocked<typeof api>;

const renderAs = (role: string, path = '/supplier-scorecards') => {
  mockUser.value = { id: 1, role };
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SupplierScorecards />
    </MemoryRouter>
  );
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getSupplierScorecardsDashboard.mockResolvedValue({});
  mockedApi.getSupplierScorecards.mockResolvedValue([]);
  mockedApi.getSupplierAudits.mockResolvedValue([]);
  mockedApi.getVendors.mockResolvedValue([
    { id: 1, code: 'VND-001', name: 'Cinder-Forge', is_approved: true, approval_status: 'approved' },
  ]);
  mockedApi.getVendorsReevaluationDue.mockResolvedValue([]);
});

afterEach(() => {
  mockUser.value = null;
});

test('quality reads the page and gets no scorecard or audit write button', async () => {
  renderAs('quality');
  expect(await screen.findAllByText('No scorecards found')).not.toHaveLength(0);
  expect(screen.queryByRole('button', { name: /New Scorecard/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Audits' }));
  expect(await screen.findAllByText('No audits found')).not.toHaveLength(0);
  expect(screen.queryByRole('button', { name: /New Audit/ })).not.toBeInTheDocument();
  expect(mockedApi.getSupplierScorecardsDashboard).toHaveBeenCalled();
});

test('quality keeps the ASL tab and its Update approval control', async () => {
  renderAs('quality', '/supplier-scorecards?tab=asl');
  expect(await screen.findAllByRole('button', { name: /Update approval for Cinder-Forge/ })).not.toHaveLength(0);
  expect(screen.getAllByRole('button', { name: /Import ASL/ })).not.toHaveLength(0);
  expect(mockedApi.getVendors).toHaveBeenCalled();
});

test.each(['admin', 'manager'])('%s still gets the scorecard and audit write buttons', async role => {
  renderAs(role);
  expect(await screen.findAllByRole('button', { name: /New Scorecard/ })).not.toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Audits' }));
  expect(await screen.findAllByRole('button', { name: /New Audit/ })).not.toHaveLength(0);
});

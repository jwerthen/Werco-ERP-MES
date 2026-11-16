/**
 * Customers → customer detail: the Export control panel is ADMIN only.
 *
 * Every endpoint behind it is `require_role([ADMIN])` server-side, which also
 * passes platform admins and superusers. The detail view must render the panel
 * for exactly that set — and never call the endpoint for anyone else, so a
 * Manager is not shown a guaranteed 403.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Customers from './Customers';

const mockAuthUser: { role: string; is_superuser: boolean } = { role: 'admin', is_superuser: false };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getCustomers: jest.fn(),
    getCustomerStats: jest.fn(),
    createCustomer: jest.fn(),
    updateCustomer: jest.fn(),
    getCustomerExportControl: jest.fn(),
    createCustomerContract: jest.fn(),
    updateCustomerContract: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const CUSTOMER = {
  id: 7,
  name: 'Acme Aero',
  code: 'ACME',
  payment_terms: 'Net 30',
  requires_coc: true,
  requires_fai: false,
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/customers?id=7']}>
      <Customers />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getCustomers.mockResolvedValue([CUSTOMER]);
  mockedApi.getCustomerStats.mockResolvedValue({
    customer_id: 7,
    customer_name: 'Acme Aero',
    part_count: 0,
    work_order_counts: { total: 0, by_status: {} },
    parts: [],
    assemblies: [],
    current_work_orders: [],
    past_work_orders: [],
    recent_work_orders: [],
  });
  mockedApi.getCustomerExportControl.mockResolvedValue({
    customer_id: 7,
    customer_name: 'Acme Aero',
    customer_is_deleted: false,
    controlled: false,
    data_classification: null,
    controlling_contract_id: null,
    contracts: [],
  });
});

describe('Customers detail — export control gating', () => {
  it.each([
    ['admin', false],
    ['platform_admin', false],
    ['manager', true],
  ])('renders the panel for role %s (superuser=%s)', async (role, isSuperuser) => {
    mockAuthUser.role = role;
    mockAuthUser.is_superuser = isSuperuser;
    renderDetail();

    expect(await screen.findByTestId('export-control-panel')).toBeInTheDocument();
    expect(await screen.findByTestId('export-control-badge')).toHaveTextContent('Not controlled');
    expect(mockedApi.getCustomerExportControl).toHaveBeenCalledWith(7);
  });

  it.each(['manager', 'supervisor', 'quality', 'viewer'])('hides the panel from %s', async role => {
    mockAuthUser.role = role;
    mockAuthUser.is_superuser = false;
    renderDetail();

    await screen.findByRole('button', { name: /^Edit Customer$/ });
    await waitFor(() => expect(mockedApi.getCustomerStats).toHaveBeenCalled());
    expect(screen.queryByTestId('export-control-panel')).not.toBeInTheDocument();
    expect(mockedApi.getCustomerExportControl).not.toHaveBeenCalled();
  });
});

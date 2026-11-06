/**
 * CustomerComplaints — RBAC gating of the record controls.
 *
 * `POST /complaints/complaints/` and `POST /complaints/rma/` are the server's AUTHOR
 * tier (Admin / Manager / Supervisor / Quality = `quality:inspect`). The page is
 * reachable with `quality:view`, which Operator and Viewer also hold, and it used to
 * render "New Complaint" (header + empty state) and "Create RMA" for all of them —
 * controls the server now refuses with 403.
 *
 * This locks that an author role sees all three, and a view-only role sees none.
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CustomerComplaints from './CustomerComplaints';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getComplaints: jest.fn(),
    getComplaintsDashboard: jest.fn(),
    createComplaint: jest.fn(),
    createRMA: jest.fn(),
  },
}));

// Mutable mock user so each test can pick a role before rendering.
let mockUser: { id: number; role: string; is_superuser?: boolean } = {
  id: 1,
  role: 'quality',
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

const complaint = {
  id: 7,
  complaint_number: 'CC-2026-007',
  customer_name: 'Acme Aerospace',
  customer_contact: null,
  customer_po_number: null,
  lot_number: null,
  serial_number: null,
  quantity_affected: 3,
  severity: 'minor',
  status: 'received',
  title: 'Dented brackets',
  description: 'Three brackets arrived dented on one flange.',
  date_received: '2026-07-30',
  estimated_cost: 0,
  rmas: [],
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/customer-complaints']}>
      <CustomerComplaints />
    </MemoryRouter>
  );

async function expandComplaint() {
  const cell = (await screen.findAllByText('CC-2026-007')).find(el => el.closest('td') !== null) as HTMLElement;
  fireEvent.click(cell);
  // The expanded panel always carries its "Full Details" control.
  await screen.findByRole('button', { name: /full details/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getComplaintsDashboard.mockResolvedValue({
    open_complaints: 0,
    avg_resolution_days: null,
    by_severity: {},
    by_customer: [],
    satisfaction_rate: null,
    trend: [],
    open_rmas: 0,
  } as any);
});

describe.each(['admin', 'manager', 'supervisor', 'quality'])('author role %s', role => {
  beforeEach(() => {
    mockUser = { id: 1, role, is_superuser: false };
  });

  it('sees New Complaint in the header and the empty state', async () => {
    mockedApi.getComplaints.mockResolvedValue([] as any);
    renderPage();
    await screen.findAllByText(/no complaints found/i);
    // Header button plus the empty-state action (rendered by the table and the mobile list).
    expect(screen.getAllByRole('button', { name: /new complaint/i }).length).toBeGreaterThanOrEqual(2);
  });

  it('sees Create RMA on an expanded complaint', async () => {
    mockedApi.getComplaints.mockResolvedValue([complaint] as any);
    renderPage();
    await expandComplaint();
    expect(screen.getByRole('button', { name: /create rma/i })).toBeInTheDocument();
  });
});

describe.each(['operator', 'viewer'])('view-only role %s', role => {
  beforeEach(() => {
    mockUser = { id: 1, role, is_superuser: false };
  });

  it('sees no New Complaint control, in the header or the empty state', async () => {
    mockedApi.getComplaints.mockResolvedValue([] as any);
    renderPage();
    await screen.findAllByText(/no complaints found/i);
    expect(screen.queryByRole('button', { name: /new complaint/i })).not.toBeInTheDocument();
  });

  it('sees no Create RMA control on an expanded complaint', async () => {
    mockedApi.getComplaints.mockResolvedValue([complaint] as any);
    renderPage();
    await expandComplaint();
    expect(screen.queryByRole('button', { name: /create rma/i })).not.toBeInTheDocument();
  });
});

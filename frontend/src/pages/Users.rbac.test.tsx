/**
 * RBAC gating of the Users page write controls.
 *
 * Backend: every write in users.py is require_role([ADMIN]); GET /users is
 * ADMIN/MANAGER. Frontend permissions.ts was tightened so a manager keeps the
 * read-only list (users:view) but LOST users:create/users:edit, and a supervisor
 * has no users:* at all. Users.tsx gates Add User / Import CSV / Print Badges and
 * the per-row Edit / Reset Password / Activate-Deactivate controls (plus the
 * DataTable selection) behind canManageUsers.
 *
 * This locks: an admin sees the write affordances; a manager sees the read-only
 * user list but NONE of the write affordances (so the UI never offers a
 * guaranteed-403 action).
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Users from './Users';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
    getPendingUserApprovals: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string; is_superuser: boolean } = {
  id: 99,
  role: 'admin',
  is_superuser: false,
};

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const USER_1 = {
  id: 1,
  email: 'rosa@werco.test',
  employee_id: '40231',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'operator',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

function renderUsers() {
  return render(
    <MemoryRouter initialEntries={['/users']}>
      <Users />
    </MemoryRouter>
  );
}

describe('Users page write-control RBAC', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getUsers.mockResolvedValue([USER_1]);
    mockedApi.getPendingUserApprovals.mockResolvedValue([]);
  });

  it('admin sees the write affordances (Add User / Import CSV / Edit / Reset Password)', async () => {
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };

    renderUsers();
    // The list renders a desktop <table> and a parallel mobile-card list, so
    // per-row controls appear twice in jsdom; assert on the count, not a single node.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.getByRole('button', { name: 'Add User' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Import CSV/ })).toBeInTheDocument();
    expect(screen.getAllByLabelText('Edit user').length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText('Reset Password').length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText('Deactivate user').length).toBeGreaterThan(0);
  });

  it('manager sees the read-only list but NONE of the write affordances', async () => {
    mockAuthUser = { id: 99, role: 'manager', is_superuser: false };

    renderUsers();
    // Manager keeps users:view, so the list still renders.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.queryByRole('button', { name: 'Add User' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Import CSV/ })).not.toBeInTheDocument();
    expect(screen.queryAllByLabelText('Edit user')).toHaveLength(0);
    expect(screen.queryAllByLabelText('Reset Password')).toHaveLength(0);
    expect(screen.queryAllByLabelText('Deactivate user')).toHaveLength(0);
  });
});

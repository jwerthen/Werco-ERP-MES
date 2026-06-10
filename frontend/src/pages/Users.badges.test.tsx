/**
 * A0.4 badge printing affordances on the Users page.
 *
 * Locks:
 * 1. The select-all checkbox is MEMBERSHIP-based (checked iff every visible user
 *    is selected) and selections are PRUNED when the users list refetches, so a
 *    stale id can never reach the badge sheet.
 * 2. The "Print Badges" button is admin/manager-only (canManageUsers) — the badge
 *    sheet's GET /users fetch is server-enforced to ADMIN/MANAGER, so supervisors
 *    (users:view) must not be offered a guaranteed-403 flow.
 */

import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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

const USER_2 = {
  id: 2,
  email: 'sam@werco.test',
  employee_id: '40232',
  first_name: 'Sam',
  last_name: 'Lee',
  role: 'quality',
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

describe('Users badge printing', () => {
  let openSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
    mockedApi.getUsers.mockResolvedValue([USER_1, USER_2]);
    mockedApi.getPendingUserApprovals.mockResolvedValue([]);
    openSpy = jest.spyOn(window, 'open').mockImplementation(() => null);
  });

  afterEach(() => {
    openSpy.mockRestore();
  });

  it('select-all is membership-based and selections are pruned when the list refetches', async () => {
    // Initial load (includeInactive=false) has two users; the refetch
    // (includeInactive=true) returns only one — simulating any refetch that
    // drops a previously selected user.
    mockedApi.getUsers.mockImplementation(async (includeInactive?: boolean) =>
      includeInactive ? [USER_1] : [USER_1, USER_2]
    );

    renderUsers();
    await waitFor(() => expect(screen.getByText('Rosa Vega')).toBeInTheDocument());

    const selectAll = screen.getByLabelText('Select all users for badge printing') as HTMLInputElement;
    fireEvent.click(selectAll);
    expect(selectAll.checked).toBe(true);
    expect(screen.getByRole('button', { name: /Print Badges \(2\)/ })).toBeInTheDocument();

    // Trigger a refetch that no longer contains USER_2.
    fireEvent.click(screen.getByLabelText('Show inactive users'));
    await waitFor(() => expect(screen.queryByText('Sam Lee')).not.toBeInTheDocument());

    // Stale id pruned; select-all stays checked because every VISIBLE user is selected.
    expect(screen.getByRole('button', { name: /Print Badges \(1\)/ })).toBeInTheDocument();
    expect((screen.getByLabelText('Select all users for badge printing') as HTMLInputElement).checked).toBe(true);

    // The badge sheet only ever receives ids that are still in the list.
    fireEvent.click(screen.getByRole('button', { name: /Print Badges \(1\)/ }));
    expect(openSpy).toHaveBeenCalledWith('/print/badges?user_ids=1', '_blank');
  });

  it('select-all reflects partial selection after new users appear in a refetch', async () => {
    mockedApi.getUsers.mockImplementation(async (includeInactive?: boolean) =>
      includeInactive ? [USER_1, USER_2] : [USER_1]
    );

    renderUsers();
    await waitFor(() => expect(screen.getByText('Rosa Vega')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Select all users for badge printing'));
    fireEvent.click(screen.getByLabelText('Show inactive users'));
    await waitFor(() => expect(screen.getByText('Sam Lee')).toBeInTheDocument());

    // Only 1 of the 2 now-visible users is selected: select-all must be unchecked.
    expect((screen.getByLabelText('Select all users for badge printing') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole('button', { name: /Print Badges \(1\)/ })).toBeInTheDocument();
  });

  it.each(['admin', 'manager'])('shows the Print Badges button for %s', async (role) => {
    mockAuthUser = { id: 99, role, is_superuser: false };

    renderUsers();
    await waitFor(() => expect(screen.getByText('Rosa Vega')).toBeInTheDocument());

    expect(screen.getByRole('button', { name: /Print Badges/ })).toBeInTheDocument();
  });

  it('hides the Print Badges button for supervisors (server-side GET /users is ADMIN/MANAGER-only)', async () => {
    mockAuthUser = { id: 99, role: 'supervisor', is_superuser: false };

    renderUsers();
    await waitFor(() => expect(screen.getByText('Rosa Vega')).toBeInTheDocument());

    expect(screen.queryByRole('button', { name: /Print Badges/ })).not.toBeInTheDocument();
  });
});

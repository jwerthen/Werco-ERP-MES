/**
 * A0.4 badge printing affordances on the Users page.
 *
 * Locks:
 * 1. The select-all checkbox is MEMBERSHIP-based (checked iff every visible user
 *    is selected) and selections are PRUNED when the users list refetches, so a
 *    stale id can never reach the badge sheet.
 * 2. The "Print Badges" button (like the badge-selection checkboxes it depends on)
 *    is gated behind canManageUsers, which is now Admin-only. Managers keep the
 *    read-only user list (users:view) but lost users:create/users:edit, so
 *    canManageUsers is false for them — the badge print/select affordances are
 *    admin-only, and managers/supervisors do not see the button.
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
    // The user list renders both a desktop <table> and a parallel mobile-card
    // list (DataTable.mobileCards), so each name appears twice in jsdom; assert
    // on presence/absence across all matches rather than a single element.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    // Badge selection is backed by DataTable's selection prop; the select-all
    // control is the shared table-header checkbox ("Select all rows").
    const selectAll = screen.getByLabelText('Select all rows') as HTMLInputElement;
    fireEvent.click(selectAll);
    expect(selectAll.checked).toBe(true);
    expect(screen.getByRole('button', { name: /Print Badges \(2\)/ })).toBeInTheDocument();

    // Trigger a refetch that no longer contains USER_2.
    fireEvent.click(screen.getByLabelText('Show inactive users'));
    await waitFor(() => expect(screen.queryAllByText('Sam Lee')).toHaveLength(0));

    // Stale id pruned; select-all stays checked because every VISIBLE user is selected.
    expect(screen.getByRole('button', { name: /Print Badges \(1\)/ })).toBeInTheDocument();
    expect((screen.getByLabelText('Select all rows') as HTMLInputElement).checked).toBe(true);

    // The badge sheet only ever receives ids that are still in the list.
    fireEvent.click(screen.getByRole('button', { name: /Print Badges \(1\)/ }));
    expect(openSpy).toHaveBeenCalledWith('/print/badges?user_ids=1', '_blank');
  });

  it('select-all reflects partial selection after new users appear in a refetch', async () => {
    mockedApi.getUsers.mockImplementation(async (includeInactive?: boolean) =>
      includeInactive ? [USER_1, USER_2] : [USER_1]
    );

    renderUsers();
    // The user list renders both a desktop <table> and a parallel mobile-card
    // list (DataTable.mobileCards), so each name appears twice in jsdom; assert
    // on presence/absence across all matches rather than a single element.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    fireEvent.click(screen.getByLabelText('Select all rows'));
    fireEvent.click(screen.getByLabelText('Show inactive users'));
    await waitFor(() => expect(screen.getAllByText('Sam Lee').length).toBeGreaterThan(0));

    // Only 1 of the 2 now-visible users is selected: select-all must be unchecked.
    expect((screen.getByLabelText('Select all rows') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole('button', { name: /Print Badges \(1\)/ })).toBeInTheDocument();
  });

  it('shows the Print Badges button for admin', async () => {
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };

    renderUsers();
    // The user list renders both a desktop <table> and a parallel mobile-card
    // list (DataTable.mobileCards), so each name appears twice in jsdom; assert
    // on presence/absence across all matches rather than a single element.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.getByRole('button', { name: /Print Badges/ })).toBeInTheDocument();
  });

  // Badge print/select is admin-only now (canManageUsers). Managers keep the
  // read-only list but lost users:create/users:edit, so they no longer see the
  // Print Badges button; supervisors never had users:* access.
  it.each(['manager', 'supervisor'])('hides the Print Badges button for %s', async (role) => {
    mockAuthUser = { id: 99, role, is_superuser: false };

    renderUsers();
    // The user list renders both a desktop <table> and a parallel mobile-card
    // list (DataTable.mobileCards), so each name appears twice in jsdom; assert
    // on presence/absence across all matches rather than a single element.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.queryByRole('button', { name: /Print Badges/ })).not.toBeInTheDocument();
  });
});

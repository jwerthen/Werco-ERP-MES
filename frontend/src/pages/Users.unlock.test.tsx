/**
 * Failed-login lockout affordances on the Users page.
 *
 * Locks:
 * 1. A user whose `locked_until` sits in the FUTURE renders the red "Locked"
 *    StatusBadge and (for admins) the Unlock row action.
 * 2. Unlock is NON-optimistic: it calls api.unlockUser and then reloads the
 *    list — the badge only clears when the server's reload says so.
 * 3. A stale/past `locked_until` (the 30-minute timer already lapsed) renders
 *    NO badge and NO unlock action — the account is not locked anymore.
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
    unlockUser: jest.fn(),
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

const FUTURE_LOCK = new Date(Date.now() + 25 * 60 * 1000).toISOString();
const PAST_LOCK = new Date(Date.now() - 5 * 60 * 1000).toISOString();

const LOCKED_USER = {
  id: 1,
  email: 'rosa@werco.test',
  employee_id: '40231',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'operator',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  locked_until: FUTURE_LOCK,
};

const UNLOCKED_USER = {
  id: 2,
  email: 'sam@werco.test',
  employee_id: '40232',
  first_name: 'Sam',
  last_name: 'Lee',
  role: 'quality',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  locked_until: null,
};

function renderUsers() {
  return render(
    <MemoryRouter initialEntries={['/users']}>
      <Users />
    </MemoryRouter>
  );
}

describe('Users failed-login lockout', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
    mockedApi.getPendingUserApprovals.mockResolvedValue([]);
    mockedApi.unlockUser.mockResolvedValue({ message: 'User unlocked' });
  });

  it('renders the red Locked badge and the Unlock action for a future locked_until', async () => {
    mockedApi.getUsers.mockResolvedValue([LOCKED_USER, UNLOCKED_USER]);

    renderUsers();
    // The list renders both a desktop <table> and a parallel mobile-card list
    // (DataTable.mobileCards), so each locked affordance appears twice in jsdom;
    // assert on presence across all matches rather than a single element.
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.getAllByText('locked').length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText('Unlock user').length).toBeGreaterThan(0);
  });

  it('unlock calls api.unlockUser and reloads the list (non-optimistic)', async () => {
    mockedApi.getUsers.mockResolvedValue([LOCKED_USER]);

    renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));
    expect(mockedApi.getUsers).toHaveBeenCalledTimes(1);

    // The reload returns the user unlocked; the badge must clear only via that reload.
    mockedApi.getUsers.mockResolvedValue([{ ...LOCKED_USER, locked_until: null }]);
    fireEvent.click(screen.getAllByLabelText('Unlock user')[0]);

    await waitFor(() => expect(mockedApi.unlockUser).toHaveBeenCalledWith(1));
    await waitFor(() => expect(mockedApi.getUsers).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryAllByText('locked')).toHaveLength(0));
    expect(screen.queryAllByLabelText('Unlock user')).toHaveLength(0);
  });

  it('renders no badge and no unlock action for a past locked_until', async () => {
    mockedApi.getUsers.mockResolvedValue([{ ...LOCKED_USER, locked_until: PAST_LOCK }, UNLOCKED_USER]);

    renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.queryAllByText('locked')).toHaveLength(0);
    expect(screen.queryAllByLabelText('Unlock user')).toHaveLength(0);
  });
});

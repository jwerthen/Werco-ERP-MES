/**
 * Users → locked floor PINs, listed above the table.
 *
 * The `floor_pin.lockout` notification (in-app, to Admins, when wrong PINs start a lock of
 * 24 h or more) links record-less to `/users` (backend notification_links.USERS_ADMIN). The
 * landing therefore has to make the person it is about findable without scanning the list:
 * every PIN locked right now is listed with its end time and the Manage action — or, for a
 * Manager, the reason they may not manage that account. An expired lock is not listed, and
 * nothing is listed for a viewer who cannot manage floor PINs.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Users from './Users';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
    getPendingUserApprovals: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string; is_superuser: boolean } = { id: 99, role: 'admin', is_superuser: false };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const inHours = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString();

const base = { is_active: true, created_at: '2026-01-01T00:00:00Z', has_floor_pin: true };
const ROSA = {
  ...base,
  id: 1,
  email: 'rosa@werco.test',
  employee_id: '4217',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'operator',
  floor_pin_locked_until: inHours(48),
};
const LI = {
  ...base,
  id: 2,
  email: 'li@werco.test',
  employee_id: '5310',
  first_name: 'Li',
  last_name: 'Chen',
  role: 'quality',
  floor_pin_locked_until: inHours(24),
};
const SAM = {
  ...base,
  id: 3,
  email: 'sam@werco.test',
  employee_id: '7702',
  first_name: 'Sam',
  last_name: 'Ortiz',
  role: 'operator',
  floor_pin_locked_until: inHours(-1),
};

function renderUsers() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/users']}>
        <Users />
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getUsers.mockResolvedValue([ROSA, LI, SAM]);
  mockedApi.getPendingUserApprovals.mockResolvedValue([]);
});

describe('Users — locked floor PINs above the table', () => {
  it('an Admin sees every PIN locked right now, with its end time and Manage', async () => {
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
    renderUsers();

    const section = await screen.findByRole('region', { name: 'Locked floor PINs' });
    expect(within(section).getByText('2 floor PINs are locked after repeated wrong PINs')).toBeInTheDocument();
    expect(within(section).getByText('Rosa Vega')).toBeInTheDocument();
    expect(within(section).getByText('Li Chen')).toBeInTheDocument();
    // An expired lock is not a lock.
    expect(within(section).queryByText('Sam Ortiz')).not.toBeInTheDocument();
    expect(within(section).getAllByText(/^until /)).toHaveLength(2);

    fireEvent.click(within(section).getByRole('button', { name: 'Manage floor PIN for Rosa Vega' }));
    expect(await screen.findByRole('heading', { name: /Floor PIN — Rosa Vega/ })).toBeInTheDocument();
  });

  it('a Manager sees the list too, with Admin only on an account outside their scope', async () => {
    mockAuthUser = { id: 99, role: 'manager', is_superuser: false };
    renderUsers();

    const section = await screen.findByRole('region', { name: 'Locked floor PINs' });
    expect(within(section).getByRole('button', { name: 'Manage floor PIN for Rosa Vega' })).toBeInTheDocument();
    expect(within(section).queryByRole('button', { name: 'Manage floor PIN for Li Chen' })).not.toBeInTheDocument();
    expect(within(section).getByText('Admin only')).toBeInTheDocument();
  });

  it('nothing is listed when no PIN is locked, or for a role that cannot manage floor PINs', async () => {
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
    mockedApi.getUsers.mockResolvedValue([SAM]);
    const { unmount } = renderUsers();
    await waitFor(() => expect(screen.getAllByText('Sam Ortiz').length).toBeGreaterThan(0));
    expect(screen.queryByRole('region', { name: 'Locked floor PINs' })).not.toBeInTheDocument();
    unmount();

    mockAuthUser = { id: 99, role: 'supervisor', is_superuser: false };
    mockedApi.getUsers.mockResolvedValue([ROSA]);
    renderUsers();
    await waitFor(() => expect(mockedApi.getUsers).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Locked floor PINs' })).not.toBeInTheDocument();
  });
});

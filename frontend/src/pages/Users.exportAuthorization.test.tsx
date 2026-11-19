/**
 * Users → Export-authorized (AS9100D 7.5.3.1, Admin only).
 *
 *   1. the column (state, who granted it and when, Central time) is Admin-only;
 *      a Manager's read-only list never shows it;
 *   2. grant / revoke needs a reason and sends PUT /users/{id}/export-authorization
 *      with the flipped flag;
 *   3. non-optimistic: the list reloads on success; a refusal is shown verbatim
 *      in the dialog and nothing reloads.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import Users from './Users';
import { formatCentralDateTime } from '../utils/centralTime';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
    getPendingUserApprovals: jest.fn(),
    setUserExportAuthorization: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string; is_superuser: boolean } = { id: 99, role: 'admin', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const GRANTED_AT = '2026-11-18T15:30:00Z';
const ADMIN = {
  id: 99,
  email: 'ana@werco.test',
  employee_id: '1001',
  first_name: 'Ana',
  last_name: 'Ruiz',
  role: 'admin',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  export_authorized: false,
};
const AUTHORIZED = {
  id: 1,
  email: 'rosa@werco.test',
  employee_id: '40231',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'quality',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  export_authorized: true,
  export_authorized_at: GRANTED_AT,
  export_authorized_by: 99,
};
const NOT_AUTHORIZED = {
  id: 2,
  email: 'sam@werco.test',
  employee_id: '40232',
  first_name: 'Sam',
  last_name: 'Lee',
  role: 'operator',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  export_authorized: false,
};

/** The table once the user list has landed (it renders a loading state first). */
async function findLoadedTable(): Promise<HTMLElement> {
  const table = await screen.findByRole('table');
  await within(table).findByText('Rosa Vega');
  return table;
}

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
  mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
  mockedApi.getUsers.mockResolvedValue([ADMIN, AUTHORIZED, NOT_AUTHORIZED]);
  mockedApi.getPendingUserApprovals.mockResolvedValue([]);
});

it('an Admin sees who granted export authorization and when, in Central time', async () => {
  renderUsers();
  const table = await findLoadedTable();
  expect(within(table).getByRole('columnheader', { name: /Export-authorized/ })).toBeInTheDocument();
  expect(within(table).getByText(`by Ana Ruiz · ${formatCentralDateTime(GRANTED_AT)}`)).toBeInTheDocument();
  expect(within(table).getByRole('button', { name: 'Revoke export authorization for Rosa Vega' })).toBeInTheDocument();
  expect(within(table).getByRole('button', { name: 'Grant export authorization for Sam Lee' })).toBeInTheDocument();
});

it('a Manager never sees the export-authorization column', async () => {
  mockAuthUser = { id: 50, role: 'manager', is_superuser: false };
  renderUsers();
  await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));
  expect(screen.queryByRole('columnheader', { name: /Export-authorized/ })).not.toBeInTheDocument();
  expect(screen.queryAllByRole('button', { name: /export authorization for/ })).toHaveLength(0);
});

it('granting requires a reason, sends the flipped flag and reloads the list', async () => {
  mockedApi.setUserExportAuthorization.mockResolvedValue({ ...NOT_AUTHORIZED, export_authorized: true } as any);
  renderUsers();
  const table = await findLoadedTable();
  fireEvent.click(within(table).getByRole('button', { name: 'Grant export authorization for Sam Lee' }));
  const dialog = await screen.findByRole('dialog', { name: /Export authorization — Sam Lee/ });

  fireEvent.click(within(dialog).getByRole('button', { name: 'Grant authorization' }));
  expect(await within(dialog).findByText(/Give a reason/)).toBeInTheDocument();
  expect(mockedApi.setUserExportAuthorization).not.toHaveBeenCalled();

  fireEvent.change(within(dialog).getByLabelText(/Reason for granting/), {
    target: { value: ' Reviews customer ITAR drawings ' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Grant authorization' }));
  await waitFor(() =>
    expect(mockedApi.setUserExportAuthorization).toHaveBeenCalledWith(2, {
      export_authorized: true,
      reason: 'Reviews customer ITAR drawings',
    })
  );
  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Export authorization/ })).not.toBeInTheDocument());
  expect(mockedApi.getUsers).toHaveBeenCalledTimes(2);
  expect(await screen.findByText('Sam Lee is now export-authorized')).toBeInTheDocument();
});

it('a refused revoke is shown verbatim and nothing reloads', async () => {
  mockedApi.setUserExportAuthorization.mockRejectedValue({
    response: { status: 403, data: { detail: 'An API token cannot change export authorization.' } },
  });
  renderUsers();
  const table = await findLoadedTable();
  fireEvent.click(within(table).getByRole('button', { name: 'Revoke export authorization for Rosa Vega' }));
  const dialog = await screen.findByRole('dialog', { name: /Export authorization — Rosa Vega/ });
  expect(within(dialog).getByText('Ana Ruiz')).toBeInTheDocument();

  fireEvent.change(within(dialog).getByLabelText(/Reason for revoking/), { target: { value: 'Left the program' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke authorization' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'An API token cannot change export authorization.'
  );
  expect(mockedApi.setUserExportAuthorization).toHaveBeenCalledWith(1, {
    export_authorized: false,
    reason: 'Left the program',
  });
  expect(mockedApi.getUsers).toHaveBeenCalledTimes(1);
});

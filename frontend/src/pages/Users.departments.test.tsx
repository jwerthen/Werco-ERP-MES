/**
 * Department is a pick list (plan alerts_health/2 step 5).
 *
 * Locks:
 * 1. The Add/Edit form offers the server's list (GET /users/departments) as a select, not
 *    free text, with a blank "No department" option.
 * 2. A user whose stored value is not on the list keeps it as a temporary, flagged option, so
 *    an unrelated edit re-sends it unchanged (the server accepts that).
 * 3. Admins see every unlisted value with a "Pick department" action that opens that user's
 *    edit form.
 * 4. A pending approval sends the department picked on its row.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import Users from './Users';
import type { UserDepartments } from '../types/userDepartments';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
    getPendingUserApprovals: jest.fn(),
    getUserDepartments: jest.fn(),
    updateUser: jest.fn(),
    approveUser: jest.fn(),
  },
}));

let mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const LEGACY_USER = {
  id: 1,
  version: 3,
  email: 'rosa@example.test',
  employee_id: '40231',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'operator',
  department: 'Machining',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

const LISTED_USER = {
  id: 2,
  version: 1,
  email: 'sam@example.test',
  employee_id: '40232',
  first_name: 'Sam',
  last_name: 'Lee',
  role: 'quality',
  department: 'Quality',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

const DEPARTMENTS: UserDepartments = {
  departments: ['Purchasing', 'Inventory', 'Sales', 'Quality', 'Maintenance', 'Shipping', 'Production', 'Other'],
  unlisted: [{ id: 1, name: 'Rosa Vega', employee_id: '40231', is_active: true, department: 'Machining' }],
};

function renderUsers() {
  return render(
    <MemoryRouter initialEntries={['/users']}>
      <ToastProvider>
        <Users />
      </ToastProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
  mockedApi.getUsers.mockResolvedValue([LEGACY_USER, LISTED_USER]);
  mockedApi.getPendingUserApprovals.mockResolvedValue([]);
  mockedApi.getUserDepartments.mockResolvedValue(DEPARTMENTS);
  mockedApi.updateUser.mockResolvedValue({});
  mockedApi.approveUser.mockResolvedValue({});
});

it('lists users whose department is not on the list and opens their edit form', async () => {
  renderUsers();
  const panel = await screen.findByTestId('unlisted-departments');
  expect(panel).toHaveTextContent('1 user has a department that is not on the list');
  expect(panel).toHaveTextContent('Rosa Vega');
  expect(panel).toHaveTextContent('“Machining”');

  fireEvent.click(within(panel).getByRole('button', { name: 'Pick a department for Rosa Vega' }));

  const select = await screen.findByLabelText('Department');
  expect(select.tagName).toBe('SELECT');
  expect(select).toHaveValue('Machining');
  // The legacy value is kept only as a flagged, temporary option beside the real list.
  expect(within(select).getByRole('option', { name: 'Machining (not on the list)' })).toBeInTheDocument();
  expect(within(select).getByRole('option', { name: 'No department' })).toHaveValue('');
  expect(within(select).getAllByRole('option')).toHaveLength(DEPARTMENTS.departments.length + 2);

  fireEvent.change(select, { target: { value: 'Production' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update' }));
  await waitFor(() =>
    expect(mockedApi.updateUser).toHaveBeenCalledWith(1, expect.objectContaining({ department: 'Production' }))
  );
  // The correction list is re-read after a save.
  await waitFor(() => expect(mockedApi.getUserDepartments).toHaveBeenCalledTimes(2));
});

it('re-sends an unchanged legacy department on an unrelated edit', async () => {
  renderUsers();
  const panel = await screen.findByTestId('unlisted-departments');
  fireEvent.click(within(panel).getByRole('button', { name: 'Pick a department for Rosa Vega' }));
  await screen.findByLabelText('Department');
  fireEvent.click(screen.getByRole('button', { name: 'Update' }));
  await waitFor(() =>
    expect(mockedApi.updateUser).toHaveBeenCalledWith(1, expect.objectContaining({ department: 'Machining' }))
  );
});

it('hides the correction list from a Manager and never asks for it', async () => {
  mockAuthUser = { id: 50, role: 'manager', is_superuser: false };
  renderUsers();
  expect((await screen.findAllByText(/Rosa/)).length).toBeGreaterThan(0);
  expect(screen.queryByTestId('unlisted-departments')).toBeNull();
  expect(mockedApi.getUserDepartments).not.toHaveBeenCalled();
});

it('approves a pending account with the department picked on its row', async () => {
  mockedApi.getPendingUserApprovals.mockResolvedValue([
    {
      id: 7,
      email: 'pat@example.test',
      employee_id: '40299',
      first_name: 'Pat',
      last_name: 'Doe',
      role: 'operator',
      department: 'Welding',
      is_active: false,
      created_at: '2026-01-02T00:00:00Z',
    },
  ]);
  renderUsers();
  const select = await screen.findByLabelText('Approval department for Pat Doe');
  expect(screen.getByText(/Registered department “Welding” is not on the list/)).toBeInTheDocument();
  expect(select).toHaveValue('');
  fireEvent.change(select, { target: { value: 'Production' } });
  fireEvent.click(screen.getByRole('button', { name: /Approve/ }));
  await waitFor(() =>
    expect(mockedApi.approveUser).toHaveBeenCalledWith(7, { role: 'operator', department: 'Production' })
  );
});

it('keeps the form open and shows the server reason when a department is refused', async () => {
  const detail =
    'Department must be one of: Purchasing, Inventory, Sales, Quality, Maintenance, Shipping, Production, Other (or left blank).';
  mockedApi.updateUser.mockRejectedValue({ response: { status: 422, data: { detail } } });
  renderUsers();
  const panel = await screen.findByTestId('unlisted-departments');
  fireEvent.click(within(panel).getByRole('button', { name: 'Pick a department for Rosa Vega' }));
  const select = await screen.findByLabelText('Department');
  fireEvent.change(select, { target: { value: 'Quality' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update' }));

  // Non-optimistic: the refusal is shown verbatim and the form (with the pick) stays up.
  expect(await screen.findByText(detail)).toBeInTheDocument();
  expect(screen.getByLabelText('Department')).toHaveValue('Quality');
  // A refused save does not re-read the correction list as if it had worked.
  expect(mockedApi.getUserDepartments).toHaveBeenCalledTimes(1);
});

it('still offers a pick list, never free text, when the department list cannot be loaded', async () => {
  const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
  mockedApi.getUserDepartments.mockRejectedValue(new Error('network down'));
  renderUsers();
  expect((await screen.findAllByText(/Rosa/)).length).toBeGreaterThan(0);
  // The correction panel needs the server's answer, so it stays hidden.
  expect(screen.queryByTestId('unlisted-departments')).toBeNull();

  fireEvent.click(screen.getAllByRole('button', { name: /Add User/ })[0]);
  const select = await screen.findByLabelText('Department');
  expect(select.tagName).toBe('SELECT');
  const options = within(select)
    .getAllByRole('option')
    .map(option => option.textContent);
  expect(options).toEqual(['No department', ...DEPARTMENTS.departments]);
  consoleError.mockRestore();
});

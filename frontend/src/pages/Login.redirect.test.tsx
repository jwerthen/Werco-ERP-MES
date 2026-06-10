/**
 * Login — role-based default landing (B0.3 "Action Inbox as the front door").
 *
 * Managerial roles (admin/manager/supervisor) land on /action-inbox after login;
 * other roles keep the classic dashboard at "/". AuthContext is mocked at the module
 * boundary; the mocked login persists the signed-in user the same way the real one
 * does (sessionStorage), which is what Login reads to pick the landing path.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Login from './Login';

const mockNavigate = jest.fn();
const mockLogin = jest.fn();
const mockLoginWithEmployeeId = jest.fn();

jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    login: mockLogin,
    loginWithEmployeeId: mockLoginWithEmployeeId,
  }),
}));

const signInAs = async (role: string) => {
  mockLogin.mockImplementation(async () => {
    sessionStorage.setItem('user', JSON.stringify({ id: 1, role }));
  });

  render(
    <MemoryRouter>
      <Login />
    </MemoryRouter>
  );

  fireEvent.change(screen.getByPlaceholderText('you@werco.com'), { target: { value: 'user@werco.com' } });
  fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'Password123!' } });
  fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

  await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
};

describe('Login default landing redirect', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    sessionStorage.clear();
  });

  it.each(['admin', 'manager', 'supervisor'])('sends %s users to the Action Inbox', async (role) => {
    await signInAs(role);
    expect(mockNavigate).toHaveBeenCalledWith('/action-inbox', { replace: true });
  });

  it('keeps non-managerial roles on the classic dashboard', async () => {
    await signInAs('quality');
    expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
  });

  it('falls back to the dashboard when no user is stored', async () => {
    mockLogin.mockResolvedValue(undefined);
    render(
      <MemoryRouter>
        <Login />
      </MemoryRouter>
    );
    fireEvent.change(screen.getByPlaceholderText('you@werco.com'), { target: { value: 'user@werco.com' } });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'Password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true }));
  });
});

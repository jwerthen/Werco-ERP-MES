import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { AuthProvider, useAuth } from './AuthContext';
import api from '../services/api';
jest.mock('../services/api', () => ({ __esModule: true, default: { getCurrentUser: jest.fn(), logout: jest.fn() } }));
const fullUser = {
  id: 1,
  email: 'a@werco.test',
  role: 'admin',
  employee_id: 'E1',
  first_name: 'A',
  last_name: 'User',
  is_active: true,
};
function Probe() {
  const auth = useAuth();
  return (
    <>
      <span>{auth.sessionWarning ? 'warning' : 'active'}</span>
      <span>{auth.isAuthenticated ? 'signed in' : 'signed out'}</span>
      <button onClick={auth.extendSession}>Extend</button>
    </>
  );
}
beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  sessionStorage.clear();
  sessionStorage.setItem('token', 'token');
  sessionStorage.setItem('user', JSON.stringify(fullUser));
  (api.getCurrentUser as jest.Mock).mockResolvedValue(fullUser);
});
afterEach(() => {
  jest.useRealTimers();
  sessionStorage.clear();
});
it('keeps the warning visible for the final minute and does not extend from ordinary activity', async () => {
  await act(async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
  });
  act(() => {
    jest.advanceTimersByTime(14 * 60_000);
  });
  expect(screen.getByText('warning')).toBeInTheDocument();
  fireEvent.keyDown(window, { key: 'Shift' });
  act(() => {
    jest.advanceTimersByTime(59_000);
  });
  expect(screen.getByText('warning')).toBeInTheDocument();
  expect(api.logout).not.toHaveBeenCalled();
  act(() => {
    jest.advanceTimersByTime(1000);
  });
  expect(api.logout).toHaveBeenCalledTimes(1);
  expect(screen.getByText('signed out')).toBeInTheDocument();
});
it('explicit extension grants a fresh idle window', async () => {
  await act(async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
  });
  act(() => {
    jest.advanceTimersByTime(14 * 60_000);
  });
  fireEvent.click(screen.getByText('Extend'));
  expect(screen.getByText('active')).toBeInTheDocument();
  act(() => {
    jest.advanceTimersByTime(60_000);
  });
  expect(api.logout).not.toHaveBeenCalled();
  act(() => {
    jest.advanceTimersByTime(13 * 60_000);
  });
  expect(screen.getByText('warning')).toBeInTheDocument();
});

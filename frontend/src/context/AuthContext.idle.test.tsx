import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { AuthProvider, useAuth } from './AuthContext';
import api from '../services/api';
import SessionWarningModal from '../components/SessionWarningModal';
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
it('keeps the warning visible for the final minute and expires when there is no activity', async () => {
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
it.each(['touchStart', 'pointerDown', 'keyDown'] as const)(
  'a deliberate %s during the warning renews the session',
  async event => {
    await act(async () => {
      render(
        <AuthProvider>
          <Probe />
        </AuthProvider>
      );
    });
    act(() => jest.advanceTimersByTime(14 * 60_000));
    expect(screen.getByText('warning')).toBeInTheDocument();
    fireEvent[event](window);
    expect(screen.getByText('active')).toBeInTheDocument();
    act(() => jest.advanceTimersByTime(60_000));
    expect(api.logout).not.toHaveBeenCalled();
  }
);

it('does not treat modal/programmatic scrolling as fresh activity', async () => {
  await act(async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
  });
  act(() => jest.advanceTimersByTime(14 * 60_000));
  fireEvent.scroll(window);
  expect(screen.getByText('warning')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(60_000));
  expect(api.logout).toHaveBeenCalledTimes(1);
});

it('expires immediately when a suspended phone resumes after its deadline', async () => {
  await act(async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
  });
  // Move wall time without executing timers, as when a mobile tab is suspended.
  jest.setSystemTime(Date.now() + 16 * 60_000);
  fireEvent.touchStart(window);
  expect(api.logout).toHaveBeenCalledTimes(1);
  expect(screen.getByText('signed out')).toBeInTheDocument();
});

it('keeps the warning sign-out button usable while ordinary taps resume the session', async () => {
  await act(async () => {
    render(
      <AuthProvider>
        <Probe />
        <SessionWarningModal />
      </AuthProvider>
    );
  });
  act(() => jest.advanceTimersByTime(14 * 60_000));
  const signOut = screen.getByRole('button', { name: 'Log Out Now' });
  fireEvent.pointerDown(signOut);
  expect(screen.getByText('Still working?')).toBeInTheDocument();
  fireEvent.click(signOut);
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

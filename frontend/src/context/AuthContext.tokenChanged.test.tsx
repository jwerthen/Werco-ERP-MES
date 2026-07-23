/**
 * Kiosk badge-screen fallback wiring (Kiosk Foundry Redesign, decision 13):
 * on /kiosk paths the axios 401 interceptor clears the session WITHOUT
 * navigating to /login, so AuthContext must react to the
 * `werco:auth-token-changed` event and flip `isAuthenticated` — that flip is
 * what re-renders OperatorKiosk to its badge login screen without a reload.
 */
import React from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import { AuthProvider, useAuth } from './AuthContext';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    logout: jest.fn(),
    getCurrentUser: jest.fn(),
    getRolePermissions: jest.fn(),
    login: jest.fn(),
    loginWithEmployeeId: jest.fn(),
    logoutWithEmployeeId: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const fullUser = {
  id: 1,
  email: 'op@werco.test',
  role: 'operator',
  employee_id: 'EMP-4217',
  first_name: 'Kay',
  last_name: 'Operator',
  is_active: true,
};

function AuthProbe() {
  const { isAuthenticated, isLoading } = useAuth();
  if (isLoading) return <div>loading</div>;
  return <div>{isAuthenticated ? 'authenticated' : 'signed-out'}</div>;
}

describe('AuthContext werco:auth-token-changed listener', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    sessionStorage.clear();
    mockApi.getCurrentUser.mockResolvedValue(fullUser as never);
    mockApi.getRolePermissions.mockResolvedValue({} as never);
  });

  function seedSession() {
    sessionStorage.setItem('token', 'jwt-token');
    sessionStorage.setItem('user', JSON.stringify(fullUser));
  }

  it('flips isAuthenticated to false when the event fires after tokens were cleared', async () => {
    seedSession();
    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>
    );
    await screen.findByText('authenticated');

    // Simulate the axios interceptor's kiosk-path logout(): sessionStorage
    // tokens are gone by the time the event is dispatched, and no navigation
    // happens.
    act(() => {
      sessionStorage.removeItem('token');
      sessionStorage.removeItem('refreshToken');
      sessionStorage.removeItem('tokenExpiresAt');
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });

    await screen.findByText('signed-out');
    expect(sessionStorage.getItem('user')).toBeNull();
  });

  it('ignores the event while a token is still present (setToken/setTokens path)', async () => {
    seedSession();
    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>
    );
    await screen.findByText('authenticated');

    act(() => {
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });

    await waitFor(() => expect(screen.getByText('authenticated')).toBeInTheDocument());
    expect(sessionStorage.getItem('user')).not.toBeNull();
  });
});

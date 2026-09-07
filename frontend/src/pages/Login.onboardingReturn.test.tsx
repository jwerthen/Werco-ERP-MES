import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom';
import Login from './Login';
import Layout from '../components/Layout';
import TourHighlight from '../components/Tour/TourHighlight';
import { AuthProvider } from '../context/AuthContext';
import { TourProvider } from '../context/TourContext';
import { UnsavedChangesProvider } from '../context/UnsavedChangesContext';
import api from '../services/api';

// Retain the real authentication, login, shell onboarding, tour navigation and
// data router. Only unrelated network effects and heavy shell widgets are inert.
jest.mock('../components/CompanySwitcher', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/ReadOnlyBanner', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/SessionWarningModal', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/AdaptivePromptPanel', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/Tour', () => ({ TourMenu: () => null }));
jest.mock('../components/ui/BottomNav', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/ai/CopilotPanel', () => ({ CopilotPanel: () => null }));
jest.mock('../components/NotificationBell', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/GlobalSearch', () => ({
  __esModule: true,
  default: () => null,
  useGlobalSearch: () => ({ isOpen: false, open: jest.fn(), close: jest.fn() }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({}) }));
jest.mock('../hooks/useConnectionStatus', () => ({ useConnectionStatus: () => ({ label: 'Connected' }) }));
jest.mock('../hooks/useKeyboardShortcuts', () => ({ useKeyboardShortcuts: () => undefined, GLOBAL_SHORTCUTS: [] }));
jest.mock('../context/KeyboardShortcutsContext', () => ({
  useKeyboardShortcutsContext: () => ({ showHelp: jest.fn() }),
}));
jest.mock('../services/realtime', () => ({ buildWsUrl: () => 'ws://localhost/ws', getAccessToken: () => null }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    login: jest.fn(),
    setToken: jest.fn(),
    getRolePermissions: jest.fn(),
    getPendingUserApprovalSummary: jest.fn().mockResolvedValue({ count: 0 }),
  },
}));

class RouterRequest {
  url: string;
  signal?: AbortSignal;
  method: string;
  constructor(url: string, init: RequestInit = {}) {
    this.url = url;
    this.signal = init.signal ?? undefined;
    this.method = init.method ?? 'GET';
  }
}
beforeAll(() => {
  global.Request = RouterRequest as unknown as typeof Request;
  Element.prototype.scrollIntoView = jest.fn();
  window.scrollTo = jest.fn();
});
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  (api.login as jest.Mock).mockResolvedValue({
    access_token: 'fixture-token',
    user: { id: 42, company_id: 1, role: 'admin', email: 'admin@example.test', first_name: 'Test', last_name: 'Admin' },
  });
});

it.each(['/quotes?id=1', '/work-orders/new'])(
  'keeps the requested %s destination after fresh sign-in and automatic onboarding effects',
  async destination => {
    let finishPermissions!: (value: object) => void;
    (api.getRolePermissions as jest.Mock).mockReturnValue(
      new Promise(resolve => {
        finishPermissions = resolve;
      })
    );
    const router = createMemoryRouter(
      [
        {
          path: '*',
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/login" element={<Login />} />
                <Route
                  path="*"
                  element={
                    <Layout>
                      <p data-testid="destination">Requested work screen</p>
                    </Layout>
                  }
                />
              </Routes>
              <TourHighlight />
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: [{ pathname: '/login', state: { from: destination } }] }
    );
    const visited: string[] = [];
    const unsubscribe = router.subscribe(state => visited.push(state.location.pathname + state.location.search));
    render(
      <AuthProvider>
        <TourProvider>
          <RouterProvider router={router} />
        </TourProvider>
      </AuthProvider>
    );
    fireEvent.change(screen.getByRole('textbox', { name: 'Email or Employee ID' }), {
      target: { value: 'admin@example.test' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'fixture-password' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(api.getRolePermissions).toHaveBeenCalledTimes(1));
    expect(router.state.location.pathname).toBe('/login');
    await act(async () => {
      finishPermissions({});
    });
    await screen.findByTestId('destination');
    expect(router.state.location.pathname + router.state.location.search).toBe(destination);
    expect(visited).not.toContain('/');
    expect(screen.queryByRole('button', { name: 'Close tour' })).not.toBeInTheDocument();
    unsubscribe();
  }
);

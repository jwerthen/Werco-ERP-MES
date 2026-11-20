/**
 * The sign-in screens after the server ended a session with `floor_pin_reauth_required`.
 *
 * The API client (services/api.ts) clears the session and leaves the server's message in
 * sessionStorage (utils/signInNotice); off the kiosk it also redirects to
 * `/login?reason=pin_reauth&mode=employee&returnTo=…`. Pinned here: the Login page shows the
 * message once and opens Badge Only (only badge sessions are ended this way), with its own
 * copy when no message survived; the kiosk badge screen shows it until the next attempt.
 * And the 403 `floor_pin_reset_required` on sign-on is shown like `floor_pin_not_set`: the
 * message, no PIN pad.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Login from './Login';
import KioskBadgeLogin from '../components/kiosk/KioskBadgeLogin';
import { FLOOR_PIN_REAUTH_MESSAGE, FLOOR_PIN_RESET_REQUIRED_CODE } from '../services/badgeSignOn';
import { setSignInNotice } from '../utils/signInNotice';

const mockNavigate = jest.fn();
const mockBadgeLogin = jest.fn();
jest.mock('react-router-dom', () => ({ ...jest.requireActual('react-router-dom'), useNavigate: () => mockNavigate }));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ login: jest.fn(), loginWithEmployeeId: mockBadgeLogin }),
}));

const SERVER_MESSAGE = 'Sign in again with your badge and PIN.';
const RESET_MESSAGE = 'Your role changed. Ask an Admin to set a new floor PIN.';
const RESET_REQUIRED = {
  response: { status: 403, data: { detail: { code: FLOOR_PIN_RESET_REQUIRED_CODE, message: RESET_MESSAGE } } },
};

const renderLogin = (search: string) =>
  render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <Login />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
});

describe('Login after floor_pin_reauth_required', () => {
  it('shows the server message once and opens Badge Only', async () => {
    setSignInNotice(SERVER_MESSAGE);
    const { unmount } = renderLogin('?reason=pin_reauth&mode=employee&returnTo=%2Fwork-orders');

    expect(await screen.findByText(SERVER_MESSAGE)).toBeInTheDocument();
    expect(screen.getByLabelText('Employee / Badge ID')).toBeInTheDocument();
    unmount();

    // Consumed: a later visit without the reason shows nothing.
    renderLogin('');
    expect(screen.queryByText(SERVER_MESSAGE)).not.toBeInTheDocument();
  });

  it('falls back to its own copy when no message survived the redirect', () => {
    renderLogin('?reason=pin_reauth&mode=employee');
    expect(screen.getByText(FLOOR_PIN_REAUTH_MESSAGE)).toBeInTheDocument();
  });

  it('shows floor_pin_reset_required like floor_pin_not_set: the message, no PIN step', async () => {
    mockBadgeLogin.mockRejectedValueOnce(RESET_REQUIRED);
    renderLogin('?kiosk=1');

    fireEvent.change(screen.getByLabelText('Employee / Badge ID'), { target: { value: '4217' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByText(RESET_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument();
    expect(mockBadgeLogin).toHaveBeenCalledTimes(1);
  });
});

describe('KioskBadgeLogin after floor_pin_reauth_required', () => {
  it('shows the message on the badge screen until the next attempt', async () => {
    setSignInNotice(SERVER_MESSAGE);
    const onLogin = jest.fn().mockResolvedValue(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    expect(await screen.findByText(SERVER_MESSAGE)).toBeInTheDocument();

    ['4', '2', '1', '7'].forEach(key => fireEvent.keyDown(window, { key }));
    fireEvent.keyDown(window, { key: 'Enter' });

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith('4217'));
    expect(screen.queryByText(SERVER_MESSAGE)).not.toBeInTheDocument();
  });

  it('shows floor_pin_reset_required with no PIN pad', async () => {
    const onLogin = jest.fn().mockRejectedValueOnce(RESET_REQUIRED);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    ['4', '2', '1', '7'].forEach(key => fireEvent.keyDown(window, { key }));
    fireEvent.keyDown(window, { key: 'Enter' });

    expect(await screen.findByRole('alert')).toHaveTextContent(RESET_MESSAGE);
    expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument();
  });
});

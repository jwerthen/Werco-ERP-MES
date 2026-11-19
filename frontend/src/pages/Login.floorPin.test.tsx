/**
 * Login — Badge Only mode with the floor PIN (AS9100D 8.5.2).
 *
 * The desk Login page runs every badge sign-in through `signOnWithBadge`: a badge with a PIN
 * answers 401 `floor_pin_required`, the PIN step (FloorPinPad, desk variant) replaces the
 * form, and the SAME normalized badge is resent with the PIN. Pinned here: the badge-only
 * call carries no PIN argument; a wrong PIN stays on the PIN step; Back returns to the form
 * (hidden, not unmounted, so the badge is still there) with no error; `floor_pin_not_set`
 * shows its message rather than crashing on the object; password sign-in never sees a pad.
 *
 * AuthContext is mocked at the module boundary (the Login.redirect.test.tsx pattern).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  useAuth: () => ({ login: mockLogin, loginWithEmployeeId: mockLoginWithEmployeeId }),
}));

const httpError = (status: number, detail: unknown) => ({ response: { status, data: { detail } } });
const CHALLENGE = httpError(401, { code: 'floor_pin_required', message: 'Enter your PIN.' });
const WRONG = httpError(401, 'Invalid badge or PIN');
const NOT_SET_MESSAGE = 'No PIN is set for this badge. Ask a supervisor to set one.';
const NOT_SET = httpError(403, { code: 'floor_pin_not_set', message: NOT_SET_MESSAGE });

const signedIn = async () => {
  sessionStorage.setItem('user', JSON.stringify({ id: 3, company_id: 1, role: 'operator' }));
};

function renderLogin() {
  return render(
    <MemoryRouter>
      <Login />
    </MemoryRouter>
  );
}

function submitBadge(badge: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Badge Only' }));
  fireEvent.change(screen.getByLabelText('Employee / Badge ID'), { target: { value: badge } });
  fireEvent.submit(screen.getByLabelText('Employee / Badge ID').closest('form') as HTMLFormElement);
}

const pinInput = () => screen.getByTestId('floor-pin-input') as HTMLInputElement;
const pinForm = () => pinInput().closest('form') as HTMLFormElement;
const badgeForm = () => screen.getByLabelText('Employee / Badge ID').closest('form') as HTMLFormElement;

function enterPin(pin: string) {
  fireEvent.change(pinInput(), { target: { value: pin } });
  fireEvent.click(within(pinForm()).getByRole('button', { name: 'Sign In' }));
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
});

describe('Login Badge Only with a floor PIN', () => {
  it('a badge with no PIN signs in on the first call, with no PIN argument and no pad', async () => {
    mockLoginWithEmployeeId.mockImplementation(signedIn);
    renderLogin();

    submitBadge('4217');

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledTimes(1));
    expect(mockLoginWithEmployeeId.mock.calls).toEqual([['4217']]);
    expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument();
  });

  it('the challenge swaps the form for the PIN step, then the same badge is resent with the PIN', async () => {
    mockLoginWithEmployeeId.mockRejectedValueOnce(CHALLENGE).mockImplementationOnce(signedIn);
    renderLogin();

    submitBadge('42');

    expect(await screen.findByRole('heading', { name: 'Enter your PIN' })).toBeInTheDocument();
    expect(badgeForm()).toHaveClass('hidden');
    expect(screen.getByText('0042')).toBeInTheDocument();
    expect(screen.getByLabelText('PIN')).toBe(pinInput());
    expect(pinInput()).toHaveAttribute('type', 'password');
    expect(mockNavigate).not.toHaveBeenCalled();

    enterPin('0912');

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledTimes(1));
    expect(mockLoginWithEmployeeId.mock.calls).toEqual([['0042'], ['0042', '0912']]);
  });

  it('a wrong PIN stays on the PIN step with the refusal, then the right one signs in', async () => {
    mockLoginWithEmployeeId
      .mockRejectedValueOnce(CHALLENGE)
      .mockRejectedValueOnce(WRONG)
      .mockImplementationOnce(signedIn);
    renderLogin();

    submitBadge('4217');
    await screen.findByTestId('floor-pin-input');
    enterPin('1357');

    expect(await within(pinForm()).findByRole('alert')).toHaveTextContent('Invalid badge or PIN');
    await waitFor(() => expect(pinInput().value).toBe(''));
    expect(mockNavigate).not.toHaveBeenCalled();

    enterPin('0912');
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(mockLoginWithEmployeeId).toHaveBeenLastCalledWith('4217', '0912');
  });

  it('Back returns to the same form, badge intact, with no error and no sign-in', async () => {
    mockLoginWithEmployeeId.mockRejectedValueOnce(CHALLENGE);
    renderLogin();

    submitBadge('4217');
    await screen.findByTestId('floor-pin-input');
    fireEvent.click(within(pinForm()).getByRole('button', { name: 'Back' }));

    await waitFor(() => expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument());
    expect(badgeForm()).not.toHaveClass('hidden');
    expect(screen.getByLabelText('Employee / Badge ID')).toHaveValue('4217');
    expect(screen.queryByText(/Login failed|cancelled/i)).not.toBeInTheDocument();
    expect(mockLoginWithEmployeeId).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('floor_pin_not_set shows its message on the form, never an object', async () => {
    mockLoginWithEmployeeId.mockRejectedValueOnce(NOT_SET);
    renderLogin();

    submitBadge('4217');

    expect(await screen.findByText(NOT_SET_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument();
    expect(screen.queryByText(/\[object/)).not.toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('a 403 after the PIN (the policy flipped mid-sign-in) ends on the form with its message', async () => {
    mockLoginWithEmployeeId.mockRejectedValueOnce(CHALLENGE).mockRejectedValueOnce(NOT_SET);
    renderLogin();

    submitBadge('4217');
    await screen.findByTestId('floor-pin-input');
    enterPin('0912');

    expect(await screen.findByText(NOT_SET_MESSAGE)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument());
  });

  it('an unknown badge keeps its plain refusal text', async () => {
    mockLoginWithEmployeeId.mockRejectedValueOnce(httpError(401, 'Invalid employee ID'));
    renderLogin();

    submitBadge('4217');

    expect(await screen.findByText('Invalid employee ID')).toBeInTheDocument();
    expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument();
  });

  it('password sign-in never draws the PIN step', async () => {
    mockLogin.mockRejectedValueOnce(httpError(401, 'Incorrect email or password'));
    renderLogin();

    fireEvent.change(screen.getByPlaceholderText(/you@werco\.com/), { target: { value: 'user@werco.com' } });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'Password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByText('Incorrect email or password')).toBeInTheDocument();
    expect(screen.queryByTestId('floor-pin-input')).not.toBeInTheDocument();
    expect(mockLoginWithEmployeeId).not.toHaveBeenCalled();
  });
});

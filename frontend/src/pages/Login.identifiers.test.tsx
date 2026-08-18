/**
 * Login — the password mode accepts an EMAIL OR AN EMPLOYEE ID.
 *
 * POST /auth/login resolves either identifier, so the field must not be type="email":
 * a browser refuses to submit a bare badge through one, which would lock every
 * badge-only account (emp-<badge>@users.werco.com) out of the password path without
 * ever reaching the server. These assertions pin the attributes that carry that, plus
 * the mode labels — the two modes differ by PASSWORD, not by identifier shape, and a
 * label that still said "Email" would be a straightforward lie once both accept a badge.
 *
 * The passwordless badge mode is unchanged and ?kiosk=1 still pins it.
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

const renderLogin = (search = '') =>
  render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <Login />
    </MemoryRouter>
  );

const identifierField = () => screen.getByPlaceholderText(/you@werco\.com/) as HTMLInputElement;

describe('Login identifier field (password mode)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    sessionStorage.clear();
  });

  it('accepts a free-text identifier rather than constraining to an address', () => {
    renderLogin();
    const field = identifierField();
    expect(field.type).toBe('text');
    expect(field.getAttribute('autocomplete')).toBe('username');
  });

  it('labels the field as email or employee ID', () => {
    renderLogin();
    expect(screen.getByText('Email or Employee ID')).toBeInTheDocument();
    expect(identifierField().getAttribute('aria-label')).toBe('Email or Employee ID');
  });

  it('submits a bare employee ID verbatim to the password login', async () => {
    mockLogin.mockResolvedValue(undefined);
    renderLogin();

    fireEvent.change(identifierField(), { target: { value: 'EMP-1001' } });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'Password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('EMP-1001', 'Password123!'));
    expect(mockLoginWithEmployeeId).not.toHaveBeenCalled();
  });

  it('submits an email address through the same call, unchanged', async () => {
    // The regression half. The state and the api-client parameter were both renamed
    // email -> identifier, and every existing user signs in with an address: if the
    // rename dropped a wire-up, badge login would look fine while the whole office
    // could not sign in. Same call, same argument order, verbatim value.
    mockLogin.mockResolvedValue(undefined);
    renderLogin();

    fireEvent.change(identifierField(), { target: { value: 'office.person@wercomfg.com' } });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), { target: { value: 'Password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith('office.person@wercomfg.com', 'Password123!')
    );
    expect(mockLoginWithEmployeeId).not.toHaveBeenCalled();
  });
});

describe('Login mode toggle', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    sessionStorage.clear();
  });

  it('distinguishes the modes by password, not by identifier shape', () => {
    renderLogin();
    expect(screen.getByRole('button', { name: 'Badge Only' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Password' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Email' })).not.toBeInTheDocument();
  });

  it('tells the operator the badge mode needs no password, via the field description', () => {
    renderLogin();
    fireEvent.click(screen.getByRole('button', { name: 'Badge Only' }));

    const badge = screen.getByPlaceholderText('0000 or EMP-1001');
    const describedBy = (badge.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    expect(describedBy.length).toBeGreaterThan(0);

    const description = describedBy.map(id => document.getElementById(id)?.textContent || '').join(' ');
    expect(description).toMatch(/no password/i);
    expect(description).toMatch(/badge/i);
  });

  it('renders no password field in badge mode', () => {
    renderLogin();
    fireEvent.click(screen.getByRole('button', { name: 'Badge Only' }));
    expect(screen.queryByPlaceholderText('Enter your password')).not.toBeInTheDocument();
  });

  it('signs in through the passwordless path when the operator picks badge mode', async () => {
    // The toggle, not the URL. ?kiosk=1 is pinned below, but a badge-only operator at an
    // ordinary browser reaches this mode by clicking, and that route must still call
    // POST /auth/employee-login rather than the password login with an empty password --
    // which the server would answer 401 with no indication why.
    mockLoginWithEmployeeId.mockResolvedValue(undefined);
    renderLogin();

    fireEvent.click(screen.getByRole('button', { name: 'Badge Only' }));
    fireEvent.change(screen.getByPlaceholderText('0000 or EMP-1001'), { target: { value: 'EMP-1001' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    // A non-numeric badge is passed verbatim; only all-digit input is normalized.
    await waitFor(() => expect(mockLoginWithEmployeeId).toHaveBeenCalledWith('EMP-1001'));
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('still pins badge mode under ?kiosk=1 and signs in passwordlessly', async () => {
    mockLoginWithEmployeeId.mockResolvedValue(undefined);
    renderLogin('?kiosk=1');

    expect(screen.queryByPlaceholderText('Enter your password')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('0000 or EMP-1001'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    // Numeric input is normalized to a 4-digit badge; that rule is unchanged.
    await waitFor(() => expect(mockLoginWithEmployeeId).toHaveBeenCalledWith('0042'));
    expect(mockLogin).not.toHaveBeenCalled();
  });
});

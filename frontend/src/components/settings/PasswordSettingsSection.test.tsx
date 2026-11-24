/**
 * PasswordSettingsSection — change your own password on My Settings.
 *
 * Contract under test is `POST /users/change-password`
 * (backend/app/api/endpoints/users.py → change_own_password): body
 * `{current_password, new_password}`, 200 `{message}`, 400 wrong current password,
 * 422 policy, 503 lost audit. The client refuses a short / blocklisted / mismatched
 * password before any request; a server refusal shows its `detail` verbatim, keeps
 * what was typed, and never toasts success.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PasswordSettingsSection from './PasswordSettingsSection';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import type { ChangeOwnPasswordResponse } from '../../types/password';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    changeOwnPassword: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const httpError = (status: number, detail: string) => {
  const err = new Error(detail) as Error & { response: { status: number; data: { detail: string } } };
  err.response = { status, data: { detail } };
  return err;
};

const GOOD_NEW = 'correct horse battery staple';

const renderSection = () =>
  render(
    <ToastProvider>
      <PasswordSettingsSection />
    </ToastProvider>
  );

const fill = (current: string, next: string, confirm: string) => {
  fireEvent.change(screen.getByLabelText(/^current password/i), { target: { value: current } });
  fireEvent.change(screen.getByLabelText(/^new password/i), { target: { value: next } });
  fireEvent.change(screen.getByLabelText(/^confirm new password/i), { target: { value: confirm } });
};

const submit = () => fireEvent.click(screen.getByRole('button', { name: /change password/i }));

describe('PasswordSettingsSection', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('shows the policy and the note about other signed-in sessions', () => {
    renderSection();

    expect(screen.getByRole('heading', { name: /^password$/i })).toBeInTheDocument();
    expect(screen.getByText(/12 or more characters/i)).toBeInTheDocument();
    expect(screen.getByText(/does not sign you out anywhere else/i)).toBeInTheDocument();
    // All three fields are masked password inputs with the right autocomplete hints.
    expect(screen.getByLabelText(/^current password/i)).toHaveAttribute('autocomplete', 'current-password');
    expect(screen.getByLabelText(/^new password/i)).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText(/^confirm new password/i)).toHaveAttribute('autocomplete', 'new-password');
  });

  it('sends only current and new password, then clears the form and toasts success', async () => {
    const ok: ChangeOwnPasswordResponse = { message: 'Password changed successfully' };
    mockApi.changeOwnPassword.mockResolvedValue(ok);
    renderSection();

    fill('old secret phrase 1', GOOD_NEW, GOOD_NEW);
    submit();

    await waitFor(() =>
      expect(mockApi.changeOwnPassword).toHaveBeenCalledWith({
        current_password: 'old secret phrase 1',
        new_password: GOOD_NEW,
      })
    );
    expect(await screen.findByText(/password changed\. use the new password/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText(/^current password/i)).toHaveValue(''));
    expect(screen.getByLabelText(/^new password/i)).toHaveValue('');
    expect(screen.getByLabelText(/^confirm new password/i)).toHaveValue('');
  });

  it('refuses a short new password before calling the server', async () => {
    renderSection();

    fill('old secret phrase 1', 'short', 'short');
    submit();

    expect(await screen.findByText('Password must be at least 12 characters')).toBeInTheDocument();
    expect(mockApi.changeOwnPassword).not.toHaveBeenCalled();
  });

  it('refuses a blocklisted new password before calling the server', async () => {
    renderSection();

    fill('old secret phrase 1', 'mypassword-is-long', 'mypassword-is-long');
    submit();

    expect(await screen.findByText(/common word or pattern that is too easy to guess/i)).toBeInTheDocument();
    expect(mockApi.changeOwnPassword).not.toHaveBeenCalled();
  });

  it('refuses mismatched confirmation before calling the server', async () => {
    renderSection();

    fill('old secret phrase 1', GOOD_NEW, `${GOOD_NEW} x`);
    submit();

    expect(await screen.findByText(/the two new passwords do not match/i)).toBeInTheDocument();
    expect(mockApi.changeOwnPassword).not.toHaveBeenCalled();
  });

  it('is not optimistic: no success and no second request while the change is in flight', async () => {
    let resolve: (value: ChangeOwnPasswordResponse) => void = () => undefined;
    mockApi.changeOwnPassword.mockReturnValue(
      new Promise<ChangeOwnPasswordResponse>(r => {
        resolve = r;
      })
    );
    renderSection();

    fill('old secret phrase 1', GOOD_NEW, GOOD_NEW);
    submit();

    await waitFor(() => expect(mockApi.changeOwnPassword).toHaveBeenCalledTimes(1));
    // While pending: the typed values are still there, nothing claims success, and a
    // second click does not send a second change.
    expect(screen.getByLabelText(/^current password/i)).toHaveValue('old secret phrase 1');
    expect(screen.queryByText(/password changed\. use the new password/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /chang/i }));
    expect(mockApi.changeOwnPassword).toHaveBeenCalledTimes(1);

    resolve({ message: 'Password changed successfully' });
    expect(await screen.findByText(/password changed\. use the new password/i)).toBeInTheDocument();
  });

  it.each([
    [400, 'Current password is incorrect'],
    [422, 'Value error, Password must be at least 12 characters long'],
    [
      403,
      'Your password is changed by you, signed in to the app: an API token or a shop-floor badge or station token cannot change a password.',
    ],
    [503, 'Unable to save audit record'],
  ])(
    'shows the server %s detail verbatim, keeps the typed values, and never toasts success',
    async (status, detail) => {
      mockApi.changeOwnPassword.mockRejectedValue(httpError(status, detail));
      renderSection();

      fill('wrong old phrase 1', GOOD_NEW, GOOD_NEW);
      submit();

      // Inline alert + error toast both carry the server's own words.
      await waitFor(() => expect(screen.getAllByText(detail).length).toBeGreaterThanOrEqual(1));
      expect(screen.queryByText(/password changed\. use the new password/i)).not.toBeInTheDocument();
      expect(screen.getByLabelText(/^current password/i)).toHaveValue('wrong old phrase 1');
      expect(screen.getByRole('button', { name: /change password/i })).not.toBeDisabled();
    }
  );
});

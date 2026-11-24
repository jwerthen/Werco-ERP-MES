/**
 * PasswordSettingsSection — change your own password, on My Settings (`/settings`).
 *
 * Calls `POST /users/change-password` through `api.changeOwnPassword`. The write is
 * server-gated (wrong current password 400, policy 422, people-only 403, lost audit
 * 503), so nothing here is optimistic: the form keeps a loading state, the server's
 * `detail` is shown verbatim, and only a 200 clears the form and toasts success.
 *
 * The current session stays signed in after a change. So does every OTHER signed-in
 * session: access tokens are stateless and `POST /auth/refresh` does not read
 * `password_changed_at`, so an existing session keeps refreshing (7-day idle window)
 * rather than ending after one 15-minute access token. The on-screen note says that
 * plainly; do not shorten it to "15 minutes" unless the refresh path starts refusing
 * refresh tokens issued before the change.
 */

import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { KeyIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { FormField, LoadingButton, useToast } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { apiErrorDetail } from '../../utils/apiError';
import { changeOwnPasswordSchema, type ChangeOwnPasswordFormData } from '../../validation/schemas';

const EMPTY_FORM: ChangeOwnPasswordFormData = {
  current_password: '',
  new_password: '',
  confirm_password: '',
};

export default function PasswordSettingsSection() {
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<ChangeOwnPasswordFormData>({
    resolver: zodResolver(changeOwnPasswordSchema),
    defaultValues: EMPTY_FORM,
  });

  const { markSaved } = useUnsavedChanges(isDirty, 'You have typed a new password that is not saved. Discard it?');

  const onSubmit = handleSubmit(async ({ current_password, new_password }) => {
    setSaving(true);
    setServerError(null);
    try {
      await api.changeOwnPassword({ current_password, new_password });
      markSaved();
      reset(EMPTY_FORM);
      showToast('success', 'Password changed. Use the new password the next time you sign in.');
    } catch (err) {
      const message = apiErrorDetail(err, 'Failed to change your password');
      setServerError(message);
      showToast('error', message);
    } finally {
      setSaving(false);
    }
  });

  return (
    <section className="rounded-sm border border-fd-line bg-fd-panel p-4" aria-labelledby="password-settings-heading">
      <div className="mb-1 flex items-center gap-2">
        <KeyIcon className="h-5 w-5 text-fd-blue" aria-hidden="true" />
        <h2 id="password-settings-heading" className="text-sm font-semibold uppercase tracking-wide text-fd-ink">
          Password
        </h2>
      </div>
      <p className="mb-3 text-xs text-fd-mute">
        Use 12 or more characters. Common words and patterns (such as &ldquo;password&rdquo;, &ldquo;qwerty&rdquo;,
        &ldquo;123456&rdquo; or the company name) are refused. There are no rules about capitals, digits or symbols, so
        a long phrase is a good choice.
      </p>

      <form onSubmit={onSubmit} className="max-w-md space-y-3" noValidate>
        <FormField label="Current password" required error={errors.current_password?.message}>
          {field => (
            <input
              {...field}
              {...register('current_password')}
              type="password"
              autoComplete="current-password"
              className={errors.current_password ? 'input-error' : 'input'}
            />
          )}
        </FormField>

        <FormField
          label="New password"
          required
          error={errors.new_password?.message}
          help="At least 12 characters, with no common word or pattern."
        >
          {field => (
            <input
              {...field}
              {...register('new_password')}
              type="password"
              autoComplete="new-password"
              className={errors.new_password ? 'input-error' : 'input'}
            />
          )}
        </FormField>

        <FormField label="Confirm new password" required error={errors.confirm_password?.message}>
          {field => (
            <input
              {...field}
              {...register('confirm_password')}
              type="password"
              autoComplete="new-password"
              className={errors.confirm_password ? 'input-error' : 'input'}
            />
          )}
        </FormField>

        {serverError && (
          <p role="alert" className="rounded-sm border border-fd-red/40 bg-fd-red/5 px-3 py-2 text-xs text-fd-red">
            {serverError}
          </p>
        )}

        <LoadingButton type="submit" variant="primary" size="sm" loading={saving} loadingText="Changing…">
          Change password
        </LoadingButton>

        <p className="text-xs text-fd-mute">
          You stay signed in here. Changing your password does not sign you out anywhere else: a browser or computer
          where you are already signed in keeps working until you sign out there, or until it sits unused for 7 days.
          The new password is needed for every new sign-in. If you think someone else knows your password, sign out on
          the other computers and tell an administrator.
        </p>
      </form>
    </section>
  );
}

/**
 * SmsSettingsSection — the self-service SMS slice of My Settings (PR 4).
 *
 * Deliberately self-contained so PR 3's full My Settings page (the complete
 * in-app / email / digest preference matrix) can drop it in as one section
 * rather than rewriting it. Everything it renders is driven by the server:
 *
 *   - GET  /notifications/catalog             → which events are `sms_eligible`
 *                                               (the event list is NEVER hardcoded)
 *   - GET  /users/me/notification-preferences → the RESOLVED per-event channel map
 *                                               plus `phone`, `sms_egress_enabled`,
 *                                               and `sms_configured` — the three
 *                                               reasons an SMS toggle can be inert
 *   - PUT  /users/me/notification-preferences → merge-save of the SMS column only
 *   - PUT  /users/me/phone                    → save / clear the number
 *   - POST /users/me/test-sms                 → one-off test message
 *
 * Three states are called out explicitly, because each makes an SMS opt-in
 * silently do nothing:
 *   1. NO NUMBER SAVED — the per-event toggles are disabled with a hint pointing
 *      at the number field above (an opt-in with nowhere to send is meaningless).
 *   2. COMPANY SMS EGRESS OFF — a warning banner states that no text messages are
 *      being sent for the whole company and that an administrator must enable it
 *      in Admin Settings. Toggles stay usable so a user can pre-set preferences,
 *      but nothing about the state is hidden.
 *   3. PROVIDER NOT CONFIGURED — the server reports `sms_configured: false` (a
 *      plain boolean; no credential is ever sent to the client), so the UI says
 *      setup is unfinished rather than letting a test send fail mysteriously.
 *
 * Preference toggles follow the repo's optimistic-UI convention (rarely rejected):
 * flip synchronously, roll back and surface the server's verbatim `detail` on
 * failure. The phone save and the test send are NOT optimistic — both are
 * server-validated (E.164 normalization / egress + storm-cap gating), so they keep
 * a loading state and reflect only what the server returns.
 *
 * No provider credential is read, written, or displayed anywhere in this file.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  DevicePhoneMobileIcon,
  ExclamationTriangleIcon,
  PaperAirplaneIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { Button, EmptyState, ErrorState, FormField, LoadingButton, useToast } from '../ui';
import { useOptimisticMutation } from '../../hooks/useOptimisticMutation';
import { smsPhoneSchema, normalizePhoneInput, type SmsPhoneFormData } from '../../validation/schemas';
import type {
  NotificationCatalogEntry,
  NotificationEventPreference,
  NotificationPreferences,
} from '../../types/notification';

/** Channel-on resolution: an explicit user choice wins, else the catalog default. */
const smsEnabledFor = (
  entry: NotificationCatalogEntry,
  prefs: Record<string, NotificationEventPreference>,
): boolean => {
  const explicit = prefs[entry.event_key]?.sms;
  if (typeof explicit === 'boolean') return explicit;
  return entry.default_channels.includes('sms');
};

/** Present the saved E.164 number in a readable grouping (US numbers only). */
const formatSavedPhone = (phone: string): string => {
  const match = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(phone);
  return match ? `+1 (${match[1]}) ${match[2]}-${match[3]}` : phone;
};

interface ToggleContext {
  eventKey: string;
  next: boolean;
}

export default function SmsSettingsSection() {
  const { showToast } = useToast();

  // Whether this user can actually open /admin/settings — same gate SmsEgressTab uses to
  // decide who may flip the switch. Used only to avoid linking non-admins to a page they
  // cannot reach; it grants nothing (the server is the authority on the toggle).
  const { user } = useAuth();
  const canReachAdminSettings =
    !!user && (user.is_superuser === true || user.role === 'admin' || user.role === 'platform_admin');

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const [savedPhone, setSavedPhone] = useState<string>('');
  const [smsEgressAllowed, setSmsEgressAllowed] = useState(false);
  const [smsConfigured, setSmsConfigured] = useState(true);
  const [catalog, setCatalog] = useState<NotificationCatalogEntry[]>([]);
  const [prefs, setPrefs] = useState<Record<string, NotificationEventPreference>>({});

  const [savingPhone, setSavingPhone] = useState(false);
  const [sendingTest, setSendingTest] = useState(false);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<SmsPhoneFormData>({
    resolver: zodResolver(smsPhoneSchema),
    defaultValues: { phone: '' },
  });

  // The preferences response carries the SMS context (phone / egress / provider
  // configured) alongside the resolved matrix, so one call answers "is this toggle
  // inert, and why?" without a second read that could disagree with it.
  //
  // `resetForm` is opt-in: a preference save also echoes the whole payload back, and
  // resetting the form there would silently discard a number the user had typed but
  // not yet saved.
  const applyPreferences = useCallback(
    (preferences: NotificationPreferences, resetForm = false) => {
      const phone = preferences?.phone ?? '';
      setSavedPhone(phone);
      if (resetForm) reset({ phone });
      setSmsEgressAllowed(!!preferences?.sms_egress_enabled);
      setSmsConfigured(preferences?.sms_configured !== false);
      setPrefs(preferences?.preferences ?? {});
    },
    [reset],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [entries, preferences] = await Promise.all([
        api.getNotificationCatalog(),
        api.getMyNotificationPreferences(),
      ]);
      setCatalog(Array.isArray(entries) ? entries : []);
      applyPreferences(preferences, true);
    } catch (err) {
      console.error('Failed to load SMS settings:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [applyPreferences]);

  useEffect(() => {
    load();
  }, [load]);

  const smsEvents = useMemo(
    () =>
      catalog
        .filter((entry) => entry.sms_eligible)
        .sort((a, b) => a.category.localeCompare(b.category) || a.label.localeCompare(b.label)),
    [catalog],
  );

  const hasPhone = savedPhone.trim().length > 0;

  // A test send is only meaningful when all three preconditions hold; the blocked
  // reason doubles as the button's tooltip so a disabled control is never unexplained.
  const testBlockedReason = !hasPhone
    ? 'Save a mobile number first'
    : !smsEgressAllowed
      ? 'SMS is switched off company-wide'
      : !smsConfigured
        ? 'Text messaging is not set up on this server yet'
        : undefined;
  const canSendTest = testBlockedReason === undefined;

  // --- Phone number (server-validated: NOT optimistic) ----------------------
  const onSavePhone = handleSubmit(async ({ phone }) => {
    const normalized = normalizePhoneInput(phone);
    setSavingPhone(true);
    try {
      const updated = await api.updateMyPhone(normalized || null);
      const next = updated?.phone ?? '';
      setSavedPhone(next);
      reset({ phone: next });
      showToast('success', next ? 'Mobile number saved' : 'Mobile number removed');
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || err?.message || 'Failed to save the mobile number');
    } finally {
      setSavingPhone(false);
    }
  });

  const handleRemovePhone = async () => {
    setSavingPhone(true);
    try {
      await api.updateMyPhone(null);
      setSavedPhone('');
      reset({ phone: '' });
      showToast('success', 'Mobile number removed — SMS notifications are off for your account');
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || err?.message || 'Failed to remove the mobile number');
    } finally {
      setSavingPhone(false);
    }
  };

  // --- Test send (server-gated: NOT optimistic) -----------------------------
  const handleTestSms = async () => {
    setSendingTest(true);
    try {
      // Only `detail` is surfaced — the provider message id / status in the response
      // is bookkeeping, not something a user should see. A 200 does NOT mean it went
      // out: `status` is "skipped" when the provider isn't configured, and a skipped
      // send must never read as success.
      const result = await api.sendTestSms();
      const sent = result?.status === 'sent';
      showToast(sent ? 'success' : 'error', result?.detail || 'Test message sent — it should arrive shortly.');
      if (!sent) setSmsConfigured(false);
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || err?.message || 'Failed to send the test message');
    } finally {
      setSendingTest(false);
    }
  };

  // --- Per-event SMS opt-in (rarely rejected: optimistic + rollback) --------
  const { run: toggleSms } = useOptimisticMutation<NotificationPreferences, ToggleContext>({
    applyOptimistic: ({ eventKey, next }) =>
      setPrefs((current) => ({
        ...current,
        [eventKey]: { ...(current[eventKey] ?? {}), sms: next },
      })),
    rollback: ({ eventKey, next }) =>
      setPrefs((current) => ({
        ...current,
        [eventKey]: { ...(current[eventKey] ?? {}), sms: !next },
      })),
    // Merge-save the SMS flag for ONE event only — email/digest choices and every
    // other event are left untouched by the partial payload.
    mutate: ({ eventKey, next }) =>
      api.updateMyNotificationPreferences({ preferences: { [eventKey]: { sms: next } } }),
    // The PUT echoes the full resolved matrix + SMS context, so adopt it wholesale
    // rather than trusting the optimistic guess.
    reconcile: (result) => {
      if (result?.preferences) applyPreferences(result);
    },
    errorFallback: 'Failed to update your SMS preference',
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  if (loadError) {
    return <ErrorState message="Could not load your SMS settings." onRetry={load} />;
  }

  return (
    <div className="space-y-4">
      {/* Company-wide egress state — an opt-in here does nothing while this is OFF. */}
      {!smsEgressAllowed && (
        <div
          className="flex items-start gap-3 rounded-sm border border-fd-amber/40 bg-fd-amber/5 px-4 py-3"
          role="status"
        >
          <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0 text-fd-amber mt-0.5" aria-hidden="true" />
          <div className="text-sm">
            <p className="font-semibold text-fd-amber">Text messages are switched off company-wide</p>
            <p className="mt-0.5 text-fd-mute">
              No text messages are being sent for this company, so the opt-ins below will not deliver anything
              yet.{' '}
              {/* Only link the admin page to people who can actually open it — /admin/settings is
                  permission-gated, so linking every operator there just dead-ends them. */}
              {canReachAdminSettings ? (
                <>
                  Enable SMS egress in{' '}
                  <Link to="/admin/settings?tab=smsprivacy" className="text-fd-blue hover:text-fd-ink underline">
                    Admin Settings → SMS Privacy
                  </Link>
                  .
                </>
              ) : (
                <>Ask an administrator to enable SMS egress for the company.</>
              )}{' '}
              You can still set your preferences now — they take effect as soon as it is enabled.
            </p>
          </div>
        </div>
      )}

      {/* Provider not set up server-side — reported as a plain boolean, never a credential. */}
      {smsEgressAllowed && !smsConfigured && (
        <div
          className="flex items-start gap-3 rounded-sm border border-fd-amber/40 bg-fd-amber/5 px-4 py-3"
          role="status"
        >
          <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0 text-fd-amber mt-0.5" aria-hidden="true" />
          <div className="text-sm">
            <p className="font-semibold text-fd-amber">Text messaging is not finished being set up</p>
            <p className="mt-0.5 text-fd-mute">
              SMS is allowed for this company, but the messaging provider is not configured on the server yet, so
              nothing will send. Ask an administrator to finish the setup.
            </p>
          </div>
        </div>
      )}

      {/* --- Mobile number ---------------------------------------------------- */}
      <section className="rounded-sm border border-fd-line bg-fd-panel p-4">
        <div className="mb-3 flex items-center gap-2">
          <DevicePhoneMobileIcon className="h-5 w-5 text-fd-blue" aria-hidden="true" />
          <h2 className="text-sm font-semibold uppercase tracking-wide text-fd-ink">Mobile number</h2>
        </div>

        <form onSubmit={onSavePhone} className="max-w-md space-y-3" noValidate>
          <FormField
            label="Mobile number for text alerts"
            error={errors.phone?.message}
            help="Include the country code, e.g. +1 512 555 0142. Leave blank to remove your number and stop all text messages. Standard carrier message and data rates apply."
          >
            {(field) => (
              <input
                {...field}
                {...register('phone')}
                type="tel"
                autoComplete="tel"
                inputMode="tel"
                placeholder="+1 512 555 0142"
                className={errors.phone ? 'input-error' : 'input'}
              />
            )}
          </FormField>

          <div className="flex flex-wrap items-center gap-2">
            <LoadingButton
              type="submit"
              variant="primary"
              size="sm"
              loading={savingPhone}
              loadingText="Saving…"
              disabled={!isDirty}
            >
              Save number
            </LoadingButton>
            {hasPhone && (
              <Button type="button" variant="ghost" size="sm" onClick={handleRemovePhone} disabled={savingPhone}>
                Remove number
              </Button>
            )}
            <LoadingButton
              type="button"
              variant="secondary"
              size="sm"
              loading={sendingTest}
              loadingText="Sending…"
              disabled={!canSendTest}
              onClick={handleTestSms}
              title={testBlockedReason}
            >
              <PaperAirplaneIcon className="mr-1.5 h-4 w-4" aria-hidden="true" />
              Send test SMS
            </LoadingButton>
          </div>

          <p className="text-xs text-fd-mute">
            {hasPhone ? (
              <>
                Alerts are sent to <span className="font-mono text-fd-body">{formatSavedPhone(savedPhone)}</span>.
              </>
            ) : (
              'No mobile number saved — text alerts are off for your account.'
            )}
          </p>
        </form>
      </section>

      {/* --- Per-event SMS opt-ins -------------------------------------------- */}
      <section className="rounded-sm border border-fd-line bg-fd-panel p-4">
        <div className="mb-1 flex items-center gap-2">
          <PaperAirplaneIcon className="h-5 w-5 text-fd-blue" aria-hidden="true" />
          <h2 className="text-sm font-semibold uppercase tracking-wide text-fd-ink">Text-message alerts</h2>
        </div>
        <p className="mb-3 text-xs text-fd-mute">
          Text alerts are opt-in and available only for the most urgent events. Messages stay deliberately terse
          because a text shows on a locked phone screen — the record number, what happened, and sometimes a
          one-word category, but never customer names, part details, quantities, or anything an operator typed.
          Open the app for the detail. Your in-app and email notifications are unaffected by these switches.
        </p>

        {!hasPhone && (
          <p className="mb-3 rounded-sm border border-fd-line bg-fd-sunken px-3 py-2 text-xs text-fd-mute">
            Save a mobile number above to turn any of these on.
          </p>
        )}

        {smsEvents.length === 0 ? (
          <EmptyState
            icon={DevicePhoneMobileIcon}
            title="No events offer text alerts"
            description="No notification event is currently eligible for SMS delivery."
          />
        ) : (
          <ul className="divide-y divide-fd-line">
            {smsEvents.map((entry) => {
              const checked = smsEnabledFor(entry, prefs);
              const forced = entry.mandatory_channel === 'sms';
              const inputId = `sms-pref-${entry.event_key}`;
              return (
                <li key={entry.event_key} className="py-2.5">
                  <label
                    htmlFor={inputId}
                    className={`grid grid-cols-[auto_1fr] items-start gap-x-3 ${
                      hasPhone && !forced ? 'cursor-pointer' : 'cursor-not-allowed opacity-70'
                    }`}
                  >
                    <input
                      id={inputId}
                      type="checkbox"
                      className="checkbox mt-0.5 row-span-2"
                      checked={forced ? true : checked}
                      disabled={!hasPhone || forced}
                      onChange={(e) => {
                        void toggleSms({ eventKey: entry.event_key, next: e.target.checked });
                      }}
                    />
                    <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-fd-ink">
                      {entry.label}
                      <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-fd-faint">
                        {entry.category}
                      </span>
                      {forced && (
                        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-fd-amber">
                          Always on
                        </span>
                      )}
                    </span>
                    <span className="mt-0.5 block text-xs text-fd-mute">{entry.description}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

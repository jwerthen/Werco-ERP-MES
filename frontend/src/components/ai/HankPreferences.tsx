import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import { DEFAULT_HANK_PREFERENCES, HankPreferencesResponse, HankPreferencesValues } from '../../types/hankPreferences';
import { formatCentralDateTime } from '../../utils/centralTime';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from './hankSession';

const schema = z.object({
  briefing_detail: z.enum(['concise', 'standard']),
  focus_area: z.enum(['role_default', 'my_work', 'shop', 'quality', 'purchasing', 'inventory', 'shipping']),
  handoff_format: z.enum(['bullets', 'checklist']),
  follow_up_alerts: z.boolean(),
});
type Operation = 'save' | 'reset' | 'refresh';

export interface HankPreferencesProps {
  onBusyChange?: (busy: boolean) => void;
}

export function HankPreferences({ onBusyChange }: HankPreferencesProps) {
  const [scope] = useState(getHankSessionScope);
  const [sessionChanged, setSessionChanged] = useState(false);
  const [saved, setSaved] = useState<HankPreferencesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const {
    register,
    handleSubmit,
    reset,
    formState: { isDirty, errors },
  } = useForm<HankPreferencesValues>({
    resolver: zodResolver(schema),
    defaultValues: DEFAULT_HANK_PREFERENCES,
  });
  const currentSession = useCallback(
    () => mounted.current && scope !== null && scope === getHankSessionScope(),
    [scope]
  );

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (scope !== getHankSessionScope()) {
        controllerRef.current?.abort();
        setSessionChanged(true);
        busyCallback.current?.(false);
      }
    });
    return () => {
      mounted.current = false;
      controllerRef.current?.abort();
      busyCallback.current?.(false);
      unsubscribe();
    };
  }, [scope]);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setLoadError(false);
    api
      .getHankPreferences(controller.signal)
      .then(result => {
        if (!currentSession() || controller.signal.aborted) return;
        setSaved(result);
        reset(result.preferences);
      })
      .catch(() => {
        if (currentSession() && !controller.signal.aborted) setLoadError(true);
      })
      .finally(() => {
        if (currentSession() && !controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [attempt, currentSession, reset]);

  const run = async (action: Operation, values?: HankPreferencesValues) => {
    if (!currentSession() || !saved || inFlight.current) return;
    if (action !== 'refresh' && (!saved.can_edit || isHankReadOnlySession() || needsRefresh)) return;
    if (action === 'save' && !values) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    inFlight.current = true;
    setOperation(action);
    setError('');
    setNotice('');
    busyCallback.current?.(true);
    try {
      const command = { expected_company_id: saved.company_id, expected_version: saved.version };
      const result =
        action === 'refresh'
          ? await api.getHankPreferences(controller.signal)
          : action === 'save' && values
            ? await api.updateHankPreferences({ ...command, preferences: values }, controller.signal)
            : await api.resetHankPreferences(command, controller.signal);
      if (!currentSession() || controller.signal.aborted) return;
      if (result.company_id !== saved.company_id) {
        setError('The returned preferences did not match this company. Reopen Hank to continue.');
        setNeedsRefresh(true);
        return;
      }
      setSaved(result);
      reset(result.preferences);
      setNeedsRefresh(false);
      setNotice(
        action === 'save'
          ? 'Preferences saved.'
          : action === 'reset'
            ? 'Default preferences restored.'
            : 'Saved preferences loaded. Review them before making further changes.'
      );
    } catch (cause: unknown) {
      if (!currentSession() || controller.signal.aborted) return;
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(
        typeof detail === 'string'
          ? detail
          : 'Your request was not confirmed. Reload saved preferences to check their current state.'
      );
      const refused = status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429;
      if (action !== 'refresh' && (!refused || status === 409)) setNeedsRefresh(true);
    } finally {
      inFlight.current = false;
      if (controllerRef.current === controller) controllerRef.current = null;
      if (currentSession()) {
        setOperation(null);
        busyCallback.current?.(false);
      }
    }
  };

  if (sessionChanged || !currentSession())
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see preferences for your current company.
      </p>
    );
  if (loading)
    return (
      <p role="status" className="text-sm text-fd-mute">
        Loading your Hank preferences…
      </p>
    );
  if (loadError || !saved)
    return (
      <div role="alert" className="space-y-2 text-sm text-fd-red">
        <p>Your Hank preferences could not be loaded.</p>
        <button type="button" className="btn text-xs" onClick={() => setAttempt(value => value + 1)}>
          Retry preferences
        </button>
      </div>
    );
  const canEdit = saved.can_edit && !isHankReadOnlySession();
  const busy = operation !== null;
  return (
    <section aria-label="Hank preferences" className="space-y-4" aria-busy={busy}>
      <div>
        <h3 className="text-sm font-semibold text-fd-ink">How Hank works with you</h3>
        <p className="mt-1 text-xs text-fd-mute">
          These preferences apply to you in this company. Changes take effect after you save.
        </p>
      </div>
      <p className="text-xs text-fd-mute">
        {saved.version === 0
          ? 'Using defaults. You have no saved preferences yet.'
          : saved.updated_at
            ? `Last saved ${formatCentralDateTime(saved.updated_at)}`
            : 'Your saved preferences are loaded.'}
      </p>
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="text-sm text-fd-body">
          {notice}
        </p>
      )}
      {!canEdit && (
        <p className="text-xs text-fd-mute">
          You can view these preferences. Editing requires an interactive session with write access.
        </p>
      )}
      {needsRefresh && (
        <p className="text-xs text-fd-amber">
          Reload saved preferences before changing them again. Reloading replaces the unsaved selections below with the
          server’s current values.
        </p>
      )}
      <form
        aria-label="Hank preferences form"
        className="space-y-4"
        onSubmit={handleSubmit(values => void run('save', values))}
      >
        <fieldset disabled={!canEdit || busy || needsRefresh} className="space-y-4">
          <FormField
            label="Briefing detail"
            help="Concise shows up to 3 items per section; standard shows up to 5. Source links let you see the full list."
            error={errors.briefing_detail?.message}
          >
            {field => (
              <select {...field} {...register('briefing_detail')} className="input w-full">
                <option value="concise">Concise</option>
                <option value="standard">Standard</option>
              </select>
            )}
          </FormField>
          <FormField
            label="Show this area first"
            help="Moves the area first when available, while keeping other sections and your existing permissions."
            error={errors.focus_area?.message}
          >
            {field => (
              <select {...field} {...register('focus_area')} className="input w-full">
                <option value="role_default">Default for my role</option>
                <option value="my_work">My work</option>
                <option value="shop">Shop</option>
                <option value="quality">Quality</option>
                <option value="purchasing">Purchasing</option>
                <option value="inventory">Inventory</option>
                <option value="shipping">Shipping</option>
              </select>
            )}
          </FormField>
          <FormField
            label="Handoff format"
            help="Sets the format Hank uses for shift handoffs."
            error={errors.handoff_format?.message}
          >
            {field => (
              <select {...field} {...register('handoff_format')} className="input w-full">
                <option value="bullets">Bullets</option>
                <option value="checklist">Checklist</option>
              </select>
            )}
          </FormField>
          <FormField
            label="Follow-up alerts"
            help="Adds a private in-app alert when one of your follow-ups completes. Completed results remain in your task inbox when alerts are off."
            error={errors.follow_up_alerts?.message}
          >
            {field => (
              <input {...field} {...register('follow_up_alerts')} type="checkbox" className="checkbox checkbox-sm" />
            )}
          </FormField>
        </fieldset>
        <div className="flex flex-wrap gap-2">
          {canEdit && (
            <>
              <LoadingButton
                type="submit"
                size="sm"
                disabled={busy || needsRefresh || !isDirty}
                loading={operation === 'save'}
                loadingText="Saving preferences…"
              >
                Save preferences
              </LoadingButton>
              <LoadingButton
                type="button"
                size="sm"
                variant="secondary"
                disabled={busy || needsRefresh || (saved.version === 0 && !isDirty)}
                loading={operation === 'reset'}
                loadingText="Restoring defaults…"
                onClick={() => void run('reset')}
              >
                Restore defaults
              </LoadingButton>
            </>
          )}
          <LoadingButton
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy}
            loading={operation === 'refresh'}
            loadingText="Loading preferences…"
            onClick={() => void run('refresh')}
          >
            Reload saved preferences
          </LoadingButton>
        </div>
      </form>
    </section>
  );
}

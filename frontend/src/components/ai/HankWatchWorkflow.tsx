import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { isAxiosError } from 'axios';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankCapabilities, HankTask } from '../../types/hankTasks';
import type { HankWatchCommand, HankWatchCreate } from '../../types/hankWatches';
import type { HankPreferencesResponse } from '../../types/hankPreferences';
import { formatCentralDateTime } from '../../utils/centralTime';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from './hankSession';

const formSchema = z.object({
  work_order_id: z
    .string()
    .refine(value => /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)), 'Select a work order.'),
  condition: z.enum(['blockers_cleared', 'pdf_attached']),
  document_type: z.string(),
});
type WatchForm = z.infer<typeof formSchema>;
type Operation = HankWatchCommand | 'create' | 'refresh';
const LABELS: Record<HankWatchCommand, string> = {
  check: 'Check now',
  snooze: 'Snooze 1 hour',
  resume: 'Resume',
  cancel: 'Stop follow-up',
};

export interface HankWatchWorkflowProps {
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
  initialTask?: HankTask;
  onTaskChanged?: (task: HankTask) => void;
}

export function HankWatchWorkflow({ onNavigate, onBusyChange, initialTask, onTaskChanged }: HankWatchWorkflowProps) {
  const [scope] = useState(getHankSessionScope);
  const [sessionChanged, setSessionChanged] = useState(false);
  const [capabilities, setCapabilities] = useState<HankCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [capabilityError, setCapabilityError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [task, setTask] = useState<HankTask | null>(initialTask || null);
  const [pendingCreate, setPendingCreate] = useState<HankWatchCreate | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [notice, setNotice] = useState('');
  const [documentTypes, setDocumentTypes] = useState<Array<{ value: string; label: string }>>([]);
  const [typesLoading, setTypesLoading] = useState(false);
  const [typesError, setTypesError] = useState(false);
  const [typesAttempt, setTypesAttempt] = useState(0);
  const [alertPreferences, setAlertPreferences] = useState<HankPreferencesResponse | null>(null);
  const [preferencesError, setPreferencesError] = useState(false);
  const [preferencesAttempt, setPreferencesAttempt] = useState(0);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const capabilityController = useRef<AbortController | null>(null);
  const preferencesController = useRef<AbortController | null>(null);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const {
    control,
    register,
    watch,
    handleSubmit,
    formState: { errors },
  } = useForm<WatchForm>({
    resolver: zodResolver(formSchema),
    defaultValues: { work_order_id: '', condition: 'blockers_cleared', document_type: '' },
  });
  const condition = watch('condition');
  const documentType = watch('document_type');
  const currentSession = useCallback(
    () => mounted.current && scope !== null && scope === getHankSessionScope(),
    [scope]
  );
  const canWatch = !!capabilities?.can_watch && !isHankReadOnlySession();
  const busy = operation !== null;

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (scope !== getHankSessionScope()) {
        controllerRef.current?.abort();
        capabilityController.current?.abort();
        preferencesController.current?.abort();
        setSessionChanged(true);
        busyCallback.current?.(false);
      }
    });
    return () => {
      mounted.current = false;
      controllerRef.current?.abort();
      capabilityController.current?.abort();
      preferencesController.current?.abort();
      busyCallback.current?.(false);
      unsubscribe();
    };
  }, [scope]);

  useEffect(() => {
    const controller = new AbortController();
    capabilityController.current = controller;
    setLoading(true);
    setCapabilityError(false);
    api
      .getHankCapabilities(controller.signal)
      .then(result => {
        if (currentSession() && !controller.signal.aborted) setCapabilities(result);
      })
      .catch(() => {
        if (currentSession() && !controller.signal.aborted) setCapabilityError(true);
      })
      .finally(() => {
        if (currentSession() && !controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [attempt, currentSession]);

  useEffect(() => {
    const controller = new AbortController();
    preferencesController.current = controller;
    setPreferencesError(false);
    setAlertPreferences(null);
    api
      .getHankPreferences(controller.signal)
      .then(result => {
        if (currentSession() && !controller.signal.aborted) setAlertPreferences(result);
      })
      .catch(() => {
        if (currentSession() && !controller.signal.aborted) setPreferencesError(true);
      });
    return () => controller.abort();
  }, [preferencesAttempt, currentSession]);

  useEffect(() => {
    if (task || !canWatch || condition !== 'pdf_attached') return;
    let active = true;
    setTypesLoading(true);
    setTypesError(false);
    api
      .getDocumentTypes()
      .then((types: Array<{ value: string; label: string }>) => {
        if (active && currentSession()) setDocumentTypes(types);
      })
      .catch(() => {
        if (active && currentSession()) setTypesError(true);
      })
      .finally(() => {
        if (active && currentSession()) setTypesLoading(false);
      });
    return () => {
      active = false;
    };
  }, [condition, canWatch, task, typesAttempt, currentSession]);

  useEffect(() => {
    if (initialTask && !inFlight.current) {
      setTask(initialTask);
      setPendingCreate(null);
    }
  }, [initialTask]);

  const run = async (action: Operation, body?: HankWatchCreate) => {
    if (inFlight.current || !currentSession() || !capabilities) return;
    if (action !== 'refresh' && !canWatch) return;
    if (action === 'create' && !body) return;
    if (
      action !== 'create' &&
      (!task || task.company_id !== capabilities.company_id || task.kind !== 'watch_work_order')
    )
      return;
    if (action !== 'create' && action !== 'refresh' && needsRefresh) return;
    inFlight.current = true;
    const controller = new AbortController();
    controllerRef.current = controller;
    setOperation(action);
    busyCallback.current?.(true);
    setError('');
    setNotice('');
    try {
      let saved: HankTask;
      if (action === 'create' && body) saved = await api.createHankWatch(body, controller.signal);
      else if (action === 'refresh' && task) saved = await api.getHankTask(task.id, controller.signal);
      else if (task) {
        const command = { expected_company_id: capabilities.company_id, expected_version: task.version };
        switch (action) {
          case 'check':
            saved = await api.checkHankWatch(task.id, command, controller.signal);
            break;
          case 'snooze':
            saved = await api.snoozeHankWatch(task.id, command, controller.signal);
            break;
          case 'resume':
            saved = await api.resumeHankWatch(task.id, command, controller.signal);
            break;
          case 'cancel':
            saved = await api.cancelHankWatch(task.id, command, controller.signal);
            break;
          default:
            return;
        }
      } else return;
      if (!currentSession() || controller.signal.aborted) return;
      if (saved.company_id !== capabilities.company_id || saved.kind !== 'watch_work_order') {
        setError('The returned follow-up did not match this workspace. Reopen it from your task inbox.');
        setNeedsRefresh(true);
        return;
      }
      setTask(saved);
      setPendingCreate(null);
      setNeedsRefresh(false);
      if (action === 'refresh') setPreferencesAttempt(value => value + 1);
      if (action === 'check' && saved.status === 'watching') setNotice('Check complete. The condition is not yet met.');
      if (action === 'resume') setNotice('Follow-up resumed. Check now to see the current condition.');
      onTaskChanged?.(saved);
    } catch (cause: unknown) {
      if (!currentSession() || controller.signal.aborted) return;
      const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(
        typeof detail === 'string' ? detail : 'The request was not confirmed. Recover its status before continuing.'
      );
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      const refused = status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429;
      if (action === 'create' && refused) setPendingCreate(null);
      if (action !== 'create' && action !== 'refresh' && (!refused || status === 409)) setNeedsRefresh(true);
    } finally {
      inFlight.current = false;
      if (controllerRef.current === controller) controllerRef.current = null;
      if (currentSession()) {
        setOperation(null);
        busyCallback.current?.(false);
      }
    }
  };

  const start = handleSubmit(values => {
    if (!currentSession() || !capabilities || !canWatch || inFlight.current) return;
    if (pendingCreate) {
      void run('create', pendingCreate);
      return;
    }
    const body: HankWatchCreate = {
      expected_company_id: capabilities.company_id,
      request_key: crypto.randomUUID(),
      work_order_id: Number(values.work_order_id),
      condition: values.condition,
      document_type: values.condition === 'pdf_attached' ? values.document_type || null : null,
    };
    setPendingCreate(body);
    void run('create', body);
  });

  if (sessionChanged || !currentSession())
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see follow-ups for your current company.
      </p>
    );
  if (loading)
    return (
      <p role="status" className="text-sm text-fd-mute">
        Checking follow-up access…
      </p>
    );
  if (capabilityError || !capabilities)
    return (
      <div role="alert" className="space-y-2 text-sm text-fd-red">
        <p>Follow-up access could not be loaded.</p>
        <button type="button" className="btn text-xs" onClick={() => setAttempt(value => value + 1)}>
          Retry follow-up access
        </button>
      </div>
    );
  if (task && (task.company_id !== capabilities.company_id || task.kind !== 'watch_work_order'))
    return (
      <p role="alert" className="text-sm text-fd-amber">
        This follow-up does not belong to this workspace. Reopen it from your task inbox.
      </p>
    );

  const display = task?.status === 'completed' && task.result ? task.result : task?.preview;
  const alertsEnabled =
    alertPreferences?.company_id === capabilities.company_id ? alertPreferences.preferences.follow_up_alerts : null;
  const commands: HankWatchCommand[] =
    task?.status === 'watching'
      ? ['check', 'snooze', 'cancel']
      : task && ['snoozed', 'needs_attention'].includes(task.status)
        ? ['resume', 'cancel']
        : [];
  return (
    <section aria-label="Follow up with Hank" className="space-y-4" aria-busy={busy}>
      <div className="space-y-1 text-xs text-fd-mute">
        <p>
          {alertsEnabled === false
            ? 'Follow-up alerts are off in your current Hank preferences. Completed results stay in Tasks.'
            : alertsEnabled === true
              ? 'Your current Hank preferences enable private in-app alerts when a follow-up completes.'
              : 'In-app alerts follow your Hank preferences; results stay in Tasks.'}
        </p>
        {preferencesError && (
          <button
            type="button"
            className="underline"
            disabled={busy}
            onClick={() => setPreferencesAttempt(value => value + 1)}
          >
            Retry alert preference
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="text-xs text-fd-body">
          {notice}
        </p>
      )}
      {needsRefresh && (
        <p className="text-xs text-fd-amber">
          Refresh follow-up status before taking another action. The last request may have completed or the saved
          version may have changed.
        </p>
      )}
      {task ? (
        <>
          <div>
            <h3 className="text-sm font-semibold text-fd-ink">{task.title}</h3>
            <p className="mt-1 text-xs text-fd-mute">
              {task.status.replace(/_/g, ' ')} · Updated {formatCentralDateTime(task.updated_at)}
            </p>
          </div>
          {display && (
            <div className="space-y-2 border border-slate-700 p-3 rounded-[3px]">
              <h4 className="text-xs font-semibold text-fd-ink">
                {task.status === 'completed' ? 'Follow-up result' : 'Saved follow-up'}
              </h4>
              <p className="text-sm text-fd-body">{display.summary}</p>
              {task.status !== 'completed' &&
                task.preview.changes.map((change, index) => (
                  <p key={index} className="text-xs text-fd-body">
                    {change}
                  </p>
                ))}
              {display.warnings.map((warning, index) => (
                <p key={index} className="text-xs text-fd-amber">
                  {warning}
                </p>
              ))}
              <div className="flex flex-wrap gap-2">
                {display.references.map(reference => (
                  <Link
                    key={`${reference.type}:${reference.id}`}
                    to={reference.url}
                    onClick={onNavigate}
                    className="text-xs text-fd-blue underline"
                  >
                    {reference.label}
                  </Link>
                ))}
              </div>
            </div>
          )}
          <p className="text-xs text-fd-mute">
            {task.last_checked_at
              ? `Last checked ${formatCentralDateTime(task.last_checked_at)}`
              : ['watching', 'snoozed'].includes(task.status)
                ? 'Waiting for first check'
                : 'Not checked'}
          </p>
          {task.status === 'snoozed' && task.snoozed_until && (
            <p className="text-xs text-fd-mute">Snoozed until {formatCentralDateTime(task.snoozed_until)}</p>
          )}
          {['watching', 'snoozed'].includes(task.status) && (
            <p className="text-xs text-fd-mute">
              The last check time shows how fresh this status is.{' '}
              {task.status === 'snoozed'
                ? 'Resume to check the current condition.'
                : 'Use Check now for a current check.'}
            </p>
          )}
          {task.error_message && (
            <p role="alert" className="text-sm text-fd-amber">
              {task.error_message}
            </p>
          )}
          {!canWatch && (
            <p className="text-xs text-fd-mute">
              Follow-up controls are unavailable in this session. You can review its saved status.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {canWatch &&
              commands.map(command => (
                <LoadingButton
                  key={command}
                  type="button"
                  size="sm"
                  variant={command === 'cancel' ? 'secondary' : 'primary'}
                  disabled={busy || needsRefresh}
                  loading={operation === command}
                  loadingText="Saving…"
                  onClick={() => void run(command)}
                >
                  {LABELS[command]}
                </LoadingButton>
              ))}
            <LoadingButton
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              loading={operation === 'refresh'}
              loadingText="Refreshing…"
              onClick={() => void run('refresh')}
            >
              Refresh follow-up status
            </LoadingButton>
          </div>
        </>
      ) : !canWatch ? (
        <p className="text-sm text-fd-mute">
          Follow-ups require an interactive session with permission to view work orders. They are unavailable in this
          session.
        </p>
      ) : (
        <form aria-label="Start a follow-up with Hank" onSubmit={start} className="space-y-4">
          <h3 className="text-sm font-semibold text-fd-ink">New follow-up</h3>
          <fieldset disabled={busy || !!pendingCreate} className="space-y-4">
            <FormField label="Work order to follow" required error={errors.work_order_id?.message}>
              {field => (
                <Controller
                  name="work_order_id"
                  control={control}
                  render={({ field: input }) => (
                    <EntityPicker
                      {...field}
                      kind="workOrder"
                      value={input.value}
                      onChange={input.onChange}
                      disabled={busy || !!pendingCreate}
                    />
                  )}
                />
              )}
            </FormField>
            <FormField label="Follow until" required>
              {field => (
                <select {...field} {...register('condition')} className="input w-full">
                  <option value="blockers_cleared">Active blockers are cleared</option>
                  <option value="pdf_attached">A new PDF is attached</option>
                </select>
              )}
            </FormField>
            {condition === 'pdf_attached' && (
              <>
                <FormField label="PDF document type" help="Only PDFs newly attached after this follow-up starts count.">
                  {field => (
                    <select
                      {...field}
                      {...register('document_type')}
                      disabled={busy || !!pendingCreate || typesLoading}
                      className="input w-full"
                    >
                      <option value="">Any PDF document type</option>
                      {documentTypes.map(type => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </select>
                  )}
                </FormField>
                {typesLoading && (
                  <p role="status" className="text-xs text-fd-mute">
                    Loading document types…
                  </p>
                )}
                {typesError && (
                  <p role="alert" className="text-xs text-fd-amber">
                    Document types could not be loaded. You can follow any PDF, or{' '}
                    <button type="button" className="underline" onClick={() => setTypesAttempt(value => value + 1)}>
                      retry document types
                    </button>
                    .
                  </p>
                )}
              </>
            )}
          </fieldset>
          <div className="space-y-2 border border-slate-700 p-3 text-xs rounded-[3px]">
            <p className="font-semibold text-fd-ink">Condition to follow</p>
            <p className="text-fd-body">
              {condition === 'blockers_cleared'
                ? 'Follow the selected work order until no active blockers remain.'
                : `Follow the selected work order until a new PDF${documentType ? ` of type ${documentTypes.find(type => type.value === documentType)?.label || documentType}` : ''} is attached.`}
            </p>
            <p className="text-fd-mute">
              {condition === 'blockers_cleared'
                ? 'Cleared blockers do not establish that the job is ready to run.'
                : 'A matching attachment does not confirm its contents, approval, or release.'}
            </p>
            <p className="text-fd-mute">
              This saves a follow-up in your task inbox. Periodic checks may be delayed; use Check now for a current
              check.
            </p>
          </div>
          {pendingCreate && (
            <p className="text-xs text-fd-amber">
              If the response was interrupted, retry this same follow-up to recover it without creating another.
            </p>
          )}
          {pendingCreate ? (
            <LoadingButton
              type="button"
              size="sm"
              loading={busy}
              loadingText="Starting follow-up…"
              onClick={() => void run('create', pendingCreate)}
            >
              Retry starting follow-up
            </LoadingButton>
          ) : (
            <LoadingButton type="submit" size="sm" loading={busy} loadingText="Starting follow-up…">
              Start follow-up
            </LoadingButton>
          )}
        </form>
      )}
    </section>
  );
}

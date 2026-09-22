import React, { useEffect, useRef, useState } from 'react';
import { useFieldArray, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Link } from 'react-router-dom';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import type { HankCapabilities, HankTaskCommand } from '../../types/hankTasks';
import type { HankRoutine, HankRoutineValues, HankRoutineRun, HankRoutineStepKind } from '../../types/hankWork';
import { formatCentralDateTime } from '../../utils/centralTime';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { useHankSessionGuard } from './useHankSessionGuard';

const KINDS: HankRoutineStepKind[] = [
  'readiness',
  'knowledge',
  'document_intake',
  'receive_delivery',
  'report_production',
  'shipping_packet',
  'draft_shipment',
  'purchasing_impact',
  'handoff',
  'checklist',
];
const schema = z.object({
  title: z.string().trim().min(1).max(160),
  description: z.string().max(2000),
  steps: z
    .array(
      z.object({
        kind: z.enum([
          'readiness',
          'knowledge',
          'document_intake',
          'receive_delivery',
          'report_production',
          'shipping_packet',
          'draft_shipment',
          'purchasing_impact',
          'handoff',
          'checklist',
        ]),
        title: z.string().trim().min(1).max(160),
        instruction: z.string().trim().min(1).max(1000),
      })
    )
    .min(1)
    .max(12),
});
function requestError(cause: unknown) {
  const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
  return typeof detail === 'string'
    ? detail
    : 'The request was not confirmed. Reload the saved state or retry the same request.';
}
function requiresRefresh(cause: unknown) {
  const status = isAxiosError(cause) ? cause.response?.status : undefined;
  return !status || status === 409 || status === 408 || status === 429 || status >= 500;
}
type StepOpen = (kind: HankRoutineStepKind, run: HankRoutineRun) => void;

function RoutineEditor({
  companyId,
  initial,
  template,
  onSaved,
  onBusyChange,
}: {
  companyId: number;
  initial?: HankRoutine;
  template?: HankRoutineValues;
  onSaved: (routine: HankRoutine) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const { current, controller, release, changed } = useHankSessionGuard();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const [pending, setPending] = useState<
    (HankRoutineValues & { expected_company_id: number; request_key: string }) | null
  >(null);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const flight = useRef(false);
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<HankRoutineValues>({
    resolver: zodResolver(schema),
    defaultValues: initial ||
      template || { title: '', description: '', steps: [{ kind: 'checklist', title: '', instruction: '' }] },
  });
  const { fields, append, remove, move } = useFieldArray({ control, name: 'steps' });
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    callback.current?.(busy || !!pending);
  }, [busy, pending]);
  const save = async (values: HankRoutineValues) => {
    if (!current() || flight.current || stale) return;
    flight.current = true;
    const request = controller();
    setBusy(true);
    callback.current?.(true);
    setError('');
    const body = pending || { ...values, expected_company_id: companyId, request_key: crypto.randomUUID() };
    if (!initial) setPending(body);
    try {
      const result = initial
        ? await api.updateHankRoutine(
            initial.id,
            { ...values, expected_company_id: companyId, expected_version: initial.version },
            request.signal
          )
        : await api.createHankRoutine(body, request.signal);
      if (current() && !request.signal.aborted) {
        setPending(null);
        onSaved(result);
      }
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        setError(requestError(cause));
        if (initial && requiresRefresh(cause)) setStale(true);
        if (!requiresRefresh(cause) || (isAxiosError(cause) && cause.response?.status === 409)) setPending(null);
      }
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
      }
    }
  };
  if (changed) return null;
  return (
    <form aria-label="Routine editor" onSubmit={handleSubmit(save)} className="space-y-4">
      <p className="text-xs text-fd-mute">
        Saving creates a draft. An authorized employee must approve this exact version before anyone starts it. Editing
        an approved routine clears its approval.
      </p>
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
      {stale && (
        <p className="text-xs text-fd-amber">
          Return to the routine and refresh its saved version before editing again.
        </p>
      )}
      <fieldset disabled={busy || !!pending || stale} className="space-y-3">
        <FormField label="Routine title" required error={errors.title?.message}>
          {field => <input {...field} {...register('title')} className="input w-full" />}
        </FormField>
        <FormField label="Purpose" error={errors.description?.message}>
          {field => <textarea {...field} {...register('description')} rows={3} className="input w-full h-auto" />}
        </FormField>
        {fields.map((item, index) => (
          <div key={item.id} className="space-y-2 border border-slate-700 p-3">
            <p className="text-xs font-semibold text-fd-ink">Step {index + 1}</p>
            <FormField label={`Step ${index + 1} type`}>
              {field => (
                <select {...field} {...register(`steps.${index}.kind`)} className="input w-full">
                  {KINDS.map(kind => (
                    <option key={kind} value={kind}>
                      {kind.replace(/_/g, ' ')}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label={`Step ${index + 1} title`} required error={errors.steps?.[index]?.title?.message}>
              {field => <input {...field} {...register(`steps.${index}.title`)} className="input w-full" />}
            </FormField>
            <FormField
              label={`Step ${index + 1} instructions`}
              required
              error={errors.steps?.[index]?.instruction?.message}
            >
              {field => (
                <textarea
                  {...field}
                  {...register(`steps.${index}.instruction`)}
                  rows={3}
                  className="input w-full h-auto"
                />
              )}
            </FormField>
            <div className="flex gap-3">
              <button
                type="button"
                className="text-xs underline"
                disabled={!index}
                onClick={() => move(index, index - 1)}
              >
                Move up
              </button>
              <button
                type="button"
                className="text-xs underline"
                disabled={index === fields.length - 1}
                onClick={() => move(index, index + 1)}
              >
                Move down
              </button>
              <button
                type="button"
                className="text-xs underline"
                disabled={fields.length === 1}
                onClick={() => remove(index)}
              >
                Remove step
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="btn text-xs"
          disabled={fields.length >= 12}
          onClick={() => append({ kind: 'checklist', title: '', instruction: '' })}
        >
          Add step
        </button>
      </fieldset>
      {pending ? (
        <LoadingButton type="button" size="sm" loading={busy} onClick={() => void save(pending)}>
          Retry same routine draft
        </LoadingButton>
      ) : (
        <LoadingButton type="submit" size="sm" loading={busy} disabled={stale}>
          Save routine draft
        </LoadingButton>
      )}
    </form>
  );
}

function RoutineRun({
  initial,
  onNavigate,
  onBusyChange,
  onOpenStep,
}: {
  initial: HankRoutineRun;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
  onOpenStep: StepOpen;
}) {
  const [run, setRun] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [choices, setChoices] = useState<Array<{ id: number; label: string }>>([]);
  const [choiceError, setChoiceError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { current, controller, release, changed } = useHankSessionGuard();
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const flight = useRef(false);
  const { register, handleSubmit, reset } = useForm<{ note: string; evidence_id: string }>({
    resolver: zodResolver(z.object({ note: z.string().max(2000), evidence_id: z.string() })),
    defaultValues: { note: '', evidence_id: '' },
  });
  const step = run.steps[run.current_step];
  const actionStep =
    step &&
    ['receive_delivery', 'report_production', 'draft_shipment', 'document_intake', 'handoff'].includes(step.kind);
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    reset({ note: '', evidence_id: '' });
  }, [run.current_step, reset]);
  useEffect(() => {
    if (!step || !actionStep || run.status !== 'active') return;
    const request = controller();
    setChoices([]);
    setChoiceError(false);
    const load = async () => {
      if (step.kind === 'document_intake') {
        const result = await api.getHankIntakes({ limit: 50 }, request.signal);
        return result.batches
          .flatMap(batch => batch.files)
          .filter(
            file =>
              file.status === 'completed' &&
              (!run.work_order_id || file.plan?.input.work_order_id === run.work_order_id)
          )
          .map(file => ({ id: file.id, label: `${file.filename} · ${file.result?.document_number || 'filed'}` }));
      }
      if (step.kind === 'handoff') {
        const result = await api.getHankHandoffs({ status: 'completed', limit: 50 }, request.signal);
        return result.handoffs
          .filter(item => !run.work_order_id || item.work_order_id === run.work_order_id)
          .map(item => ({ id: item.id, label: `${item.work_order_number} · ${item.summary}` }));
      }
      const result = await api.getHankTasks({ status: 'completed', limit: 50 }, request.signal);
      return result.tasks
        .filter(
          task =>
            task.kind === step.kind &&
            (step.kind !== 'receive_delivery' ||
              !run.purchase_order_id ||
              task.input.purchase_order_id === run.purchase_order_id) &&
            (step.kind !== 'draft_shipment' || !run.work_order_id || task.input.work_order_id === run.work_order_id)
        )
        .map(task => ({ id: task.id, label: task.title }));
    };
    void load()
      .then(items => {
        if (current() && !request.signal.aborted) setChoices(items);
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setChoiceError(true);
      })
      .finally(() => release(request));
    return () => request.abort();
  }, [step, actionStep, run.status, run.work_order_id, run.purchase_order_id, attempt, current, controller, release]);
  const command = async (action: 'refresh' | 'advance' | 'cancel', values?: { note: string; evidence_id: string }) => {
    if (!current() || flight.current || (action !== 'refresh' && (!run.can_edit || needsRefresh))) return;
    if (action === 'advance' && (!values || (actionStep ? !values.evidence_id : !values.note.trim()))) {
      setError(actionStep ? 'Choose a completed record for this step.' : 'Record what you reviewed or performed.');
      return;
    }
    flight.current = true;
    const request = controller();
    setBusy(true);
    callback.current?.(true);
    setError('');
    try {
      const body: HankTaskCommand = { expected_company_id: run.company_id, expected_version: run.version };
      const evidence =
        actionStep && values?.evidence_id
          ? {
              [step.kind === 'document_intake' ? 'intake_file_id' : step.kind === 'handoff' ? 'handoff_id' : 'task_id']:
                Number(values.evidence_id),
            }
          : {};
      const result =
        action === 'refresh'
          ? await api.getHankRoutineRun(run.id, request.signal)
          : action === 'cancel'
            ? await api.cancelHankRoutineRun(run.id, body, request.signal)
            : await api.advanceHankRoutine(run.id, { ...body, note: values!.note, ...evidence }, request.signal);
      if (current() && !request.signal.aborted) {
        setRun(result);
        setNeedsRefresh(false);
      }
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        setError(requestError(cause));
        if (action !== 'refresh' && requiresRefresh(cause)) setNeedsRefresh(true);
      }
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
        callback.current?.(false);
      }
    }
  };
  if (changed) return null;
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink">{run.title}</h3>
      <p className="text-xs text-fd-mute">
        {run.status} · Approved routine version {run.routine_version} · Updated {formatCentralDateTime(run.updated_at)}
      </p>
      <p className="text-xs text-fd-mute">
        Each step requires your review or a saved completion receipt. Routine approval does not expand your permissions.
      </p>
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
      {needsRefresh && <p className="text-xs text-fd-amber">Refresh this run before taking another action.</p>}
      <ol className="space-y-2">
        {run.steps.map((item, index) => (
          <li
            key={index}
            className={`p-3 border ${index === run.current_step && run.status === 'active' ? 'border-blue-700' : 'border-slate-700'}`}
          >
            <p className="text-xs font-semibold text-fd-ink">
              {index + 1}. {item.title} ·{' '}
              {index < run.current_step
                ? 'finished'
                : index === run.current_step && run.status === 'active'
                  ? 'waiting on you'
                  : 'not started'}
            </p>
            <p className="mt-1 text-xs text-fd-body">{item.instruction}</p>
            {run.results
              .filter(result => result.step_index === index)
              .map(result => (
                <div key={result.step_index} className="mt-2 space-y-1">
                  <p className="text-xs text-fd-mute">{result.note}</p>
                  {result.evidence.map(reference => (
                    <Link
                      key={`${reference.type}:${reference.id}`}
                      to={reference.url}
                      onClick={onNavigate}
                      className="text-xs text-fd-blue underline mr-2"
                    >
                      {reference.label}
                    </Link>
                  ))}
                </div>
              ))}
          </li>
        ))}
      </ol>
      {step && run.status === 'active' && run.can_edit && (
        <form
          aria-label="Review routine step"
          className="space-y-3"
          onSubmit={handleSubmit(values => void command('advance', values))}
        >
          {step.kind !== 'checklist' && (
            <button
              type="button"
              className="btn text-xs"
              disabled={busy || needsRefresh}
              onClick={() => onOpenStep(step.kind, run)}
            >
              Open current step workspace
            </button>
          )}
          {actionStep && (
            <FormField
              label="Completed evidence"
              help="Recent completed records are listed. The server verifies the step type and job or PO context."
            >
              {field => (
                <select
                  {...field}
                  {...register('evidence_id')}
                  disabled={busy || needsRefresh}
                  className="input w-full"
                >
                  <option value="">Choose saved evidence…</option>
                  {choices.map(choice => (
                    <option key={choice.id} value={choice.id}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          )}
          {choiceError && (
            <p role="alert" className="text-xs text-fd-red">
              Evidence could not be loaded.
            </p>
          )}
          {actionStep && (
            <button
              type="button"
              className="text-xs text-fd-blue underline"
              disabled={busy}
              onClick={() => setAttempt(value => value + 1)}
            >
              Refresh evidence choices
            </button>
          )}
          <FormField label="Step review note" required={!actionStep}>
            {field => (
              <textarea
                {...field}
                {...register('note')}
                disabled={busy || needsRefresh}
                className="input w-full h-auto"
                rows={3}
              />
            )}
          </FormField>
          <LoadingButton type="submit" size="sm" disabled={busy || needsRefresh}>
            Confirm this step
          </LoadingButton>
        </form>
      )}
      <div className="flex flex-wrap gap-2">
        <LoadingButton type="button" size="sm" variant="ghost" loading={busy} onClick={() => void command('refresh')}>
          Refresh routine run
        </LoadingButton>
        {run.status === 'active' && run.can_edit && (
          <button
            type="button"
            className="text-xs underline"
            disabled={busy || needsRefresh}
            onClick={() => void command('cancel')}
          >
            Cancel routine run
          </button>
        )}
      </div>
    </div>
  );
}

export function HankRoutines({
  initialId,
  workOrderId,
  purchaseOrderId,
  onNavigate,
  onBusyChange,
  onOpenStep,
}: {
  initialId?: number;
  workOrderId?: number;
  purchaseOrderId?: number;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
  onOpenStep: StepOpen;
}) {
  const [catalog, setCatalog] = useState<Awaited<ReturnType<typeof api.getHankRoutines>> | null>(null);
  const [cap, setCap] = useState<HankCapabilities | null>(null);
  const [runs, setRuns] = useState<HankRoutineRun[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [selected, setSelected] = useState<HankRoutine | null>(null);
  const [activeRun, setActiveRun] = useState<HankRoutineRun | null>(null);
  const [deepId, setDeepId] = useState(initialId);
  useEffect(() => {
    setDeepId(initialId);
  }, [initialId]);
  const [template, setTemplate] = useState<HankRoutineValues>();
  const [editing, setEditing] = useState(false);
  const [job, setJob] = useState(String(workOrderId || ''));
  const [po, setPO] = useState(String(purchaseOrderId || ''));
  const [pendingStart, setPendingStart] = useState<
    (HankTaskCommand & { request_key: string; work_order_id?: number; purchase_order_id?: number }) | null
  >(null);
  const [busy, setBusy] = useState(false);
  const [childBusy, setChildBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { current, controller, release, changed } = useHankSessionGuard();
  const flight = useRef(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    callback.current?.(busy || childBusy || !!pendingStart);
  }, [busy, childBusy, pendingStart]);
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setError('');
    Promise.all([
      api.getHankRoutines(request.signal),
      api.getHankCapabilities(request.signal),
      api.getHankRoutineRuns({ limit: 20 }, request.signal),
      deepId ? api.getHankRoutineRun(deepId, request.signal) : Promise.resolve(null),
    ])
      .then(([list, capabilities, history, run]) => {
        if (current() && !request.signal.aborted) {
          setCatalog(list);
          setCap(capabilities);
          setRuns(history.runs);
          setCursor(history.has_more ? history.next_before_id : null);
          if (run) setActiveRun(run);
        }
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setError('Routines could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [deepId, attempt, current, controller, release]);
  const command = async (action: 'approve' | 'archive' | 'start' | 'refresh') => {
    if (!selected || !cap || !current() || flight.current || (needsRefresh && action !== 'refresh')) return;
    const request = controller();
    flight.current = true;
    setBusy(true);
    callback.current?.(true);
    setError('');
    try {
      if (action === 'start') {
        const body = pendingStart || {
          expected_company_id: cap.company_id,
          expected_version: selected.version,
          request_key: crypto.randomUUID(),
          work_order_id: job ? Number(job) : undefined,
          purchase_order_id: po ? Number(po) : undefined,
        };
        setPendingStart(body);
        const result = await api.startHankRoutine(selected.id, body, request.signal);
        if (current() && !request.signal.aborted) {
          setActiveRun(result);
          setPendingStart(null);
          setNeedsRefresh(false);
        }
      } else {
        const result =
          action === 'refresh'
            ? await api.getHankRoutine(selected.id, request.signal)
            : await api.commandHankRoutine(
                selected.id,
                action,
                { expected_company_id: cap.company_id, expected_version: selected.version },
                request.signal
              );
        if (current() && !request.signal.aborted) {
          setSelected(result);
          setNeedsRefresh(false);
          setCatalog(
            value =>
              value && { ...value, routines: value.routines.map(item => (item.id === result.id ? result : item)) }
          );
        }
      }
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        setError(requestError(cause));
        if (
          action === 'start' &&
          (!requiresRefresh(cause) || (isAxiosError(cause) && cause.response?.status === 409))
        ) {
          setPendingStart(null);
          if (isAxiosError(cause) && cause.response?.status === 409) setNeedsRefresh(true);
        }
        if (action !== 'start' && action !== 'refresh' && requiresRefresh(cause)) setNeedsRefresh(true);
      }
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
      }
    }
  };
  const openRun = async (id: number) => {
    if (!current() || busy) return;
    const request = controller();
    setLoading(true);
    setError('');
    try {
      const result = await api.getHankRoutineRun(id, request.signal);
      if (current() && !request.signal.aborted) setActiveRun(result);
    } catch {
      if (current()) setError('The saved routine run could not be loaded.');
    } finally {
      release(request);
      if (current()) setLoading(false);
    }
  };
  const moreRuns = async () => {
    if (!cursor || loading || !current()) return;
    const request = controller();
    setLoading(true);
    try {
      const result = await api.getHankRoutineRuns({ limit: 20, before_id: cursor }, request.signal);
      if (current() && !request.signal.aborted) {
        setRuns(previous => [...previous, ...result.runs]);
        setCursor(result.has_more ? result.next_before_id : null);
      }
    } catch {
      if (current()) setError('Older routine runs could not be loaded.');
    } finally {
      release(request);
      if (current()) setLoading(false);
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-xs text-fd-amber">
        Your session changed. Reopen Hank to see routines.
      </p>
    );
  const childChange = (value: boolean) => {
    setChildBusy(value);
  };
  return (
    <section aria-label="Approved routines" className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-fd-ink flex-1">Approved routines</h3>
        {(selected || activeRun || editing) && (
          <button
            type="button"
            disabled={busy || childBusy || !!pendingStart}
            className="text-xs text-fd-blue underline"
            onClick={() => {
              setDeepId(undefined);
              setSelected(null);
              setActiveRun(null);
              setEditing(false);
              setPendingStart(null);
              setNeedsRefresh(false);
              setAttempt(value => value + 1);
            }}
          >
            Routine library
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
      {loading && (
        <p role="status" className="text-xs text-fd-mute">
          Loading routines…
        </p>
      )}
      {!catalog && !loading && (
        <button type="button" className="btn text-xs" onClick={() => setAttempt(value => value + 1)}>
          Retry routines
        </button>
      )}
      {activeRun ? (
        <RoutineRun
          key={activeRun.id}
          initial={activeRun}
          onNavigate={onNavigate}
          onBusyChange={childChange}
          onOpenStep={onOpenStep}
        />
      ) : editing && cap ? (
        <RoutineEditor
          key={selected?.id || template?.title || 'new'}
          companyId={cap.company_id}
          initial={selected || undefined}
          template={template}
          onBusyChange={childChange}
          onSaved={saved => {
            setSelected(saved);
            setEditing(false);
            setCatalog(
              value => value && { ...value, routines: [saved, ...value.routines.filter(item => item.id !== saved.id)] }
            );
          }}
        />
      ) : selected ? (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold text-fd-ink">{selected.title}</h4>
          <p className="text-xs text-fd-mute">
            {selected.status} · Version {selected.version}
          </p>
          <p className="text-xs text-fd-body">{selected.description}</p>
          <ol className="space-y-2">
            {selected.steps.map((step, index) => (
              <li key={index} className="border border-slate-700 p-3">
                <p className="text-xs font-semibold text-fd-ink">
                  {index + 1}. {step.title}
                </p>
                <p className="text-xs text-fd-body">{step.instruction}</p>
              </li>
            ))}
          </ol>
          {needsRefresh && <p className="text-xs text-fd-amber">Refresh this routine before taking another action.</p>}
          {selected.status === 'approved' && cap?.can_watch && (
            <div className="space-y-3">
              <FormField label="Routine work order (optional)">
                {field => (
                  <EntityPicker
                    {...field}
                    kind="workOrder"
                    value={job}
                    onChange={setJob}
                    disabled={busy || !!pendingStart}
                    optional
                  />
                )}
              </FormField>
              <FormField label="Routine purchase order (optional)">
                {field => (
                  <HankPurchaseOrderPicker {...field} value={po} onChange={setPO} disabled={busy || !!pendingStart} />
                )}
              </FormField>
              <LoadingButton
                type="button"
                size="sm"
                loading={busy}
                disabled={needsRefresh}
                onClick={() => void command('start')}
              >
                {pendingStart ? 'Retry starting this routine' : 'Start approved routine'}
              </LoadingButton>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {selected.can_approve && selected.status === 'draft' && (
              <button
                type="button"
                className="btn text-xs"
                disabled={busy || needsRefresh || !!pendingStart}
                onClick={() => void command('approve')}
              >
                Approve this version
              </button>
            )}
            {selected.can_manage && selected.status !== 'archived' && (
              <>
                <button
                  type="button"
                  className="btn text-xs"
                  disabled={busy || needsRefresh || !!pendingStart}
                  onClick={() => setEditing(true)}
                >
                  Edit routine
                </button>
                <button
                  type="button"
                  className="text-xs underline"
                  disabled={busy || needsRefresh || !!pendingStart}
                  onClick={() => void command('archive')}
                >
                  Archive routine
                </button>
              </>
            )}
            <button
              type="button"
              className="text-xs text-fd-blue underline"
              disabled={busy}
              onClick={() => void command('refresh')}
            >
              Refresh routine
            </button>
          </div>
        </div>
      ) : (
        catalog && (
          <>
            <p className="text-xs text-fd-mute">
              Use an approved sequence, review each step, and attach its saved evidence. Existing runs retain the
              version they started with.
            </p>
            {catalog.routines.map(routine => (
              <button
                key={routine.id}
                type="button"
                className="block w-full text-left border border-slate-700 p-3"
                onClick={() => {
                  setSelected(routine);
                  setTemplate(undefined);
                  setNeedsRefresh(false);
                }}
              >
                <span className="block text-sm text-fd-blue">{routine.title}</span>
                <span className="text-xs text-fd-mute">
                  {routine.status} · {routine.steps.length} steps · Version {routine.version}
                </span>
              </button>
            ))}
            {catalog.truncated && <p className="text-xs text-fd-amber">Showing the first 100 routines.</p>}
            {catalog.can_manage && (
              <details>
                <summary className="text-xs text-fd-blue cursor-pointer">Prepare a routine draft</summary>
                <div className="mt-2 space-y-2">
                  {catalog.templates.map(item => (
                    <button
                      key={item.title}
                      type="button"
                      className="block btn text-xs"
                      onClick={() => {
                        setTemplate(item);
                        setSelected(null);
                        setEditing(true);
                      }}
                    >
                      Use {item.title} template
                    </button>
                  ))}
                  <button
                    type="button"
                    className="btn text-xs"
                    onClick={() => {
                      setTemplate(undefined);
                      setSelected(null);
                      setEditing(true);
                    }}
                  >
                    Write a new routine
                  </button>
                </div>
              </details>
            )}
            <h4 className="text-xs font-semibold text-fd-ink">Your saved runs</h4>
            {runs.map(run => (
              <button
                key={run.id}
                type="button"
                className="block w-full text-left border border-slate-700 p-3"
                disabled={loading}
                onClick={() => void openRun(run.id)}
              >
                <span className="block text-sm text-fd-blue">{run.title}</span>
                <span className="text-xs text-fd-mute">
                  {run.status} · {run.current_step} of {run.steps.length} steps finished
                </span>
              </button>
            ))}
            {cursor && (
              <button type="button" className="btn text-xs" disabled={loading} onClick={() => void moreRuns()}>
                Older routine runs
              </button>
            )}
          </>
        )
      )}
    </section>
  );
}

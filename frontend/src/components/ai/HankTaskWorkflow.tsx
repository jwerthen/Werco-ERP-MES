import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { isAxiosError } from 'axios';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type {
  HankCapabilities,
  HankTask,
  HankTaskCreate,
  HankActionKind,
  HankBasicActionKind,
  HankTaskReference,
} from '../../types/hankTasks';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { formatCentralDateTime } from '../../utils/centralTime';
import { getHankSessionScope, subscribeHankSession } from './hankSession';

const ACTIONS: Record<HankActionKind, { title: string; description: string; execute: string }> = {
  repeat_job: {
    title: 'Repeat a job',
    description: 'Prepare a new draft work order from an existing job. Review the copied plan before creating it.',
    execute: 'Create draft work order',
  },
  draft_purchase_order: {
    title: 'Draft a purchase order',
    description: 'Prepare a draft PO with one part. Review quantities and pricing before creating it.',
    execute: 'Create draft purchase order',
  },
  attach_document: {
    title: 'Attach a PDF to a job',
    description: 'Link an existing PDF in Documents to a work order after reviewing both records.',
    execute: 'Attach PDF to work order',
  },
  receive_delivery: {
    title: 'Receive a delivery',
    description: 'Review receipts before posting delivered material.',
    execute: 'Record delivery',
  },
  report_production: {
    title: 'Report production',
    description: 'Review quantities and any operation hold before saving.',
    execute: 'Record production report',
  },
  draft_shipment: {
    title: 'Draft a shipment',
    description: 'Review the shipment draft before creating it.',
    execute: 'Create draft shipment',
  },
};
const basicAction = (kind: HankActionKind): kind is HankBasicActionKind =>
  ['repeat_job', 'draft_purchase_order', 'attach_document'].includes(kind);

const formSchema = z
  .object({
    kind: z.enum(['repeat_job', 'draft_purchase_order', 'attach_document']),
    source_work_order_id: z.string(),
    work_order_id: z.string(),
    vendor_id: z.string(),
    part_id: z.string(),
    document_id: z.string(),
    quantity: z.string().trim(),
    unit_price: z.string().trim(),
    due_date: z.string(),
    required_date: z.string(),
  })
  .superRefine((value, ctx) => {
    const issue = (field: keyof typeof value, message: string) =>
      ctx.addIssue({ code: 'custom', path: [field], message });
    const identifier = (field: 'source_work_order_id' | 'work_order_id' | 'vendor_id' | 'part_id' | 'document_id') => {
      if (!/^[1-9]\d*$/.test(value[field]) || !Number.isSafeInteger(Number(value[field]))) {
        issue(field, 'Select a record from the available choices.');
      }
    };
    const date = (field: 'due_date' | 'required_date') => {
      const raw = value[field];
      if (!raw) return;
      const parsed = new Date(`${raw}T00:00:00Z`);
      if (
        !/^\d{4}-\d{2}-\d{2}$/.test(raw) ||
        !Number.isFinite(parsed.getTime()) ||
        parsed.toISOString().slice(0, 10) !== raw
      ) {
        issue(field, 'Enter a valid date.');
      }
    };
    if (value.kind === 'attach_document') {
      identifier('document_id');
      identifier('work_order_id');
    } else {
      if (!value.quantity || !Number.isFinite(Number(value.quantity)) || Number(value.quantity) <= 0) {
        issue('quantity', 'Enter a quantity greater than zero.');
      }
      if (value.kind === 'repeat_job') {
        identifier('source_work_order_id');
        date('due_date');
      } else {
        identifier('vendor_id');
        identifier('part_id');
        if (!value.unit_price || !Number.isFinite(Number(value.unit_price)) || Number(value.unit_price) < 0) {
          issue('unit_price', 'Enter a unit price of zero or more.');
        }
        date('required_date');
      }
    }
  });

type TaskForm = z.infer<typeof formSchema>;
const DEFAULT_FIELDS: TaskForm = {
  kind: 'repeat_job',
  source_work_order_id: '',
  work_order_id: '',
  vendor_id: '',
  part_id: '',
  document_id: '',
  quantity: '1',
  unit_price: '',
  due_date: '',
  required_date: '',
};

interface DocumentChoice {
  id: number;
  document_number: string;
  title: string;
  revision: string;
  status: string;
  file_name?: string | null;
  mime_type?: string | null;
}

function DocumentPicker({
  value,
  onChange,
  disabled,
  error,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  error?: string;
}) {
  const [query, setQuery] = useState('');
  const [rows, setRows] = useState<DocumentChoice[]>([]);
  const [selected, setSelected] = useState<DocumentChoice | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [limited, setLimited] = useState(false);
  useEffect(() => {
    let active = true;
    const scope = getHankSessionScope();
    setLoading(true);
    setFailed(false);
    const timer = window.setTimeout(() => {
      api
        .getDocuments({ search: query.trim() || undefined, limit: 25 })
        .then((documents: DocumentChoice[]) => {
          if (!active || scope !== getHankSessionScope()) return;
          setLimited(documents.length >= 25);
          setRows(
            documents
              .slice(0, 25)
              .filter(document => document.mime_type === 'application/pdf' || /\.pdf$/i.test(document.file_name || ''))
          );
        })
        .catch(() => {
          if (active) setFailed(true);
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, 250);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [query, attempt]);
  const options = selected && !rows.some(row => row.id === selected.id) ? [selected, ...rows] : rows;
  return (
    <div className="space-y-3">
      <FormField label="Find a PDF" help="Search document number or title. Results are limited to 25 recent matches.">
        {field => (
          <input
            {...field}
            value={query}
            maxLength={100}
            onChange={event => setQuery(event.target.value)}
            disabled={disabled}
            className="input w-full"
            placeholder="Drawing number or title"
          />
        )}
      </FormField>
      <FormField label="PDF to attach" required error={error}>
        {field => (
          <select
            {...field}
            value={value}
            disabled={disabled || loading || failed}
            onChange={event => {
              onChange(event.target.value);
              setSelected(options.find(row => String(row.id) === event.target.value) || null);
            }}
            className="input w-full"
          >
            <option value="">{loading ? 'Loading documents…' : 'Select a PDF…'}</option>
            {options.map(document => (
              <option key={document.id} value={document.id}>
                {document.document_number} · Rev {document.revision} · {document.title} ({document.status})
              </option>
            ))}
          </select>
        )}
      </FormField>
      {failed && (
        <p role="alert" className="text-xs text-fd-red">
          Documents could not be loaded.{' '}
          <button
            type="button"
            disabled={disabled}
            className="underline"
            onClick={() => setAttempt(value => value + 1)}
          >
            Retry document search
          </button>
        </p>
      )}
      {!loading && !failed && !rows.length && (
        <p className="text-xs text-fd-mute">No PDFs in these results. Try a more specific document number or title.</p>
      )}
      {limited && (
        <p className="text-xs text-fd-mute">More records may match. Narrow the search if your PDF is missing.</p>
      )}
    </div>
  );
}

function taskError(cause: unknown): string {
  const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
  return typeof detail === 'string'
    ? detail
    : 'The request was not confirmed. Check its status or retry the same request.';
}

function References({ references, onNavigate }: { references: HankTaskReference[]; onNavigate: () => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {references.map(reference => (
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
  );
}

export interface HankTaskWorkflowProps {
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
  initialTask?: HankTask;
  onTaskChanged?: (task: HankTask) => void;
  onStartAnother?: () => void;
}

export function HankTaskWorkflow({
  onNavigate,
  onBusyChange,
  initialTask,
  onTaskChanged,
  onStartAnother,
}: HankTaskWorkflowProps) {
  const [scope] = useState(getHankSessionScope);
  const [capabilities, setCapabilities] = useState<HankCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [capabilityError, setCapabilityError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [task, setTask] = useState<HankTask | null>(initialTask || null);
  const [pendingCreate, setPendingCreate] = useState<HankTaskCreate | null>(null);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<'create' | 'execute' | 'cancel' | 'refresh' | null>(null);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [stale, setStale] = useState(false);
  const alive = useRef(true);
  const inFlight = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const initialTaskIdRef = useRef(initialTask?.id);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const {
    control,
    register,
    watch,
    setValue,
    getValues,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<TaskForm>({
    resolver: zodResolver(formSchema),
    defaultValues: DEFAULT_FIELDS,
  });
  const kind = watch('kind');
  const currentSession = useCallback(() => alive.current && scope === getHankSessionScope(), [scope]);

  useEffect(() => {
    alive.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (scope !== getHankSessionScope()) controllerRef.current?.abort();
    });
    return () => {
      alive.current = false;
      controllerRef.current?.abort();
      busyCallback.current?.(false);
      unsubscribe();
    };
  }, [scope]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setCapabilityError('');
    api
      .getHankCapabilities(controller.signal)
      .then(result => {
        if (!active || !currentSession()) return;
        setCapabilities(result);
        const first = result.allowed_kinds.find(basicAction);
        if (!result.allowed_kinds.includes(getValues('kind')) && first) setValue('kind', first);
      })
      .catch(() => {
        if (active && currentSession()) setCapabilityError('Hank’s available tasks could not be loaded.');
      })
      .finally(() => {
        if (active && currentSession()) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, currentSession, getValues, setValue]);

  useEffect(() => {
    if (initialTask && !inFlight.current) {
      setTask(initialTask);
      setPendingCreate(null);
      setError('');
      setUncertain(false);
      if (initialTask.id !== initialTaskIdRef.current) setStale(false);
      initialTaskIdRef.current = initialTask.id;
    }
  }, [initialTask]);

  const acceptTask = (saved: HankTask) => {
    if (!currentSession() || saved.company_id !== capabilities?.company_id) return;
    setTask(saved);
    setPendingCreate(null);
    setUncertain(false);
    onTaskChanged?.(saved);
  };

  const run = async (action: 'create' | 'execute' | 'cancel' | 'refresh', proposal?: HankTaskCreate) => {
    if (inFlight.current || !currentSession() || !capabilities) return;
    if (action !== 'refresh' && !capabilities.can_write) return;
    if (action === 'create' && (!proposal || !capabilities.allowed_kinds.includes(proposal.kind))) return;
    if (action !== 'create' && (!task || task.company_id !== capabilities.company_id)) return;
    if (action === 'execute' && (uncertain || stale || task?.status !== 'awaiting_review')) return;
    if (action === 'cancel' && (uncertain || task?.status !== 'awaiting_review')) return;
    if (
      action === 'execute' &&
      task &&
      (task.kind === 'watch_work_order' || !capabilities.allowed_kinds.includes(task.kind))
    )
      return;
    inFlight.current = true;
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setOperation(action);
    busyCallback.current?.(true);
    setError('');
    try {
      let saved: HankTask;
      if (action === 'create' && proposal) {
        saved = await api.createHankTask(proposal, controller.signal);
      } else if (action === 'refresh' && task) {
        saved = await api.getHankTask(task.id, controller.signal);
      } else if (task) {
        const command = { expected_company_id: capabilities.company_id, expected_version: task.version };
        saved =
          action === 'execute'
            ? await api.executeHankTask(task.id, command, controller.signal)
            : await api.cancelHankTask(task.id, command, controller.signal);
      } else return;
      if (!controller.signal.aborted) acceptTask(saved);
    } catch (cause: unknown) {
      if (currentSession() && !controller.signal.aborted) {
        setError(taskError(cause));
        const status = isAxiosError(cause) ? cause.response?.status : undefined;
        const confirmedRefusal =
          status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429;
        if (action === 'create' && confirmedRefusal) setPendingCreate(null);
        if (action !== 'refresh') setUncertain(!confirmedRefusal);
        if (action === 'execute' && status === 409) setStale(true);
      }
    } finally {
      inFlight.current = false;
      if (controllerRef.current === controller) controllerRef.current = null;
      if (currentSession()) {
        setBusy(false);
        setOperation(null);
        busyCallback.current?.(false);
      }
    }
  };

  const propose = handleSubmit(values => {
    if (inFlight.current || !capabilities || !currentSession()) return;
    if (pendingCreate) {
      void run('create', pendingCreate);
      return;
    }
    const base = { expected_company_id: capabilities.company_id, request_key: crypto.randomUUID() };
    const proposal: HankTaskCreate =
      values.kind === 'repeat_job'
        ? {
            ...base,
            kind: values.kind,
            input: {
              source_work_order_id: Number(values.source_work_order_id),
              quantity_ordered: Number(values.quantity),
              due_date: values.due_date || null,
            },
          }
        : values.kind === 'draft_purchase_order'
          ? {
              ...base,
              kind: values.kind,
              input: {
                vendor_id: Number(values.vendor_id),
                required_date: values.required_date || null,
                lines: [
                  {
                    part_id: Number(values.part_id),
                    quantity_ordered: Number(values.quantity),
                    unit_price: Number(values.unit_price),
                    required_date: values.required_date || null,
                  },
                ],
              },
            }
          : {
              ...base,
              kind: values.kind,
              input: { document_id: Number(values.document_id), work_order_id: Number(values.work_order_id) },
            };
    setPendingCreate(proposal);
    void run('create', proposal);
  });

  const startNew = () => {
    if (inFlight.current || uncertain) return;
    if (onStartAnother) {
      onStartAnother();
      return;
    }
    setTask(null);
    setPendingCreate(null);
    setStale(false);
    setError('');
    reset({ ...DEFAULT_FIELDS, kind: capabilities?.allowed_kinds.find(basicAction) || 'repeat_job' });
  };

  if (loading)
    return (
      <p role="status" className="text-sm text-fd-mute">
        Checking available tasks…
      </p>
    );
  if (capabilityError || !capabilities)
    return (
      <div role="alert" className="space-y-2 text-sm text-fd-red">
        <p>{capabilityError || 'Available tasks could not be loaded.'}</p>
        <button type="button" className="btn text-xs" onClick={() => setAttempt(value => value + 1)}>
          Retry loading tasks
        </button>
      </div>
    );
  if (!currentSession())
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to continue.
      </p>
    );
  if (task && task.company_id !== capabilities.company_id)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        This task belongs to a different company. Reopen it from your current workspace.
      </p>
    );

  const canAct =
    capabilities.can_write &&
    (!task || (task.kind !== 'watch_work_order' && capabilities.allowed_kinds.includes(task.kind)));
  const formDisabled = busy || !!pendingCreate;
  return (
    <section aria-label="Tasks with Hank" className="space-y-4" aria-busy={busy}>
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {uncertain && (
        <p className="text-xs text-fd-amber">
          The last request may have reached the server.{' '}
          {task
            ? 'Refresh its status before taking another action.'
            : 'Retry saving this same proposal to recover its status.'}
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
          {task.status === 'completed' && task.result ? (
            <div role="status" className="space-y-3 p-3 border border-emerald-700 rounded-[3px]">
              <p className="text-sm text-fd-body">{task.result.summary}</p>
              {task.result.warnings.map((warning, index) => (
                <p key={index} className="text-xs text-fd-amber">
                  {warning}
                </p>
              ))}
              <References references={task.result.references} onNavigate={onNavigate} />
            </div>
          ) : (
            <div className="space-y-3 p-3 border border-slate-700 rounded-[3px]">
              <h4 className="text-xs font-semibold text-fd-ink">Review the proposed change</h4>
              <p className="text-sm text-fd-body">{task.preview.summary}</p>
              <ul className="list-disc pl-4 space-y-1 text-xs text-fd-body">
                {task.preview.changes.map((change, index) => (
                  <li key={index}>{change}</li>
                ))}
              </ul>
              {task.preview.warnings.map((warning, index) => (
                <p key={index} className="text-xs text-fd-amber">
                  {warning}
                </p>
              ))}
              <References references={task.preview.references} onNavigate={onNavigate} />
            </div>
          )}
          {task.error_message && (
            <p role="alert" className="text-sm text-fd-red">
              {task.error_message}
            </p>
          )}
          {stale && task.status === 'awaiting_review' && (
            <p className="text-xs text-fd-amber">
              This preview is out of date. Cancel it and prepare a new proposal from the current records.
            </p>
          )}
          {!canAct && task.status === 'awaiting_review' && (
            <p className="text-xs text-fd-mute">
              Your current permissions allow you to review this task. They do not allow you to execute it.
            </p>
          )}
          {task.status === 'awaiting_review' && (
            <p className="text-xs text-fd-mute">
              The proposal is saved. The business record changes only after you confirm below.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {task.status === 'awaiting_review' && canAct && (
              <>
                <LoadingButton
                  type="button"
                  size="sm"
                  loading={operation === 'execute'}
                  loadingText="Completing task…"
                  disabled={busy || uncertain || stale}
                  onClick={() => void run('execute')}
                >
                  {task.kind !== 'watch_work_order' && ACTIONS[task.kind].execute}
                </LoadingButton>
                <LoadingButton
                  type="button"
                  size="sm"
                  variant="secondary"
                  loading={operation === 'cancel'}
                  loadingText="Cancelling…"
                  disabled={busy || uncertain}
                  onClick={() => void run('cancel')}
                >
                  Cancel proposal
                </LoadingButton>
              </>
            )}
            <LoadingButton
              type="button"
              size="sm"
              variant="ghost"
              loading={operation === 'refresh'}
              loadingText="Checking…"
              disabled={busy}
              onClick={() => void run('refresh')}
            >
              Refresh task status
            </LoadingButton>
            {['completed', 'cancelled', 'needs_attention'].includes(task.status) && capabilities.can_write && (
              <button type="button" disabled={busy || uncertain} className="btn text-xs" onClick={startNew}>
                Start another task
              </button>
            )}
          </div>
        </>
      ) : !capabilities.can_write || !capabilities.allowed_kinds.some(basicAction) ? (
        <p className="text-sm text-fd-mute">
          No basic task templates are available here for your current role. Check Work for other available actions.
        </p>
      ) : (
        <form onSubmit={propose} className="space-y-4" aria-label="Prepare a task with Hank">
          <div>
            <h3 className="text-sm font-semibold text-fd-ink">Let’s get a task ready</h3>
            <p className="mt-1 text-xs text-fd-mute">
              Choose the records and details. Hank will save a proposal for you to review before changing anything.
            </p>
          </div>
          <fieldset disabled={formDisabled} className="space-y-3 min-w-0">
            <FormField label="Task" required>
              {field => (
                <select {...field} {...register('kind')} className="input w-full">
                  {capabilities.allowed_kinds.filter(basicAction).map(action => (
                    <option key={action} value={action}>
                      {ACTIONS[action].title}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <p className="text-xs text-fd-mute">{ACTIONS[kind].description}</p>
            {kind === 'repeat_job' && (
              <FormField label="Job to repeat" required error={errors.source_work_order_id?.message}>
                {field => (
                  <Controller
                    name="source_work_order_id"
                    control={control}
                    render={({ field: input }) => (
                      <EntityPicker
                        {...field}
                        kind="workOrder"
                        value={input.value}
                        onChange={input.onChange}
                        disabled={formDisabled}
                      />
                    )}
                  />
                )}
              </FormField>
            )}
            {kind === 'draft_purchase_order' && (
              <>
                <FormField label="Vendor" required error={errors.vendor_id?.message}>
                  {field => (
                    <Controller
                      name="vendor_id"
                      control={control}
                      render={({ field: input }) => (
                        <EntityPicker
                          {...field}
                          kind="vendor"
                          value={input.value}
                          onChange={input.onChange}
                          disabled={formDisabled}
                        />
                      )}
                    />
                  )}
                </FormField>
                <FormField label="Part to order" required error={errors.part_id?.message}>
                  {field => (
                    <Controller
                      name="part_id"
                      control={control}
                      render={({ field: input }) => (
                        <EntityPicker
                          {...field}
                          kind="part"
                          value={input.value}
                          onChange={input.onChange}
                          disabled={formDisabled}
                        />
                      )}
                    />
                  )}
                </FormField>
              </>
            )}
            {kind !== 'attach_document' && (
              <FormField label="Quantity" required error={errors.quantity?.message}>
                {field => (
                  <input
                    {...field}
                    {...register('quantity')}
                    type="number"
                    min="0"
                    step="any"
                    className="input w-full"
                  />
                )}
              </FormField>
            )}
            {kind === 'repeat_job' && (
              <FormField label="Due date (optional)" error={errors.due_date?.message}>
                {field => <input {...field} {...register('due_date')} type="date" className="input w-full" />}
              </FormField>
            )}
            {kind === 'draft_purchase_order' && (
              <>
                <FormField label="Unit price" required error={errors.unit_price?.message}>
                  {field => (
                    <input
                      {...field}
                      {...register('unit_price')}
                      type="number"
                      min="0"
                      step="any"
                      className="input w-full"
                    />
                  )}
                </FormField>
                <FormField label="Required date (optional)" error={errors.required_date?.message}>
                  {field => <input {...field} {...register('required_date')} type="date" className="input w-full" />}
                </FormField>
              </>
            )}
            {kind === 'attach_document' && (
              <>
                <Controller
                  name="document_id"
                  control={control}
                  render={({ field }) => (
                    <DocumentPicker
                      value={field.value}
                      onChange={field.onChange}
                      disabled={formDisabled}
                      error={errors.document_id?.message}
                    />
                  )}
                />
                <FormField label="Target work order" required error={errors.work_order_id?.message}>
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
                          disabled={formDisabled}
                        />
                      )}
                    />
                  )}
                </FormField>
              </>
            )}
          </fieldset>
          {pendingCreate ? (
            <LoadingButton
              type="button"
              size="sm"
              loading={busy}
              loadingText="Saving proposal…"
              onClick={() => void run('create', pendingCreate)}
            >
              Retry saving proposal
            </LoadingButton>
          ) : (
            <LoadingButton type="submit" size="sm" loading={busy} loadingText="Preparing preview…">
              Prepare task for review
            </LoadingButton>
          )}
        </form>
      )}
    </section>
  );
}

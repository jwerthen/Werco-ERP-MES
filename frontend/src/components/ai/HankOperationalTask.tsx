import React, { useEffect, useRef, useState } from 'react';
import { Controller, useFieldArray, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import type { HankCapabilities, HankOperationalActionKind, HankTask, HankTaskCreate } from '../../types/hankTasks';
import type { HankIntakeReceivingDraft } from '../../types/hankIntake';
import type { WorkOrderBlockerCategory, WorkOrderBlockerSeverity } from '../../types/aiForward';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankJobScan } from './HankJobScan';
import { HankTaskWorkflow } from './HankTaskWorkflow';
import { useHankSessionGuard } from './useHankSessionGuard';
import { formatOperationLabel } from '../../utils/operationLabel';

const TITLES = {
  receive_delivery: 'Receive a delivery',
  report_production: 'Report production and holds',
  draft_shipment: 'Draft a shipment',
};
const CATEGORIES: WorkOrderBlockerCategory[] = [
  'material_missing',
  'machine_down',
  'tooling_missing',
  'quality_hold',
  'labor_unavailable',
  'engineering_question',
  'previous_operation',
  'other',
];
const schema = z.object({
  purchase_order_id: z.string(),
  work_order_id: z.string(),
  operation_id: z.string(),
  lines: z.array(
    z.object({
      po_line_id: z.number(),
      quantity: z.string(),
      inspection: z.enum(['', 'yes', 'no']),
      lot: z.string().max(100),
      heat: z.string().max(100),
      cert: z.string().max(100),
      packing_slip: z.string().max(100),
    })
  ),
  complete: z.string(),
  scrap: z.string(),
  scrap_reason: z.string().max(255),
  notes: z.string().max(2000),
  hold: z.boolean(),
  hold_category: z.enum([
    'material_missing',
    'machine_down',
    'tooling_missing',
    'quality_hold',
    'labor_unavailable',
    'engineering_question',
    'previous_operation',
    'other',
  ]),
  hold_severity: z.enum(['low', 'medium', 'high', 'critical']),
  hold_note: z.string().max(2000),
  quantity: z.string(),
  packages: z.string(),
  ship_to_name: z.string().max(200),
  ship_to_address: z.string().max(500),
  ship_to_city: z.string().max(100),
  ship_to_state: z.string().max(50),
  ship_to_zip: z.string().max(20),
  carrier: z.string().max(100),
});
type FormValues = z.infer<typeof schema>;
interface ReceivingPO {
  po_id: number;
  po_number: string;
  lines: Array<{
    line_id: number;
    line_number: number;
    part_number: string;
    part_name: string;
    quantity_remaining: number;
    is_closed?: boolean;
  }>;
}
interface JobChoice {
  id: number;
  work_order_number: string;
  operations: Array<{
    id: number;
    sequence: number;
    operation_number?: string;
    name: string;
    status: string;
    quantity_complete: number;
  }>;
}

export function HankOperationalTask({
  kind,
  workOrderId,
  purchaseOrderId,
  receivingDraft,
  onRefreshReceiving,
  operationId,
  onNavigate,
  onBusyChange,
}: {
  kind: HankOperationalActionKind;
  workOrderId?: number;
  purchaseOrderId?: number;
  receivingDraft?: HankIntakeReceivingDraft;
  onRefreshReceiving?: () => void;
  operationId?: number;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [capabilities, setCapabilities] = useState<HankCapabilities | null>(null);
  const [capError, setCapError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [po, setPO] = useState<ReceivingPO | null>(null);
  const [job, setJob] = useState<JobChoice | null>(null);
  const [choicesLoading, setChoicesLoading] = useState(false);
  const [choiceError, setChoiceError] = useState('');
  const [choiceAttempt, setChoiceAttempt] = useState(0);
  const [task, setTask] = useState<HankTask | null>(null);
  const [pending, setPending] = useState<HankTaskCreate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [duplicateAcknowledged, setDuplicateAcknowledged] = useState(false);
  const { current, controller, release, changed } = useHankSessionGuard();
  const inFlight = useRef(false);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      purchase_order_id: String(purchaseOrderId || ''),
      work_order_id: String(workOrderId || ''),
      operation_id: String(operationId || ''),
      lines: [],
      complete: '0',
      scrap: '0',
      scrap_reason: '',
      notes: '',
      hold: false,
      hold_category: 'other',
      hold_severity: 'medium',
      hold_note: '',
      quantity: '',
      packages: '1',
      ship_to_name: '',
      ship_to_address: '',
      ship_to_city: '',
      ship_to_state: '',
      ship_to_zip: '',
      carrier: '',
    },
  });
  const {
    register,
    control,
    watch,
    setValue,
    setError: fieldError,
    handleSubmit,
    formState: { errors },
  } = form;
  const { fields, replace } = useFieldArray({ control, name: 'lines' });
  const selectedPO = watch('purchase_order_id');
  const selectedJob = watch('work_order_id');
  const holding = watch('hold');
  useEffect(() => () => busyCallback.current?.(false), []);
  useEffect(() => {
    const request = controller();
    setCapError(false);
    api
      .getHankCapabilities(request.signal)
      .then(value => {
        if (current() && !request.signal.aborted) setCapabilities(value);
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setCapError(true);
      })
      .finally(() => release(request));
    return () => request.abort();
  }, [attempt, current, controller, release]);
  useEffect(() => {
    if (kind !== 'receive_delivery' || !selectedPO) return;
    const request = controller();
    setPO(null);
    replace([]);
    setChoicesLoading(true);
    setChoiceError('');
    api
      .getPOForReceiving(Number(selectedPO), request.signal)
      .then((value: ReceivingPO) => {
        if (!current() || request.signal.aborted) return;
        setPO(value);
        replace(
          value.lines
            .filter(line => !line.is_closed)
            .map(line => {
              const matches =
                receivingDraft?.purchase_order_id === Number(selectedPO)
                  ? receivingDraft.lines.filter(item => item.po_line_id === line.line_id)
                  : [];
              // Multiple source rows may have different lots/heats. Never collapse them silently.
              const source = matches.length === 1 ? matches[0] : undefined;
              return {
                po_line_id: line.line_id,
                quantity: source?.quantity_received != null ? String(source.quantity_received) : '',
                inspection: '' as const,
                lot: source?.lot_number || '',
                heat: source?.heat_number || '',
                cert: '',
                packing_slip: source ? receivingDraft?.packing_slip_number || '' : '',
              };
            })
        );
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setChoiceError('Receipt lines could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setChoicesLoading(false);
      });
    return () => request.abort();
  }, [kind, selectedPO, choiceAttempt, current, controller, release, replace, receivingDraft]);
  useEffect(() => {
    if (kind !== 'report_production' || !selectedJob) return;
    const request = controller();
    setJob(null);
    setChoicesLoading(true);
    setChoiceError('');
    api
      .getWorkOrder(Number(selectedJob), request.signal)
      .then((value: JobChoice) => {
        if (current() && !request.signal.aborted) {
          setJob(value);
          const selected = form.getValues('operation_id');
          if (!value.operations.some(operation => String(operation.id) === selected)) setValue('operation_id', '');
        }
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setChoiceError('Operations could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setChoicesLoading(false);
      });
    return () => request.abort();
  }, [kind, selectedJob, choiceAttempt, current, controller, release, setValue, form]);
  useEffect(() => {
    busyCallback.current?.(busy || !!pending);
  }, [busy, pending]);
  const create = async (body: HankTaskCreate) => {
    if (!current() || inFlight.current || !capabilities?.allowed_kinds.includes(kind)) return;
    const request = controller();
    inFlight.current = true;
    setBusy(true);
    busyCallback.current?.(true);
    setError('');
    try {
      const result = await api.createHankTask(body, request.signal);
      if (current() && !request.signal.aborted && result.company_id === capabilities.company_id) {
        setTask(result);
        setPending(null);
      }
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        const status = isAxiosError(cause) ? cause.response?.status : undefined;
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : null;
        setError(
          typeof detail === 'string' ? detail : 'The proposal was not confirmed. Retry the same proposal to recover it.'
        );
        if (status && status >= 400 && status < 500 && status !== 408 && status !== 429) setPending(null);
      }
    } finally {
      release(request);
      inFlight.current = false;
      if (current()) {
        setBusy(false);
      }
    }
  };
  const prepare = handleSubmit(values => {
    if (!capabilities || !current() || inFlight.current || pending) return;
    const base = { expected_company_id: capabilities.company_id, request_key: crypto.randomUUID() };
    let body: HankTaskCreate;
    if (kind === 'receive_delivery') {
      if (receivingDraft?.requires_duplicate_acknowledgement && !duplicateAcknowledged) {
        setError('Review the prior receipt and confirm this is an additional delivery before proceeding.');
        return;
      }
      const selected = values.lines.filter(line => Number(line.quantity) > 0);
      if (!selectedPO || !selected.length) {
        setError('Enter the delivered quantity on at least one line.');
        return;
      }
      const invalid = values.lines.findIndex(
        line =>
          line.quantity &&
          (!Number.isFinite(Number(line.quantity)) ||
            Number(line.quantity) < 0 ||
            (Number(line.quantity) > 0 && !line.inspection))
      );
      if (invalid >= 0) {
        fieldError(`lines.${invalid}.inspection`, {
          message: 'Enter a valid quantity and choose whether inspection is required.',
        });
        return;
      }
      body = {
        ...base,
        kind,
        input: {
          purchase_order_id: Number(selectedPO),
          ...(receivingDraft
            ? {
                source_intake_file_id: receivingDraft.file_id,
                source_intake_version: receivingDraft.file_version,
                acknowledge_duplicate_source: duplicateAcknowledged,
              }
            : {}),
          lines: selected.map(line => ({
            po_line_id: line.po_line_id,
            quantity_received: Number(line.quantity),
            requires_inspection: line.inspection === 'yes',
            lot_number: line.lot || undefined,
            heat_number: line.heat || undefined,
            cert_number: line.cert || undefined,
            packing_slip_number: line.packing_slip || undefined,
            over_receive_approved: false,
          })),
        },
      };
    } else if (kind === 'report_production') {
      if (!values.operation_id) {
        fieldError('operation_id', { message: 'Choose the operation being reported.' });
        return;
      }
      const complete = Number(values.complete);
      const scrap = Number(values.scrap);
      if (![complete, scrap].every(value => Number.isFinite(value) && value >= 0) || (!complete && !scrap)) {
        setError('Enter a positive production or scrap quantity. For a hold without production, use Shop Floor.');
        return;
      }
      if (scrap && !values.scrap_reason.trim()) {
        fieldError('scrap_reason', { message: 'Describe why the material was scrapped.' });
        return;
      }
      if (values.hold && !values.hold_note.trim()) {
        fieldError('hold_note', { message: 'Describe the reason for the hold.' });
        return;
      }
      body = {
        ...base,
        kind,
        input: {
          operation_id: Number(values.operation_id),
          quantity_complete_delta: complete,
          quantity_scrapped_delta: scrap,
          scrap_reason: values.scrap_reason || undefined,
          notes: values.notes || undefined,
          open_ncr: false,
          ...(values.hold
            ? {
                hold: {
                  category: values.hold_category as WorkOrderBlockerCategory,
                  severity: values.hold_severity as WorkOrderBlockerSeverity,
                  note: values.hold_note,
                },
              }
            : {}),
        },
      };
    } else {
      if (
        !values.work_order_id ||
        !(Number(values.quantity) > 0) ||
        !Number.isFinite(Number(values.quantity)) ||
        !Number.isSafeInteger(Number(values.packages)) ||
        Number(values.packages) < 1
      ) {
        setError('Select a work order, enter a positive shipment quantity, and enter a whole package count.');
        return;
      }
      body = {
        ...base,
        kind,
        input: {
          work_order_id: Number(values.work_order_id),
          quantity_shipped: Number(values.quantity),
          num_packages: Number(values.packages),
          ship_to_name: values.ship_to_name || undefined,
          ship_to_address: values.ship_to_address || undefined,
          ship_to_city: values.ship_to_city || undefined,
          ship_to_state: values.ship_to_state || undefined,
          ship_to_zip: values.ship_to_zip || undefined,
          carrier: values.carrier || undefined,
          packing_notes: values.notes || undefined,
        },
      };
    }
    setPending(body);
    void create(body);
  });
  if (changed)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to continue.
      </p>
    );
  if (capError)
    return (
      <p role="alert" className="text-sm text-fd-red">
        Available actions could not be loaded.{' '}
        <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
          Retry access
        </button>
      </p>
    );
  if (!capabilities)
    return (
      <p role="status" className="text-sm text-fd-mute">
        Checking available actions…
      </p>
    );
  if (task)
    return (
      <HankTaskWorkflow
        initialTask={task}
        onNavigate={onNavigate}
        onBusyChange={onBusyChange}
        onStartAnother={() => {
          setTask(null);
          setPending(null);
          if (receivingDraft) onRefreshReceiving?.();
        }}
      />
    );
  if (!capabilities.can_write || !capabilities.allowed_kinds.includes(kind))
    return <p className="text-sm text-fd-mute">This action is unavailable for your current role and company.</p>;
  const disabled = busy || !!pending;
  return (
    <section aria-label={TITLES[kind]} className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink">{TITLES[kind]}</h3>
      <p className="text-xs text-fd-mute">
        Enter what happened. Review the saved proposal before confirming any ERP changes.
      </p>
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {receivingDraft?.requires_duplicate_acknowledgement && (
        <label className="flex items-start gap-2 text-xs text-fd-amber">
          <input
            type="checkbox"
            checked={duplicateAcknowledged}
            disabled={disabled}
            onChange={event => setDuplicateAcknowledged(event.target.checked)}
          />
          I reviewed the prior receipt for this PDF and confirm these quantities are an additional delivery.
        </label>
      )}
      <form onSubmit={prepare} className="space-y-4">
        <fieldset disabled={disabled} className="space-y-3">
          <FormField label={kind === 'receive_delivery' ? 'Purchase order' : 'Work order'} required>
            {field => (
              <Controller
                name={kind === 'receive_delivery' ? 'purchase_order_id' : 'work_order_id'}
                control={control}
                render={({ field: input }) =>
                  kind === 'receive_delivery' ? (
                    <HankPurchaseOrderPicker
                      {...field}
                      value={input.value}
                      onChange={input.onChange}
                      disabled={disabled || !!receivingDraft}
                    />
                  ) : (
                    <EntityPicker
                      {...field}
                      kind="workOrder"
                      value={input.value}
                      onChange={input.onChange}
                      disabled={disabled}
                    />
                  )
                }
              />
            )}
          </FormField>
          {choicesLoading && (
            <p role="status" className="text-xs text-fd-mute">
              Loading source details…
            </p>
          )}
          {choiceError && (
            <p role="alert" className="text-xs text-fd-red">
              {choiceError}{' '}
              <button type="button" className="underline" onClick={() => setChoiceAttempt(value => value + 1)}>
                Retry source details
              </button>
            </p>
          )}
          {kind === 'receive_delivery' &&
            fields.map((line, index) => {
              const source = po?.lines.find(item => item.line_id === line.po_line_id);
              return (
                <div key={line.id} className="space-y-2 border border-slate-700 p-3">
                  <p className="text-xs font-semibold text-fd-ink">
                    Line {source?.line_number} · {source?.part_number} · {source?.part_name}
                  </p>
                  <p className="text-xs text-fd-mute">
                    Remaining: {source?.quantity_remaining}. Leave quantity blank for lines not delivered.
                  </p>
                  <FormField label={`Delivered quantity, line ${source?.line_number}`}>
                    {field => (
                      <input
                        {...field}
                        {...register(`lines.${index}.quantity`)}
                        type="number"
                        min="0"
                        step="any"
                        className="input w-full"
                      />
                    )}
                  </FormField>
                  <FormField
                    label={`Inspection required, line ${source?.line_number}`}
                    error={errors.lines?.[index]?.inspection?.message}
                  >
                    {field => (
                      <select {...field} {...register(`lines.${index}.inspection`)} className="input w-full">
                        <option value="">Choose for this delivery…</option>
                        <option value="yes">Yes — hold for inspection</option>
                        <option value="no">No — post to inventory</option>
                      </select>
                    )}
                  </FormField>
                  {(['lot', 'heat', 'cert', 'packing_slip'] as const).map(name => (
                    <FormField key={name} label={`${name.replace(/_/g, ' ')} number, line ${source?.line_number}`}>
                      {field => <input {...field} {...register(`lines.${index}.${name}`)} className="input w-full" />}
                    </FormField>
                  ))}
                </div>
              );
            })}
          {kind === 'report_production' && (
            <>
              <FormField label="Operation" required error={errors.operation_id?.message}>
                {field => (
                  <select
                    {...field}
                    {...register('operation_id')}
                    className="input w-full"
                    disabled={disabled || choicesLoading}
                  >
                    <option value="">Select operation…</option>
                    {job?.operations.map(operation => (
                      <option key={operation.id} value={operation.id}>
                        {formatOperationLabel(operation.operation_number, operation.sequence)} · {operation.name} ·{' '}
                        {operation.status} · complete {operation.quantity_complete}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Good quantity to add">
                {field => (
                  <input
                    {...field}
                    {...register('complete')}
                    type="number"
                    min="0"
                    step="any"
                    className="input w-full"
                  />
                )}
              </FormField>
              <FormField label="Scrap quantity to add">
                {field => (
                  <input {...field} {...register('scrap')} type="number" min="0" step="any" className="input w-full" />
                )}
              </FormField>
              <FormField label="Scrap reason" error={errors.scrap_reason?.message}>
                {field => <input {...field} {...register('scrap_reason')} maxLength={255} className="input w-full" />}
              </FormField>
              <FormField label="Put this operation on hold">
                {field => <input {...field} {...register('hold')} type="checkbox" className="checkbox checkbox-sm" />}
              </FormField>
              {holding && (
                <>
                  <p className="text-xs text-fd-amber">
                    A hold stops every active employee time entry on this operation. Review this effect before
                    confirming.
                  </p>
                  <FormField label="Hold reason">
                    {field => (
                      <select {...field} {...register('hold_category')} className="input w-full">
                        {CATEGORIES.map(category => (
                          <option key={category} value={category}>
                            {category.replace(/_/g, ' ')}
                          </option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <FormField label="Hold severity">
                    {field => (
                      <select {...field} {...register('hold_severity')} className="input w-full">
                        {['low', 'medium', 'high', 'critical'].map(severity => (
                          <option key={severity}>{severity}</option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <FormField label="Hold details" required error={errors.hold_note?.message}>
                    {field => (
                      <textarea {...field} {...register('hold_note')} className="input w-full h-auto" rows={3} />
                    )}
                  </FormField>
                </>
              )}
            </>
          )}
          {kind === 'draft_shipment' && (
            <>
              <p className="text-xs text-fd-mute">
                Creates a draft. Shipping authorization, certificate issuance, carrier purchase, and dispatch remain
                separate.
              </p>
              <FormField label="Shipment quantity" required>
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
              <FormField label="Packages" required>
                {field => (
                  <input {...field} {...register('packages')} type="number" min="1" step="1" className="input w-full" />
                )}
              </FormField>
              {(
                ['ship_to_name', 'ship_to_address', 'ship_to_city', 'ship_to_state', 'ship_to_zip', 'carrier'] as const
              ).map(name => (
                <FormField key={name} label={name.replace(/_/g, ' ')}>
                  {field => <input {...field} {...register(name)} className="input w-full" />}
                </FormField>
              ))}
            </>
          )}
          {kind !== 'receive_delivery' && (
            <FormField label="Notes">
              {field => <textarea {...field} {...register('notes')} className="input w-full h-auto" rows={3} />}
            </FormField>
          )}
        </fieldset>
        {pending ? (
          <LoadingButton
            type="button"
            size="sm"
            loading={busy}
            loadingText="Saving proposal…"
            onClick={() => void create(pending)}
          >
            Retry same proposal
          </LoadingButton>
        ) : (
          <LoadingButton
            type="submit"
            size="sm"
            disabled={choicesLoading || !!choiceError}
            loading={busy}
            loadingText="Preparing review…"
          >
            Prepare for review
          </LoadingButton>
        )}
      </form>
      {kind !== 'receive_delivery' && (
        <details>
          <summary className="text-xs text-fd-blue">Select by scan</summary>
          <HankJobScan
            disabled={disabled}
            onSelect={value => {
              setValue('work_order_id', String(value.workOrderId));
              setValue('operation_id', String(value.operationId || ''));
            }}
          />
        </details>
      )}
    </section>
  );
}

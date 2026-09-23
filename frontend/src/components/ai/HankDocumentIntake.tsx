import React, { useEffect, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { isAxiosError } from 'axios';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankCapabilities } from '../../types/hankTasks';
import type {
  HankIntakeBatch,
  HankIntakeFile,
  HankIntakeFieldName,
  HankIntakePlan,
  HankIntakeMatch,
} from '../../types/hankIntake';
import { usePermissions } from '../../hooks/usePermissions';
import { canPublishDocuments } from '../../utils/recordWriteAccess';
import { formatCentralDateTime } from '../../utils/centralTime';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankSourceFile } from './HankSourceFile';
import { hankEvidenceLabel } from './hankDocumentEvidence';
import { HankDocumentReceiving } from './HankDocumentReceiving';
import { HankDocumentPurchaseOrder } from './HankDocumentPurchaseOrder';
import { isHankReadOnlySession } from './hankSession';
import { useHankSessionGuard } from './useHankSessionGuard';

const planSchema = z
  .object({
    title: z.string().trim().min(1, 'Enter a document title.').max(255),
    document_type: z.string().min(1),
    revision: z.string().trim().min(1).max(20),
    description: z.string().max(3000),
    filing_mode: z.enum(['draft', 'release_receipt_certificate']),
    part_id: z.string(),
    work_order_id: z.string(),
    vendor_id: z.string(),
    purchase_order_id: z.string(),
    receipt_id: z.string(),
    reviewed_fields: z.array(z.object({ name: z.string(), value: z.string().max(300) })).max(30),
    acknowledge_duplicate: z.boolean(),
  })
  .superRefine((values, ctx) => {
    if (values.filing_mode === 'release_receipt_certificate' && !values.receipt_id)
      ctx.addIssue({ code: 'custom', path: ['receipt_id'], message: 'Choose the receipt for this certificate.' });
  });
type PlanValues = z.infer<typeof planSchema>;
function planDefaults(file: HankIntakeFile, workOrderId?: number): PlanValues {
  const plan = file.plan?.input;
  return {
    title: plan?.title || file.filename.replace(/\.(pdf|docx|xlsx|xls)$/i, '').slice(0, 255),
    document_type:
      plan?.document_type ||
      (file.analysis?.classification === 'drawing'
        ? 'drawing'
        : file.analysis?.classification === 'material_certificate'
          ? 'material_cert'
          : 'other'),
    revision: plan?.revision || 'A',
    description: plan?.description || '',
    filing_mode: plan?.filing_mode || 'draft',
    part_id: String(plan?.part_id || ''),
    work_order_id: String(plan?.work_order_id || workOrderId || ''),
    vendor_id: String(plan?.vendor_id || ''),
    purchase_order_id: String(plan?.purchase_order_id || ''),
    receipt_id: String(plan?.receipt_id || ''),
    reviewed_fields: (plan?.reviewed_fields || file.analysis?.fields || []).map(field => ({
      name: field.name,
      value: field.value || '',
    })),
    acknowledge_duplicate: plan?.acknowledge_duplicate || false,
  };
}
function IntakeReferences({ items, onNavigate }: { items: HankIntakeMatch[]; onNavigate: () => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map(item => (
        <Link
          key={`${item.kind}:${item.id}`}
          to={item.href}
          onClick={onNavigate}
          className="text-xs text-fd-blue underline"
        >
          {item.label}
        </Link>
      ))}
    </div>
  );
}

function IntakeReview({
  file,
  canWrite,
  workOrderId,
  onChanged,
  onBusyChange,
  onNavigate,
}: {
  file: HankIntakeFile;
  canWrite: boolean;
  workOrderId?: number;
  onChanged: (file: HankIntakeFile) => void;
  onBusyChange?: (busy: boolean) => void;
  onNavigate: () => void;
}) {
  const [types, setTypes] = useState<Array<{ value: string; label: string }>>([]);
  const [typeError, setTypeError] = useState(false);
  const [typeAttempt, setTypeAttempt] = useState(0);
  const [receipts, setReceipts] = useState<Array<{ id: number; label: string }>>([]);
  const [receiptError, setReceiptError] = useState(false);
  const [receiptAttempt, setReceiptAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const { current, controller, release, changed } = useHankSessionGuard();
  const flight = useRef(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const {
    register,
    control,
    handleSubmit,
    setValue,
    watch,
    reset,
    formState: { errors, isDirty },
  } = useForm<PlanValues>({ resolver: zodResolver(planSchema), defaultValues: planDefaults(file, workOrderId) });
  const purchaseOrder = watch('purchase_order_id');
  const filingMode = watch('filing_mode');
  const previousPO = useRef(purchaseOrder);
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    const defaults = planDefaults(file, workOrderId);
    previousPO.current = defaults.purchase_order_id;
    reset(defaults);
  }, [file, workOrderId, reset]);
  useEffect(() => {
    let active = true;
    setTypeError(false);
    api
      .getDocumentTypes()
      .then((result: Array<{ value: string; label: string }>) => {
        if (active && current()) setTypes(result);
      })
      .catch(() => {
        if (active && current()) setTypeError(true);
      });
    return () => {
      active = false;
    };
  }, [typeAttempt, current]);
  useEffect(() => {
    if (previousPO.current !== purchaseOrder) {
      previousPO.current = purchaseOrder;
      setValue('receipt_id', '', { shouldDirty: true });
    }
    setReceipts([]);
    if (!purchaseOrder) {
      setReceipts([]);
      return;
    }
    const request = controller();
    setReceiptError(false);
    api
      .getPOForReceiving(Number(purchaseOrder), request.signal)
      .then(
        (result: {
          lines: Array<{ receipts?: Array<{ receipt_id: number; receipt_number: string; quantity_received: number }> }>;
        }) => {
          if (current() && !request.signal.aborted)
            setReceipts(
              result.lines
                .flatMap(line => line.receipts || [])
                .map(receipt => ({
                  id: receipt.receipt_id,
                  label: `${receipt.receipt_number} · received ${receipt.quantity_received}`,
                }))
            );
        }
      )
      .catch(() => {
        if (current() && !request.signal.aborted) setReceiptError(true);
      })
      .finally(() => release(request));
    return () => request.abort();
  }, [purchaseOrder, receiptAttempt, current, controller, release, setValue]);
  const run = async (action: 'plan' | 'execute' | 'retry' | 'cancel' | 'refresh', plan?: HankIntakePlan) => {
    if (!current() || flight.current || (action !== 'refresh' && (!canWrite || needsRefresh))) return;
    const request = controller();
    flight.current = true;
    setBusy(true);
    callback.current?.(true);
    setError('');
    try {
      const command = { expected_company_id: file.company_id, expected_version: file.version };
      const result =
        action === 'refresh'
          ? await api.getHankIntakeFile(file.id, request.signal)
          : action === 'plan' && plan
            ? await api.planHankIntake(file.id, { ...command, plan }, request.signal)
            : action !== 'plan'
              ? await api.commandHankIntake(file.id, action, command, request.signal)
              : undefined;
      if (result && current() && !request.signal.aborted && result.company_id === file.company_id) {
        setNeedsRefresh(false);
        onChanged(result);
      }
    } catch (cause) {
      if (!current() || request.signal.aborted) return;
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(
        typeof detail === 'string' ? detail : 'The request was not confirmed. Refresh this file before continuing.'
      );
      const refused = status && status >= 400 && status < 500 && status !== 408 && status !== 429;
      if (action !== 'refresh' && (!refused || status === 409)) setNeedsRefresh(true);
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
        callback.current?.(false);
      }
    }
  };
  const savePlan = handleSubmit(values => {
    const optionalId = (value: string) => (value ? Number(value) : null);
    void run('plan', {
      ...values,
      part_id: optionalId(values.part_id),
      work_order_id: optionalId(values.work_order_id),
      vendor_id: optionalId(values.vendor_id),
      purchase_order_id: optionalId(values.purchase_order_id),
      receipt_id: optionalId(values.receipt_id),
      reviewed_fields: values.reviewed_fields.map(field => ({
        name: field.name as HankIntakeFieldName,
        value: field.value || null,
      })),
    });
  });
  if (changed)
    return (
      <p role="alert" className="text-xs text-fd-amber">
        Your session changed. Reopen this intake.
      </p>
    );
  const disabled = busy || needsRefresh || !canWrite;
  const editable = ['awaiting_review', 'planned'].includes(file.status);
  const receiptOptions = Array.from(
    new Map(
      [
        ...(!purchaseOrder
          ? file.analysis?.matches
              .filter(item => item.kind === 'receipt')
              .map(item => ({ id: item.id, label: item.label })) || []
          : []),
        ...receipts,
      ].map(item => [item.id, item])
    ).values()
  );
  const pages = [
    ...(file.analysis?.evidence || []),
    ...(file.analysis?.fields.flatMap(field => field.evidence) || []),
    ...(file.analysis?.lines.flatMap(line => line.evidence) || []),
  ].map(item => item.page);
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink break-words">{file.filename}</h3>
      <p className="text-xs text-fd-mute">
        {file.status.replace(/_/g, ' ')} · Updated {formatCentralDateTime(file.updated_at)}
        {file.page_count && /\.pdf$/i.test(file.filename) ? ` · ${file.page_count} pages` : ''}
      </p>
      <HankSourceFile
        filename={file.filename}
        pages={pages}
        load={signal => api.getHankIntakeSource(file.id, signal)}
        loadPreview={signal => api.getHankIntakeSourcePreview(file.id, signal)}
      />
      {['queued', 'analyzing'].includes(file.status) && (
        <p role="status" className="text-xs text-fd-mute">
          {file.status === 'queued' ? 'Waiting for document analysis.' : 'Hank is analyzing this document.'} Refresh to
          check progress.
        </p>
      )}
      {file.error_message && (
        <p role="alert" className="text-sm text-fd-amber">
          {file.error_message}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {needsRefresh && (
        <p className="text-xs text-fd-amber">
          Refresh the saved file before another change. The previous request may have completed or its version may have
          changed.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <LoadingButton type="button" size="sm" variant="ghost" loading={busy} onClick={() => void run('refresh')}>
          Refresh file
        </LoadingButton>
        {canWrite && ['failed', 'queued', 'analyzing'].includes(file.status) && (
          <button type="button" className="btn text-xs" disabled={disabled} onClick={() => void run('retry')}>
            Retry failed or stale analysis
          </button>
        )}
        {canWrite && !['completed', 'cancelled'].includes(file.status) && (
          <button
            type="button"
            className="text-xs text-fd-mute underline"
            disabled={disabled}
            onClick={() => void run('cancel')}
          >
            Cancel intake file
          </button>
        )}
      </div>
      {file.result && (
        <div className="space-y-2 border border-slate-700 p-3">
          <h4 className="text-xs font-semibold text-fd-ink">Filing receipt</h4>
          <p className="text-sm text-fd-body">{file.result.summary}</p>
          <Link to={file.result.href} onClick={onNavigate} className="text-xs text-fd-blue underline">
            {file.result.document_number}
          </Link>
          {file.result.warnings.map((warning, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {warning}
            </p>
          ))}
          <IntakeReferences items={file.result.references} onNavigate={onNavigate} />
        </div>
      )}
      {file.analysis && (
        <div className="space-y-3">
          <p className="text-xs font-semibold text-fd-ink">
            Suggested {file.analysis.classification.replace(/_/g, ' ')} · {file.analysis.confidence} confidence
          </p>
          <p className="text-xs text-fd-body">{file.analysis.summary}</p>
          {file.analysis.warnings.map((warning, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {warning}
            </p>
          ))}
          {!!file.analysis.lines.length && (
            <details>
              <summary className="text-xs text-fd-blue cursor-pointer">
                Review {file.analysis.lines.length} extracted lines
              </summary>
              <p className="mt-2 text-xs text-fd-mute">
                These are source suggestions. Filing this document does not create purchase-order or receipt lines.
              </p>
              <div className="space-y-2 mt-2">
                {file.analysis.lines.map((line, index) => (
                  <div key={index} className="border border-slate-700 p-2 space-y-1 text-xs text-fd-body">
                    <p>
                      {line.description || line.part_number || `Line ${index + 1}`} · {line.confidence} confidence
                    </p>
                    <p>
                      Part {line.part_number || 'unknown'} · Quantity {line.quantity || 'unknown'} · Unit price{' '}
                      {line.unit_price || 'unknown'}
                    </p>
                    {line.evidence.map((evidence, item) => (
                      <p key={item} className="text-fd-mute">
                        {hankEvidenceLabel(file, evidence)}: {evidence.excerpt}
                      </p>
                    ))}
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
      {editable && (
        <form aria-label="Review document filing" onSubmit={savePlan} className="space-y-4">
          <fieldset disabled={disabled} className="space-y-3">
            <p className="text-xs text-fd-mute">
              Check extracted values against the source and choose the records to link. Nothing files until you confirm
              the saved plan.
            </p>
            <FormField label="Document title" required error={errors.title?.message}>
              {field => <input {...field} {...register('title')} className="input w-full" />}
            </FormField>
            <FormField label="Document type" required>
              {field => (
                <select
                  {...field}
                  {...register('document_type')}
                  value={watch('document_type')}
                  className="input w-full"
                >
                  {!types.length && (
                    <option value={watch('document_type')}>{watch('document_type').replace(/_/g, ' ')}</option>
                  )}
                  {types.map(type => (
                    <option key={type.value} value={type.value}>
                      {type.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {typeError && (
              <p className="text-xs text-fd-amber">
                Document types could not be loaded.{' '}
                <button type="button" className="underline" onClick={() => setTypeAttempt(value => value + 1)}>
                  Retry types
                </button>
              </p>
            )}
            <FormField label="Revision" required error={errors.revision?.message}>
              {field => <input {...field} {...register('revision')} className="input w-full" />}
            </FormField>
            <FormField label="Description">
              {field => <textarea {...field} {...register('description')} className="input w-full h-auto" rows={3} />}
            </FormField>
            {file.analysis?.fields.map((item, index) => (
              <div key={`${item.name}:${index}`} className="border border-slate-700 p-2 space-y-1">
                <FormField
                  label={`${item.name.replace(/_/g, ' ')} · ${item.confidence} confidence`}
                  error={errors.reviewed_fields?.[index]?.value?.message}
                >
                  {field => (
                    <input {...field} {...register(`reviewed_fields.${index}.value`)} className="input w-full" />
                  )}
                </FormField>
                {item.evidence.map((evidence, evidenceIndex) => (
                  <p key={evidenceIndex} className="text-[11px] text-fd-mute">
                    {hankEvidenceLabel(file, evidence)}: {evidence.excerpt}
                  </p>
                ))}
              </div>
            ))}
            {!!file.analysis?.matches.length && (
              <details>
                <summary className="text-xs text-fd-blue cursor-pointer">Suggested record matches</summary>
                {file.analysis.matches.map(match => (
                  <div key={`${match.kind}:${match.id}`} className="mt-2 space-y-1 border border-slate-700 p-2">
                    <p className="text-xs text-fd-body">{match.label}</p>
                    <p className="text-xs text-fd-mute">{match.reason}</p>
                    <button
                      type="button"
                      className="text-xs text-fd-blue underline"
                      onClick={() =>
                        setValue(
                          `${match.kind === 'purchase_order' ? 'purchase_order' : match.kind}_id` as
                            | 'part_id'
                            | 'work_order_id'
                            | 'vendor_id'
                            | 'purchase_order_id'
                            | 'receipt_id',
                          String(match.id),
                          { shouldDirty: true }
                        )
                      }
                    >
                      Use this {match.kind.replace(/_/g, ' ')}
                    </button>
                  </div>
                ))}
              </details>
            )}
            {(['part', 'workOrder', 'vendor'] as const).map(kind => {
              const name = kind === 'workOrder' ? 'work_order_id' : kind === 'part' ? 'part_id' : 'vendor_id';
              return (
                <FormField key={name} label={`${kind === 'workOrder' ? 'Work order' : kind} association (optional)`}>
                  {field => (
                    <Controller
                      name={name}
                      control={control}
                      render={({ field: input }) => (
                        <EntityPicker
                          {...field}
                          kind={kind}
                          value={input.value}
                          onChange={input.onChange}
                          disabled={disabled}
                          optional
                        />
                      )}
                    />
                  )}
                </FormField>
              );
            })}
            <FormField label="Purchase order reference (optional)">
              {field => (
                <Controller
                  name="purchase_order_id"
                  control={control}
                  render={({ field: input }) => (
                    <HankPurchaseOrderPicker
                      {...field}
                      value={input.value}
                      onChange={input.onChange}
                      disabled={disabled}
                    />
                  )}
                />
              )}
            </FormField>
            <FormField label="Receipt reference" error={errors.receipt_id?.message}>
              {field => (
                <select {...field} {...register('receipt_id')} value={watch('receipt_id')} className="input w-full">
                  <option value="">No receipt selected</option>
                  {receiptOptions.map(receipt => (
                    <option key={receipt.id} value={receipt.id}>
                      {receipt.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {receiptError && (
              <p className="text-xs text-fd-amber">
                Receipt choices could not be loaded.{' '}
                <button type="button" className="underline" onClick={() => setReceiptAttempt(value => value + 1)}>
                  Retry receipts
                </button>
              </p>
            )}
            <FormField label="Filing action">
              {field => (
                <select {...field} {...register('filing_mode')} className="input w-full">
                  <option value="draft">Save a draft document</option>
                  <option value="release_receipt_certificate">Release and link a receipt certificate</option>
                </select>
              )}
            </FormField>
            <p className="text-xs text-fd-amber">
              {filingMode === 'draft'
                ? 'The filed document remains a draft for your document-control process.'
                : 'Confirmation releases this certificate and binds it to the selected receipt. Verify the receipt and certificate contents.'}
            </p>
            {!!(
              file.analysis?.has_duplicates ||
              (file.analysis?.duplicate_file_ids.length || 0) + (file.analysis?.duplicate_document_ids.length || 0)
            ) && (
              <FormField
                label="I reviewed the possible duplicates"
                help="Hank found matching file content. Review it before making another document."
              >
                {field => (
                  <input
                    {...field}
                    {...register('acknowledge_duplicate')}
                    type="checkbox"
                    className="checkbox checkbox-sm"
                  />
                )}
              </FormField>
            )}
          </fieldset>
          <LoadingButton type="submit" size="sm" disabled={disabled} loading={busy}>
            Save filing plan for review
          </LoadingButton>
        </form>
      )}
      {file.plan && file.status === 'planned' && (
        <div className="space-y-3 border border-slate-700 p-3">
          <h4 className="text-xs font-semibold text-fd-ink">Review the saved filing plan</h4>
          {file.plan.changes.map((change, index) => (
            <p key={index} className="text-xs text-fd-body">
              {change}
            </p>
          ))}
          {file.plan.warnings.map((warning, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {warning}
            </p>
          ))}
          <IntakeReferences items={file.plan.references} onNavigate={onNavigate} />
          {isDirty && <p className="text-xs text-fd-amber">Save your changed plan before filing.</p>}
          <LoadingButton
            type="button"
            size="sm"
            loading={busy}
            disabled={disabled || isDirty}
            onClick={() => void run('execute')}
          >
            {file.plan.input.filing_mode === 'draft' ? 'File draft document' : 'Release and link certificate'}
          </LoadingButton>
        </div>
      )}
    </div>
  );
}

export function HankDocumentIntake({
  initialId,
  workOrderId,
  onNavigate,
  onBusyChange,
  onUseInChat,
}: {
  initialId?: number;
  workOrderId?: number;
  onUseInChat?: (file: HankIntakeFile) => void;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const { role, isSuperuser } = usePermissions();
  const canWrite = canPublishDocuments({ role, is_superuser: isSuperuser }) && !isHankReadOnlySession();
  const [cap, setCap] = useState<HankCapabilities | null>(null);
  const [batches, setBatches] = useState<HankIntakeBatch[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState(initialId);
  const [receiving, setReceiving] = useState(false);
  const [creatingPO, setCreatingPO] = useState(false);
  const [file, setFile] = useState<HankIntakeFile | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [pending, setPending] = useState<{ files: File[]; key: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [childBusy, setChildBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const { current, controller, release, changed } = useHankSessionGuard();
  const flight = useRef(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    callback.current?.(busy || childBusy || !!pending);
  }, [busy, childBusy, pending]);
  useEffect(() => {
    if (initialId && !flight.current) setSelectedId(initialId);
  }, [initialId]);
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setError('');
    Promise.all([
      api.getHankCapabilities(request.signal),
      selectedId
        ? api.getHankIntakeFile(selectedId, request.signal)
        : api.getHankIntakes({ limit: 20 }, request.signal),
    ])
      .then(([capabilities, result]) => {
        if (!current() || request.signal.aborted) return;
        setCap(capabilities);
        if ('batches' in result) {
          setBatches(result.batches);
          setCursor(result.has_more ? result.next_before_id : null);
          setFile(null);
        } else setFile(result);
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setError('Document intake could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [selectedId, attempt, current, controller, release]);
  const upload = async () => {
    if (!current() || !cap || !canWrite || flight.current || (!files.length && !pending)) return;
    const submission = pending || { files: [...files], key: crypto.randomUUID() };
    setPending(submission);
    flight.current = true;
    const request = controller();
    setBusy(true);
    callback.current?.(true);
    setError('');
    try {
      const body = new FormData();
      body.append('expected_company_id', String(cap.company_id));
      body.append('request_key', submission.key);
      submission.files.forEach(item => body.append('files', item));
      const batch = await api.createHankIntake(body, request.signal);
      if (current() && !request.signal.aborted && batch.company_id === cap.company_id) {
        setBatches(previous => [batch, ...previous.filter(item => item.id !== batch.id)]);
        setPending(null);
        setFiles([]);
      }
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        const status = isAxiosError(cause) ? cause.response?.status : undefined;
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(
          typeof detail === 'string' ? detail : 'Upload was not confirmed. Retry these same files to recover the batch.'
        );
        if (status && status >= 400 && status < 500 && status !== 408 && status !== 429) setPending(null);
      }
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
      }
    }
  };
  const loadMore = async () => {
    if (!cursor || loading || !current()) return;
    const request = controller();
    setLoading(true);
    try {
      const result = await api.getHankIntakes({ limit: 20, before_id: cursor }, request.signal);
      if (current() && !request.signal.aborted) {
        setBatches(previous => [...previous, ...result.batches]);
        setCursor(result.has_more ? result.next_before_id : null);
      }
    } catch {
      if (current()) setError('Older intake batches could not be loaded.');
    } finally {
      release(request);
      if (current()) setLoading(false);
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see document intake.
      </p>
    );
  return (
    <section aria-label="Document intake" className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-fd-ink flex-1">Document intake</h3>
        {selectedId && (
          <button
            type="button"
            className="text-xs text-fd-blue underline"
            disabled={busy || childBusy || !!pending}
            onClick={() => {
              setSelectedId(undefined);
              setFile(null);
              setReceiving(false);
              setCreatingPO(false);
            }}
          >
            All batches
          </button>
        )}
        <button
          type="button"
          className="text-xs text-fd-blue underline"
          disabled={busy || childBusy || loading}
          onClick={() => setAttempt(value => value + 1)}
        >
          Refresh intake
        </button>
      </div>
      {loading && (
        <p role="status" className="text-xs text-fd-mute">
          Loading document intake…
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}{' '}
          <button type="button" className="underline" disabled={busy} onClick={() => setAttempt(value => value + 1)}>
            Retry intake loading
          </button>
        </p>
      )}
      {!selectedId && (
        <>
          {canWrite && (
            <div className="space-y-3">
              <p className="text-xs text-fd-mute">
                Upload up to 5 PDF, Word (.docx), or Excel (.xlsx, .xls) files, then use their extracted information in
                chat, create purchase orders, prepare material receipts, or file the documents. Review the source before
                confirming any changes.
              </p>
              <FormField
                label="Documents to review"
                help="10 MB per file, 25 MB per batch, and up to 25 pages per PDF."
              >
                {field => (
                  <input
                    {...field}
                    type="file"
                    multiple
                    accept="application/pdf,.pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.docx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsx,application/vnd.ms-excel,.xls"
                    className="input w-full"
                    disabled={busy || !!pending}
                    onChange={event => {
                      const items = Array.from(event.target.files || []);
                      if (
                        items.length > 5 ||
                        items.some(
                          item =>
                            item.size > 10 * 1024 * 1024 || !item.size || !/\.(pdf|docx|xlsx|xls)$/i.test(item.name)
                        ) ||
                        items.reduce((sum, item) => sum + item.size, 0) > 25 * 1024 * 1024
                      ) {
                        setError(
                          'Choose up to 5 non-empty PDF, DOCX, XLSX, or XLS files, at most 10 MB each and 25 MB total.'
                        );
                        setFiles([]);
                        return;
                      }
                      setFiles(items);
                      setError('');
                    }}
                  />
                )}
              </FormField>
              <LoadingButton
                type="button"
                size="sm"
                loading={busy}
                disabled={!cap || (!files.length && !pending)}
                onClick={() => void upload()}
              >
                {pending ? 'Retry same document batch' : 'Upload for review'}
              </LoadingButton>
            </div>
          )}
          {batches.map(batch => (
            <article key={batch.id} className="space-y-2 border border-slate-700 p-3">
              <p className="text-xs text-fd-mute">Uploaded {formatCentralDateTime(batch.created_at)}</p>
              {batch.files.map(item => (
                <button
                  key={item.id}
                  type="button"
                  disabled={busy || !!pending}
                  onClick={() => {
                    setFile(null);
                    setSelectedId(item.id);
                  }}
                  className="block w-full text-left text-sm text-fd-blue break-words"
                >
                  {item.filename} <span className="text-xs text-fd-mute">· {item.status.replace(/_/g, ' ')}</span>
                </button>
              ))}
            </article>
          ))}
          {!loading && !error && !batches.length && (
            <p className="text-xs text-fd-mute">No saved intake batches yet.</p>
          )}
          {cursor && (
            <button type="button" className="btn text-xs" disabled={loading || busy} onClick={() => void loadMore()}>
              Older document batches
            </button>
          )}
        </>
      )}
      {selectedId &&
        file &&
        !receiving &&
        !creatingPO &&
        file.analysis &&
        ['awaiting_review', 'planned', 'completed'].includes(file.status) && (
          <div className="flex flex-wrap gap-2">
            {onUseInChat && (
              <button
                type="button"
                className="btn text-xs"
                disabled={busy || childBusy}
                onClick={() => onUseInChat(file)}
              >
                Use in chat
              </button>
            )}
            {canWrite &&
              cap?.allowed_kinds.includes('draft_purchase_order') &&
              file.analysis.classification === 'purchase_order' && (
                <button
                  type="button"
                  className="btn text-xs"
                  disabled={busy || childBusy}
                  onClick={() => setCreatingPO(true)}
                >
                  Create purchase order
                </button>
              )}
            {canWrite && cap?.allowed_kinds.includes('receive_delivery') && (
              <button
                type="button"
                className="btn text-xs"
                disabled={busy || childBusy}
                onClick={() => setReceiving(true)}
              >
                Receive materials
              </button>
            )}
          </div>
        )}
      {selectedId && file && receiving && (
        <>
          <button
            type="button"
            className="text-xs text-fd-blue underline"
            disabled={childBusy}
            onClick={() => setReceiving(false)}
          >
            Back to document review
          </button>
          <HankDocumentReceiving file={file} onNavigate={onNavigate} onBusyChange={setChildBusy} />
        </>
      )}
      {selectedId && file && creatingPO && (
        <>
          <button
            type="button"
            className="text-xs text-fd-blue underline"
            disabled={childBusy}
            onClick={() => setCreatingPO(false)}
          >
            Back to document review
          </button>
          <HankDocumentPurchaseOrder key={file.id} file={file} onNavigate={onNavigate} onBusyChange={setChildBusy} />
        </>
      )}
      {selectedId && file && !receiving && !creatingPO && (
        <IntakeReview
          key={file.id}
          file={file}
          canWrite={canWrite}
          workOrderId={workOrderId}
          onChanged={setFile}
          onNavigate={onNavigate}
          onBusyChange={value => {
            setChildBusy(value);
          }}
        />
      )}
    </section>
  );
}

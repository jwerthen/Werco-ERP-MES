import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PrinterIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button, ConfirmDialog, ErrorState, FormField } from '../ui';
import usePermissions from '../../hooks/usePermissions';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { formatCentralDate, formatCentralDateTime } from '../../utils/centralTime';
import CarFormFields, {
  BLANK_CAR_FORM_VALUES,
  carFormPayload,
  carFormValuesFromRecord,
  formatMoney,
  type CarFormValues,
} from '../quality/CarFormFields';
import CarPdfPreviewModal from '../quality/CarPdfPreviewModal';
import { carInitiationReasonLabel } from '../../types/quality';

const ncrFields = ['containment_action', 'root_cause'];
const carFields = [
  'containment_action',
  'root_cause_analysis',
  'root_cause',
  'corrective_action',
  'preventive_action',
  'verification_method',
  'verification_results',
  'effectiveness_check',
];
const statuses = {
  ncr: ['open', 'under_review', 'pending_disposition', 'closed'],
  car: ['open', 'root_cause_analysis', 'corrective_action', 'verification', 'closed'],
};
const label = (key: string) => key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
const snapshotOf = (form: Record<string, any>, carForm: CarFormValues) => JSON.stringify({ form, carForm });

export default function QualityRecordDetail({
  kind,
  id,
  onClose,
  onSaved,
}: {
  kind: 'ncr' | 'car';
  id: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [record, setRecord] = useState<any>(null);
  const [form, setForm] = useState<Record<string, any>>({});
  const [carForm, setCarForm] = useState<CarFormValues>(BLANK_CAR_FORM_VALUES);
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [showPdf, setShowPdf] = useState(false);
  const [confirmApproval, setConfirmApproval] = useState(false);
  const [approving, setApproving] = useState(false);
  const { can } = usePermissions();
  const editable = can('quality:approve') && record?.status !== 'void';
  // A closed record is final: the server refuses (409) every change to it except a
  // status-only reopen, so only the status control stays live until it is reopened.
  const closedRecord = record?.status === 'closed';
  const dirty = !!record && snapshotOf(form, carForm) !== original;
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);
  // The Quality Manager approval mirrors POST /quality/car/{id}/quality-approval
  // (Admin / Manager / Quality — exactly the quality:approve holders). Recorded once,
  // never on a VOID CAR; the server is still the gate (and refuses an API token).
  const canRecordApproval =
    kind === 'car' && can('quality:approve') && !!record && record.status !== 'void' && !record.quality_approved_at;

  const applyRecord = (data: any) => {
    setRecord(data);
    const fields = kind === 'ncr' ? ncrFields : carFields;
    const next: any = { status: data.status, ...Object.fromEntries(fields.map(key => [key, data[key] || ''])) };
    if (kind === 'ncr') {
      next.disposition = data.disposition || 'pending';
      next.quantity_rejected = data.quantity_rejected || 0;
    } else {
      next.priority = data.priority;
      next.due_date = data.due_date || '';
      next.verification_due = data.verification_due || '';
    }
    const nextCar = kind === 'car' ? carFormValuesFromRecord(data) : BLANK_CAR_FORM_VALUES;
    setForm(next);
    setCarForm(nextCar);
    setOriginal(snapshotOf(next, nextCar));
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    setRecord(null);
    setError('');
    setNotice('');
    (kind === 'ncr' ? api.getNCR(id) : api.getCAR(id))
      .then(data => {
        if (active) applyRecord(data);
      })
      .catch(err => {
        if (active) setError(err.response?.data?.detail || 'Unable to load quality record');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [kind, id, attempt]);
  const close = () => {
    if (!saving && !approving && confirmDiscard()) onClose();
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (saving || !editable) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const payload = {
        ...form,
        ...(kind === 'car' ? carFormPayload(carForm) : {}),
        version: record.version ?? 0,
        expected_updated_at: record.updated_at,
        ...(kind === 'car' ? { due_date: form.due_date || null, verification_due: form.verification_due || null } : {}),
      };
      const result = kind === 'ncr' ? await api.updateNCR(id, payload) : await api.updateCAR(id, payload);
      setRecord(result);
      setOriginal(snapshotOf(form, carForm));
      markSaved();
      onSaved();
      onClose();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail.map((item: any) => item.msg).join('; ')
            : 'Unable to save this record'
      );
    } finally {
      setSaving(false);
    }
  };
  // Non-optimistic: the server may refuse (already approved / VOID 409, API token 403),
  // so nothing changes on screen until it answers — then the saved record is shown.
  const recordApproval = async () => {
    if (approving || !canRecordApproval || dirty) return;
    setApproving(true);
    setError('');
    setNotice('');
    try {
      const result = await api.approveCARQuality(id);
      applyRecord(result);
      setNotice(
        `Quality Manager approval recorded${result.quality_approved_by_name ? ` for ${result.quality_approved_by_name}` : ''}.`
      );
      onSaved();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Unable to record the quality approval');
    } finally {
      setApproving(false);
      setConfirmApproval(false);
    }
  };
  const carSummary: Array<[string, React.ReactNode]> =
    kind === 'car' && record
      ? [
          ['Initiated by', record.initiated_by_name || '—'],
          ['Assigned to', record.assigned_to_name || '—'],
          ['Created', formatCentralDateTime(record.created_at)],
          ['Date submitted', record.submitted_date ? formatCentralDate(record.submitted_date) : '—'],
          [
            'Reason for initiation',
            record.initiation_reason
              ? `${carInitiationReasonLabel(record.initiation_reason)}${
                  record.initiation_reason === 'other' && record.initiation_reason_other
                    ? `: ${record.initiation_reason_other}`
                    : ''
                }`
              : '—',
          ],
          [
            'Part',
            record.part_number
              ? `${record.part_number}${
                  record.part_current_revision ? ` (current Rev ${record.part_current_revision})` : ''
                }${record.part_name ? ` — ${record.part_name}` : ''}`
              : '—',
          ],
          ['Customer', record.customer_name || '—'],
          [
            'Vendor',
            record.vendor_name
              ? `${record.vendor_name}${record.vendor_po_number ? ` · PO ${record.vendor_po_number}` : ''}`
              : '—',
          ],
          ['Cost total', formatMoney(record.cost_total)],
          ...(record.closed_date
            ? ([['Closed', formatCentralDate(record.closed_date)]] as Array<[string, React.ReactNode]>)
            : []),
        ]
      : [];
  return (
    <Modal
      open
      ariaLabel="Quality record details"
      onClose={close}
      size={kind === 'car' ? '3xl' : 'xl'}
      closeOnBackdrop={false}
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <h2 className="text-lg font-semibold">{record?.[`${kind}_number`] || `${kind.toUpperCase()} detail`}</h2>
        {kind === 'car' && record && (
          <Button variant="secondary" size="sm" onClick={() => setShowPdf(true)} className="flex items-center gap-1.5">
            <PrinterIcon className="h-4 w-4" aria-hidden="true" />
            Print CAR
          </Button>
        )}
      </div>
      {loading ? (
        <p role="status">Loading full record…</p>
      ) : !record ? (
        <ErrorState message={error} onRetry={() => setAttempt(n => n + 1)} />
      ) : (
        <form onSubmit={save} className="space-y-4">
          <h3 className="font-semibold text-lg">{record.title}</h3>
          <p className="whitespace-pre-wrap break-words">{record.description || record.problem_description}</p>
          {kind === 'ncr' ? (
            <dl className="grid grid-cols-2 gap-3 text-sm">
              {[
                'source',
                'part',
                'lot_number',
                'serial_number',
                'quantity_affected',
                'specification',
                'actual_value',
                'required_value',
                'supplier_name',
                'created_at',
                'closed_date',
              ]
                .filter(key => record[key] != null)
                .map(key => (
                  <div key={key}>
                    <dt className="text-slate-400">{label(key)}</dt>
                    <dd className="whitespace-pre-wrap break-words">
                      {key === 'part' ? `${record.part.part_number} — ${record.part.name}` : String(record[key])}
                    </dd>
                  </div>
                ))}
            </dl>
          ) : (
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
              {carSummary.map(([term, value]) => (
                <div key={term}>
                  <dt className="text-slate-400">{term}</dt>
                  <dd className="whitespace-pre-wrap break-words">{value}</dd>
                </div>
              ))}
            </dl>
          )}
          {kind === 'car' && (
            <div
              className="flex flex-wrap items-center justify-between gap-3 border border-fd-line bg-slate-900/40 px-3 py-2 text-sm"
              data-testid="car-quality-approval"
            >
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Quality Manager approval
                </p>
                {record.quality_approved_at ? (
                  <p className="text-slate-100">
                    Approved by {record.quality_approved_by_name || `user #${record.quality_approved_by}`} on{' '}
                    {formatCentralDateTime(record.quality_approved_at)}
                  </p>
                ) : (
                  <p className="text-slate-300">Not yet approved. It does not gate closure.</p>
                )}
              </div>
              {canRecordApproval && (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={dirty || approving || saving}
                  title={dirty ? 'Save or discard your edits before recording the approval' : undefined}
                  onClick={() => setConfirmApproval(true)}
                >
                  Record quality approval
                </Button>
              )}
            </div>
          )}
          {notice && (
            <p role="status" className="text-sm text-fd-green">
              {notice}
            </p>
          )}
          {record.work_order_id && (
            <Link className="text-werco-primary underline" to={`/work-orders/${record.work_order_id}`}>
              Open linked work order
            </Link>
          )}
          {record.car_id && (
            <Link className="text-werco-primary underline" to={`/quality?tab=car&car=${record.car_id}`}>
              Open corrective action
            </Link>
          )}
          <fieldset disabled={!editable || saving || approving} className="space-y-4">
            <FormField label="Status">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={form.status || ''}
                  onChange={e => setForm(f => ({ ...f, status: e.target.value }))}
                >
                  {(record.status === 'void' ? ['void'] : statuses[kind]).map(status => (
                    <option key={status} value={status}>
                      {label(status)}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {closedRecord && editable && (
              <p className="text-sm text-slate-400">
                This record is closed and read-only. To change it, set the status back to an open state and save, then
                edit it.
              </p>
            )}
            <fieldset disabled={closedRecord} className="space-y-4">
              {kind === 'ncr' && (
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Disposition">
                    {field => (
                      <select
                        {...field}
                        className="input"
                        value={form.disposition}
                        onChange={e => setForm(f => ({ ...f, disposition: e.target.value }))}
                      >
                        {['pending', 'use_as_is', 'rework', 'repair', 'scrap', 'return_to_vendor'].map(value => (
                          <option key={value} value={value}>
                            {label(value)}
                          </option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <FormField label="Quantity rejected">
                    {field => (
                      <input
                        {...field}
                        type="number"
                        className="input"
                        min="0"
                        max={record.quantity_affected}
                        step="any"
                        value={form.quantity_rejected}
                        onChange={e => setForm(f => ({ ...f, quantity_rejected: Number(e.target.value) }))}
                      />
                    )}
                  </FormField>
                </div>
              )}
              {kind === 'car' && (
                <div className="grid grid-cols-3 gap-3">
                  <FormField label="Priority (1 highest)">
                    {field => (
                      <input
                        {...field}
                        className="input"
                        type="number"
                        min="1"
                        max="10"
                        value={form.priority}
                        onChange={e => setForm(f => ({ ...f, priority: Number(e.target.value) }))}
                      />
                    )}
                  </FormField>
                  {['due_date', 'verification_due'].map(key => (
                    <FormField key={key} label={label(key)}>
                      {field => (
                        <input
                          {...field}
                          type="date"
                          className="input"
                          value={form[key]}
                          onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                        />
                      )}
                    </FormField>
                  ))}
                </div>
              )}
              {kind === 'car' && (
                <CarFormFields
                  values={carForm}
                  onChange={patch => setCarForm(current => ({ ...current, ...patch }))}
                  sections={['initiation', 'appliesTo']}
                  record={record}
                />
              )}
              {(kind === 'ncr' ? ncrFields : carFields).map(key => (
                <FormField key={key} label={label(key)}>
                  {field => (
                    <textarea
                      {...field}
                      className="input"
                      rows={3}
                      maxLength={2000}
                      value={form[key] || ''}
                      onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                    />
                  )}
                </FormField>
              ))}
              {kind === 'car' && (
                <CarFormFields
                  values={carForm}
                  onChange={patch => setCarForm(current => ({ ...current, ...patch }))}
                  sections={['cost', 'approvals']}
                  record={record}
                />
              )}
            </fieldset>
          </fieldset>
          {form.status === 'closed' && (
            <p className="text-sm text-slate-400">
              {kind === 'ncr'
                ? 'Closure requires a final disposition and root cause of at least 20 characters.'
                : 'Closure requires a root cause, corrective action, verification method and verification results.'}
            </p>
          )}
          {error && (
            <div role="alert" className="text-red-300">
              {error}
              <button
                type="button"
                className="underline ml-2"
                onClick={() => {
                  if (confirmDiscard()) setAttempt(n => n + 1);
                }}
              >
                Reload current record
              </button>
            </div>
          )}
          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={close} disabled={saving || approving}>
              Close
            </Button>
            {editable && (
              <Button type="submit" disabled={saving || approving}>
                {saving ? 'Saving…' : 'Save changes'}
              </Button>
            )}
          </div>
        </form>
      )}
      {showPdf && record && kind === 'car' && (
        <CarPdfPreviewModal carId={id} carNumber={record.car_number} onClose={() => setShowPdf(false)} />
      )}
      <ConfirmDialog
        open={confirmApproval}
        title="Record the Quality Manager approval?"
        message={`This records you as the Quality Manager approving ${record?.car_number || 'this CAR'}, dated now. It is recorded once and cannot be edited afterwards.`}
        variant="info"
        confirmLabel="Record approval"
        pending={approving}
        onConfirm={() => void recordApproval()}
        onCancel={() => {
          if (!approving) setConfirmApproval(false);
        }}
      />
    </Modal>
  );
}

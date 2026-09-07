import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button, ErrorState, FormField } from '../ui';
import usePermissions from '../../hooks/usePermissions';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';

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
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { can } = usePermissions();
  const editable = can('quality:approve') && record?.status !== 'void';
  const { confirmDiscard, markSaved } = useUnsavedChanges(!!record && JSON.stringify(form) !== original);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setRecord(null);
    setError('');
    (kind === 'ncr' ? api.getNCR(id) : api.getCAR(id))
      .then(data => {
        if (!active) return;
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
        setForm(next);
        setOriginal(JSON.stringify(next));
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
    if (!saving && confirmDiscard()) onClose();
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (saving || !editable) return;
    setSaving(true);
    setError('');
    try {
      const payload = {
        ...form,
        version: record.version ?? 0,
        expected_updated_at: record.updated_at,
        ...(kind === 'car' ? { due_date: form.due_date || null, verification_due: form.verification_due || null } : {}),
      };
      const result = kind === 'ncr' ? await api.updateNCR(id, payload) : await api.updateCAR(id, payload);
      setRecord(result);
      setOriginal(JSON.stringify(form));
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
  return (
    <Modal open ariaLabel="Quality record details" onClose={close} size="xl" closeOnBackdrop={false}>
      <h2 className="text-lg font-semibold mb-4">{record?.[`${kind}_number`] || `${kind.toUpperCase()} detail`}</h2>
      {loading ? (
        <p role="status">Loading full record…</p>
      ) : !record ? (
        <ErrorState message={error} onRetry={() => setAttempt(n => n + 1)} />
      ) : (
        <form onSubmit={save} className="space-y-4">
          <h3 className="font-semibold text-lg">{record.title}</h3>
          <p className="whitespace-pre-wrap break-words">{record.description || record.problem_description}</p>
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
          <fieldset disabled={!editable || saving} className="space-y-4">
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
            <Button variant="secondary" onClick={close} disabled={saving}>
              Close
            </Button>
            {editable && (
              <Button type="submit" disabled={saving}>
                {saving ? 'Saving…' : 'Save changes'}
              </Button>
            )}
          </div>
        </form>
      )}
    </Modal>
  );
}

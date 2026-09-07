import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button, ErrorState, FormField } from '../ui';
import usePermissions from '../../hooks/usePermissions';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import EntityPicker from './EntityPicker';

export default function PurchaseOrderDetail({
  id,
  onClose,
  onSaved,
  onLoaded,
}: {
  id: number;
  onClose: () => void;
  onSaved: () => void;
  onLoaded: (record: any) => void;
}) {
  const [record, setRecord] = useState<any>(null);
  const [form, setForm] = useState<any>(null);
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const { can } = usePermissions();
  const editable = record?.status === 'draft' && can('purchasing:create');
  const { confirmDiscard, markSaved } = useUnsavedChanges(!!form && JSON.stringify(form) !== original);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setRecord(null);
    setForm(null);
    setError('');
    api
      .getPurchaseOrder(id)
      .then(data => {
        if (!active) return;
        setRecord(data);
        onLoaded(data);
        const next = {
          required_date: data.required_date || '',
          expected_date: data.expected_date || '',
          ship_to: data.ship_to || '',
          shipping_method: data.shipping_method || '',
          notes: data.notes || '',
          lines: data.lines.map((line: any) => ({
            id: line.id,
            part_id: line.part_id,
            quantity_ordered: Number(line.quantity_ordered),
            unit_price: Number(line.unit_price),
            required_date: line.required_date || '',
            notes: line.notes || '',
          })),
        };
        setForm(next);
        setOriginal(JSON.stringify(next));
      })
      .catch(err => {
        if (active) setError(err.response?.data?.detail || 'Unable to load purchase order');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [id, attempt]);
  const close = () => {
    if (!saving && confirmDiscard()) onClose();
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (saving || !editable) return;
    if (
      !form.lines.length ||
      form.lines.some(
        (line: any) =>
          !line.part_id ||
          !Number.isFinite(Number(line.quantity_ordered)) ||
          Number(line.quantity_ordered) <= 0 ||
          !Number.isFinite(Number(line.unit_price)) ||
          Number(line.unit_price) < 0
      )
    ) {
      setError('Add at least one part with a positive quantity and a nonnegative price.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await api.updatePurchaseOrder(id, {
        ...form,
        version: record.version || 0,
        expected_updated_at: record.updated_at,
      });
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
            : 'Unable to save purchase order'
      );
    } finally {
      setSaving(false);
    }
  };
  const lineChange = (index: number, key: string, value: any) =>
    setForm((prev: any) => ({
      ...prev,
      lines: prev.lines.map((line: any, i: number) => (i === index ? { ...line, [key]: value } : line)),
    }));
  return (
    <Modal open ariaLabel="Purchase order details" onClose={close} size="2xl" closeOnBackdrop={false}>
      <h2 className="text-lg font-semibold mb-3">{record?.po_number || 'Purchase order'}</h2>
      {loading ? (
        <p role="status">Loading purchase order…</p>
      ) : !record ? (
        <ErrorState message={error} onRetry={() => setAttempt(n => n + 1)} />
      ) : (
        <form className="space-y-4" onSubmit={save}>
          <div className="flex justify-between gap-4">
            <div>
              <p className="font-medium">
                {record.vendor?.code} · {record.vendor?.name}
              </p>
              <p className="text-slate-400 capitalize">
                {record.status.replace(/_/g, ' ')} · {record.lines.length} lines
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => window.open(`/print/purchase-order/${id}?autoprint=1`, '_blank')}
            >
              Print purchase order
            </Button>
          </div>
          <fieldset disabled={!editable || saving} className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              {['required_date', 'expected_date', 'ship_to', 'shipping_method'].map(key => (
                <FormField key={key} label={key.replace(/_/g, ' ')}>
                  {field => (
                    <input
                      {...field}
                      className="input"
                      type={key.endsWith('_date') ? 'date' : 'text'}
                      value={form[key]}
                      onChange={e => setForm((prev: any) => ({ ...prev, [key]: e.target.value }))}
                    />
                  )}
                </FormField>
              ))}
            </div>
            <FormField label="Notes">
              {field => (
                <textarea
                  {...field}
                  className="input"
                  value={form.notes}
                  onChange={e => setForm((prev: any) => ({ ...prev, notes: e.target.value }))}
                />
              )}
            </FormField>
            <h3 className="font-semibold">Line items</h3>
            {form.lines.map((line: any, index: number) => (
              <div key={line.id || `new-${index}`} className="border border-fd-line rounded p-3 space-y-3">
                <FormField label={`Line ${index + 1} · Part`}>
                  {field =>
                    editable ? (
                      <EntityPicker
                        {...field}
                        kind="part"
                        value={line.part_id}
                        onChange={value => lineChange(index, 'part_id', Number(value))}
                      />
                    ) : (
                      <p>
                        {record.lines[index]?.part?.part_number} — {record.lines[index]?.part?.name}
                      </p>
                    )
                  }
                </FormField>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  <FormField label="Ordered">
                    {field => (
                      <input
                        {...field}
                        type="number"
                        className="input"
                        min="0.0001"
                        step="any"
                        value={line.quantity_ordered}
                        onChange={e => lineChange(index, 'quantity_ordered', e.target.value)}
                      />
                    )}
                  </FormField>
                  <FormField label="Unit price">
                    {field => (
                      <input
                        {...field}
                        type="number"
                        min="0"
                        step="0.01"
                        className="input"
                        value={line.unit_price}
                        onChange={e => lineChange(index, 'unit_price', e.target.value)}
                      />
                    )}
                  </FormField>
                  <FormField label="Required date">
                    {field => (
                      <input
                        {...field}
                        type="date"
                        className="input"
                        value={line.required_date}
                        onChange={e => lineChange(index, 'required_date', e.target.value)}
                      />
                    )}
                  </FormField>
                  <div>
                    <p className="text-sm text-slate-400">Received / remaining</p>
                    <p>
                      {record.lines.find((old: any) => old.id === line.id)?.quantity_received || 0} /{' '}
                      {Math.max(
                        0,
                        Number(line.quantity_ordered) -
                          Number(record.lines.find((old: any) => old.id === line.id)?.quantity_received || 0)
                      )}
                    </p>
                  </div>
                </div>
                <FormField label="Line notes">
                  {field => (
                    <input
                      {...field}
                      className="input"
                      value={line.notes}
                      onChange={e => lineChange(index, 'notes', e.target.value)}
                    />
                  )}
                </FormField>
                {editable && (
                  <button
                    type="button"
                    className="text-red-300 underline"
                    onClick={() =>
                      setForm((prev: any) => ({
                        ...prev,
                        lines: prev.lines.filter((_: any, i: number) => i !== index),
                      }))
                    }
                  >
                    Remove line {index + 1}
                  </button>
                )}
              </div>
            ))}
            {editable && (
              <Button
                variant="secondary"
                onClick={() =>
                  setForm((prev: any) => ({
                    ...prev,
                    lines: [
                      ...prev.lines,
                      { part_id: 0, quantity_ordered: 1, unit_price: 0, required_date: '', notes: '' },
                    ],
                  }))
                }
              >
                Add line
              </Button>
            )}
          </fieldset>
          <p className="text-right font-semibold">
            Line subtotal:{' '}
            {form.lines
              .reduce((sum: number, line: any) => sum + Number(line.quantity_ordered) * Number(line.unit_price), 0)
              .toFixed(2)}
          </p>
          {record.status !== 'draft' && (
            <Link className="text-werco-primary underline" to={`/warehouse?tab=receiving&po=${id}`}>
              Open receiving and receipt history
            </Link>
          )}
          {error && (
            <p role="alert" className="text-red-300">
              {error}{' '}
              <button
                type="button"
                className="underline"
                onClick={() => {
                  if (confirmDiscard()) setAttempt(n => n + 1);
                }}
              >
                Reload current record
              </button>
            </p>
          )}
          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={close} disabled={saving}>
              Close
            </Button>
            {editable && (
              <Button type="submit" disabled={saving}>
                {saving ? 'Saving…' : 'Save draft'}
              </Button>
            )}
          </div>
        </form>
      )}
    </Modal>
  );
}

import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { Button, FormField } from '../ui';
import EntityPicker from './EntityPicker';
import { workspaceIdentity } from '../../hooks/useWorkspaceRecords';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { SupplierConfirmationSubmission } from '../../types/receivingDelivery';

export interface SupplierPO {
  id: number;
  updated_at?: string;
  required_date?: string | null;
  expected_date?: string | null;
  supplier_acknowledged_at?: string | null;
  supplier_confirmed_date?: string | null;
  supplier_confirmation_reference?: string | null;
  supplier_confirmation_note?: string | null;
  follow_up_owner_id?: number | null;
  follow_up_due_date?: string | null;
}
export default function SupplierFollowup({
  record,
  canEdit,
  onSaved,
  onDirtyChange,
  onBusyChange,
  onReload,
}: {
  record: SupplierPO;
  canEdit: boolean;
  onSaved: (record: SupplierPO) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
  onReload?: () => void;
}) {
  const [identity] = useState(() => workspaceIdentity());
  const initial: SupplierConfirmationSubmission = {
    expected_updated_at: record.updated_at || null,
    acknowledged: !!record.supplier_acknowledged_at,
    supplier_confirmed_date: record.supplier_confirmed_date || null,
    supplier_confirmation_reference: record.supplier_confirmation_reference || null,
    supplier_confirmation_note: record.supplier_confirmation_note || '',
    follow_up_owner_id: record.follow_up_owner_id || null,
    follow_up_due_date: record.follow_up_due_date || null,
  };
  const [form, setForm] = useState(initial);
  const [baseline, setBaseline] = useState(JSON.stringify(initial));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const dirty = canEdit && JSON.stringify(form) !== baseline;
  const { markSaved, confirmDiscard } = useUnsavedChanges(dirty);
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);
  const save = async () => {
    if (busy || identity !== workspaceIdentity()) return;
    if (!form.supplier_confirmation_note.trim()) {
      setError('Record the supplier response or follow-up reason.');
      return;
    }
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const result = await api.updateSupplierConfirmation(record.id, form);
      if (identity !== workspaceIdentity()) return;
      markSaved();
      const next = { ...form, expected_updated_at: result.updated_at };
      setForm(next);
      setBaseline(JSON.stringify(next));
      setSaved(true);
      onSaved(result);
    } catch (err: unknown) {
      if (identity !== workspaceIdentity()) return;
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Unable to save supplier response. Your entries are retained.');
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="Supplier confirmation and follow-up" className="border border-fd-line p-3 space-y-3">
      <h3 className="font-semibold">Supplier confirmation and follow-up</h3>
      <p className="text-sm text-slate-400">
        Requested: {record.required_date || 'not set'} · Earlier estimate: {record.expected_date || 'not set'}. These
        dates stay unchanged.
      </p>
      <fieldset disabled={!canEdit || busy} className="space-y-3">
        <label className="flex gap-2">
          <input
            type="checkbox"
            checked={form.acknowledged}
            onChange={e =>
              setForm(previous => ({
                ...previous,
                acknowledged: e.target.checked,
                supplier_confirmed_date: e.target.checked ? previous.supplier_confirmed_date : null,
              }))
            }
          />
          Supplier acknowledged this order
        </label>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <FormField label="Supplier-confirmed arrival date">
            {field => (
              <input
                {...field}
                className="input w-full"
                type="date"
                disabled={!form.acknowledged || !canEdit || busy}
                value={form.supplier_confirmed_date || ''}
                onChange={e => setForm(previous => ({ ...previous, supplier_confirmed_date: e.target.value || null }))}
              />
            )}
          </FormField>
          <FormField label="Supplier reference">
            {field => (
              <input
                {...field}
                className="input w-full"
                maxLength={100}
                value={form.supplier_confirmation_reference || ''}
                onChange={e =>
                  setForm(previous => ({ ...previous, supplier_confirmation_reference: e.target.value || null }))
                }
              />
            )}
          </FormField>
          <FormField label="Follow-up owner">
            {field => (
              <EntityPicker
                {...field}
                kind="user"
                optional
                value={form.follow_up_owner_id || ''}
                disabled={!canEdit || busy}
                onChange={value =>
                  setForm(previous => ({ ...previous, follow_up_owner_id: value ? Number(value) : null }))
                }
              />
            )}
          </FormField>
          <FormField label="Next follow-up date">
            {field => (
              <input
                {...field}
                className="input w-full"
                type="date"
                value={form.follow_up_due_date || ''}
                onChange={e => setForm(previous => ({ ...previous, follow_up_due_date: e.target.value || null }))}
              />
            )}
          </FormField>
        </div>
        <FormField label="Supplier response or follow-up reason">
          {field => (
            <textarea
              {...field}
              className="input w-full"
              rows={3}
              maxLength={2000}
              value={form.supplier_confirmation_note}
              onChange={e => setForm(previous => ({ ...previous, supplier_confirmation_note: e.target.value }))}
            />
          )}
        </FormField>
      </fieldset>
      <p className="text-sm text-slate-400">
        Unconfirmed orders and due follow-ups appear in Action Inbox. Clearing acknowledgment withdraws the confirmed
        date; no message is sent to the supplier.
      </p>
      {error && (
        <p role="alert" className="text-red-300">
          {error}
        </p>
      )}
      {saved && <p role="status">Supplier follow-up saved.</p>}
      {error && onReload && (
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          onClick={() => {
            if (confirmDiscard()) onReload();
          }}
        >
          Reload supplier response
        </Button>
      )}
      {canEdit && (
        <Button type="button" onClick={save} disabled={busy}>
          {busy ? 'Saving supplier response…' : 'Save supplier response'}
        </Button>
      )}
    </section>
  );
}

import React, { useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { toDisplayString } from '../../utils/apiError';
import type { AslStatus, AslVendor, VendorApprovalUpdate } from '../../types/supplierApproval';
import {
  ASL_BASES,
  ASL_SCOPES,
  ASL_STATUSES,
  NOT_ON_ASL_LABEL,
  aslStatusLabel,
  basisLabel,
  derivedIsApproved,
  scopeLabel,
} from '../../utils/supplierApproval';

interface Props {
  /** The vendor whose ASL entry is being decided; null keeps the dialog closed. */
  vendor: AslVendor | null;
  onClose: () => void;
  /** Called with the vendor the SERVER returned (never an optimistic copy). */
  onSaved: (vendor: AslVendor) => void;
}

export interface ApprovalForm {
  approval_status: AslStatus | '';
  approved_scopes: string[];
  approval_bases: string[];
  approval_date: string;
  reevaluation_due: string;
  last_performance_evaluation: string;
  approval_notes: string;
}

const formFor = (vendor: AslVendor): ApprovalForm => ({
  approval_status: vendor.approval_status ?? '',
  approved_scopes: [...(vendor.approved_scopes ?? [])],
  approval_bases: [...(vendor.approval_bases ?? [])],
  approval_date: vendor.approval_date ?? '',
  reevaluation_due: vendor.reevaluation_due ?? '',
  last_performance_evaluation: vendor.last_performance_evaluation ?? '',
  approval_notes: vendor.approval_notes ?? '',
});

const sameList = (a: string[], b: string[]) =>
  a.length === b.length && [...a].sort().join('|') === [...b].sort().join('|');
const ordered = (values: string[], order: string[]) => order.filter(value => values.includes(value));

/**
 * The payload carries `approval_status`, `reason` and the CAS stamp always, and every
 * other field ONLY when the person changed it. That matters: the server applies a field
 * only when it is sent, and it records Central today as the approval date when a vendor
 * becomes approved WITHOUT one — re-sending an untouched blank date would suppress that.
 */
export function buildApprovalPayload(
  initial: ApprovalForm,
  form: ApprovalForm,
  reason: string,
  expectedUpdatedAt: string | null
): VendorApprovalUpdate {
  const payload: VendorApprovalUpdate = {
    approval_status: form.approval_status || null,
    reason: reason.trim(),
    expected_updated_at: expectedUpdatedAt,
  };
  if (!sameList(initial.approved_scopes, form.approved_scopes)) {
    payload.approved_scopes = form.approved_scopes.length ? ordered(form.approved_scopes, ASL_SCOPES) : null;
  }
  if (!sameList(initial.approval_bases, form.approval_bases)) {
    payload.approval_bases = form.approval_bases.length ? ordered(form.approval_bases, ASL_BASES) : null;
  }
  for (const key of ['approval_date', 'reevaluation_due', 'last_performance_evaluation'] as const) {
    if (initial[key] !== form[key]) payload[key] = form[key] || null;
  }
  if (initial.approval_notes.trim() !== form.approval_notes.trim()) {
    payload.approval_notes = form.approval_notes.trim() || null;
  }
  return payload;
}

/**
 * Record a supplier-approval decision on the Approved Supplier List (AS9100D 8.4).
 *
 * Server-gated and non-optimistic: the dialog stays open on a refusal (409 when the
 * vendor changed since it was opened, 503 when the audit row cannot be written) and
 * closes only on success, handing back what the server stored.
 */
export function UpdateApprovalDialog({ vendor, onClose, onSaved }: Props) {
  const [form, setForm] = useState<ApprovalForm | null>(vendor ? formFor(vendor) : null);
  const [initial, setInitial] = useState<ApprovalForm | null>(form);
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const next = vendor ? formFor(vendor) : null;
    setForm(next);
    setInitial(next);
    setReason('');
    setError('');
  }, [vendor]);

  const dirty = useMemo(
    () => !!form && !!initial && (JSON.stringify(form) !== JSON.stringify(initial) || reason.trim() !== ''),
    [form, initial, reason]
  );
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);

  if (!vendor || !form || !initial) return null;

  const status = form.approval_status || null;
  const derived = derivedIsApproved(status);
  const toggle = (key: 'approved_scopes' | 'approval_bases', value: string) =>
    setForm(prev =>
      prev
        ? {
            ...prev,
            [key]: prev[key].includes(value) ? prev[key].filter(item => item !== value) : [...prev[key], value],
          }
        : prev
    );

  const close = () => {
    if (pending) return;
    if (confirmDiscard()) onClose();
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending || !reason.trim()) return;
    setPending(true);
    setError('');
    try {
      const saved = await api.updateVendorApproval(
        vendor.id,
        buildApprovalPayload(initial, form, reason, vendor.updated_at ?? null)
      );
      markSaved();
      onSaved(saved);
    } catch (err: any) {
      setError(toDisplayString(err?.response?.data?.detail) || 'Unable to save the supplier approval.');
    } finally {
      setPending(false);
    }
  };

  const dateField = (
    key: 'approval_date' | 'reevaluation_due' | 'last_performance_evaluation',
    label: string,
    help?: string
  ) => (
    <FormField label={label} help={help}>
      {field => (
        <input
          {...field}
          type="date"
          className="input"
          value={form[key]}
          onChange={e => setForm(prev => (prev ? { ...prev, [key]: e.target.value } : prev))}
        />
      )}
    </FormField>
  );

  return (
    <Modal
      open
      onClose={close}
      size="3xl"
      closeOnBackdrop={false}
      closeOnEscape={!pending}
      ariaLabel={`Update approval for ${vendor.name}`}
    >
      <form onSubmit={submit} className="space-y-4">
        <div>
          <h2 className="text-lg font-semibold text-white">Update approval</h2>
          <p className="text-sm text-slate-400">
            <span className="font-mono">{vendor.code}</span> · {vendor.name} · now{' '}
            <span className="text-slate-200">{aslStatusLabel(vendor.approval_status)}</span>
          </p>
        </div>

        {error && (
          <p role="alert" className="rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
            {error}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="ASL status">
            {field => (
              <select
                {...field}
                className="input"
                value={form.approval_status}
                onChange={e =>
                  setForm(prev => (prev ? { ...prev, approval_status: e.target.value as AslStatus | '' } : prev))
                }
              >
                <option value="">{NOT_ON_ASL_LABEL}</option>
                {ASL_STATUSES.map(value => (
                  <option key={value} value={value}>
                    {aslStatusLabel(value)}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <div className="self-end text-xs text-slate-400" data-testid="derived-approval">
            {derived === null
              ? 'Not on the ASL: the vendor’s Approved flag is left as it is.'
              : derived
                ? 'Purchase orders can be raised to this vendor (Approved flag on).'
                : status === 'disqualified'
                  ? 'Disqualified: every new purchase order to this vendor is refused.'
                  : 'Conditional: the vendor is dropped from the purchase-order vendor list (Approved flag off).'}
          </div>
        </div>

        <fieldset className="border border-slate-700 rounded-sm p-3">
          <legend className="px-1 text-sm font-medium text-slate-300">Approved scope of supply</legend>
          <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {ASL_SCOPES.map(slug => (
              <label key={slug} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.approved_scopes.includes(slug)}
                  onChange={() => toggle('approved_scopes', slug)}
                  className="rounded-sm border-slate-600"
                />
                <span>{scopeLabel(slug)}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="border border-slate-700 rounded-sm p-3">
          <legend className="px-1 text-sm font-medium text-slate-300">Approval basis</legend>
          <div className="grid gap-x-4 gap-y-1 sm:grid-cols-3">
            {ASL_BASES.map(slug => (
              <label key={slug} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.approval_bases.includes(slug)}
                  onChange={() => toggle('approval_bases', slug)}
                  className="rounded-sm border-slate-600"
                />
                <span>{basisLabel(slug)}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-3">
          {dateField(
            'approval_date',
            'Approved date',
            initial.approval_date ? undefined : 'Left blank, approving records today.'
          )}
          {dateField('reevaluation_due', 'Re-evaluation due')}
          {dateField('last_performance_evaluation', 'Last performance evaluation')}
        </div>

        <FormField label="ASL notes">
          {field => (
            <textarea
              {...field}
              rows={2}
              maxLength={4000}
              className="input"
              value={form.approval_notes}
              onChange={e => setForm(prev => (prev ? { ...prev, approval_notes: e.target.value } : prev))}
            />
          )}
        </FormField>

        <FormField label="Reason for this change" required help="Recorded on the audit trail with the change.">
          {field => (
            <textarea
              {...field}
              rows={2}
              required
              maxLength={2000}
              className="input"
              value={reason}
              onChange={e => setReason(e.target.value)}
            />
          )}
        </FormField>

        <div className="flex justify-end gap-2 border-t border-slate-700 pt-3">
          <Button variant="secondary" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={pending} loadingText="Saving…" disabled={!reason.trim()}>
            Save approval
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default UpdateApprovalDialog;

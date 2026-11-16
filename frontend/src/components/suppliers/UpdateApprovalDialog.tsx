import React, { useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { toDisplayString } from '../../utils/apiError';
import { formatCentralDate } from '../../utils/centralTime';
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
 * Whether saving `status` makes this vendor approved when it is not approved now. That is
 * the server's trigger for recording an approval date: a request that makes a not-approved
 * vendor approved and sends no `approval_date` gets Central today, WHATEVER date the vendor
 * already carries (a Conditional vendor's date from the ASL import, or the date of an
 * approval it has since lost) — the new approval decision is today's.
 */
export const becomesApproved = (status: AslStatus | '' | null, vendorIsApproved: boolean): boolean =>
  derivedIsApproved(status || null) === true && !vendorIsApproved;

/**
 * The payload carries `approval_status`, `reason` and the CAS stamp always, and every
 * other field ONLY when the person changed it: the server applies a field only when it is
 * sent.
 *
 * The approval date is the exception, so that the date the dialog SHOWS is the date the
 * server STORES. When the save makes a not-approved vendor approved, a date in the field is
 * always sent (even one equal to the date on file — leaving it out would have the server
 * replace it with today), and a blank field is left out so the server records today, which
 * is what the field's help text says. The dialog blanks the date on file when the status is
 * changed to an approving one, so an untouched old date is never silently replaced.
 */
export function buildApprovalPayload(
  initial: ApprovalForm,
  form: ApprovalForm,
  reason: string,
  expectedUpdatedAt: string | null,
  vendorIsApproved: boolean
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
  for (const key of ['reevaluation_due', 'last_performance_evaluation'] as const) {
    if (initial[key] !== form[key]) payload[key] = form[key] || null;
  }
  if (becomesApproved(form.approval_status, vendorIsApproved)) {
    if (form.approval_date) payload.approval_date = form.approval_date;
  } else if (initial.approval_date !== form.approval_date) {
    payload.approval_date = form.approval_date || null;
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
  // True while the approval date is blank only because choosing an approving status cleared
  // the date on file; choosing a non-approving status again puts that date back.
  const [dateCleared, setDateCleared] = useState(false);

  useEffect(() => {
    const next = vendor ? formFor(vendor) : null;
    setForm(next);
    setInitial(next);
    setReason('');
    setError('');
    setDateCleared(false);
  }, [vendor]);

  const dirty = useMemo(
    () => !!form && !!initial && (JSON.stringify(form) !== JSON.stringify(initial) || reason.trim() !== ''),
    [form, initial, reason]
  );
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);

  if (!vendor || !form || !initial) return null;

  const status = form.approval_status || null;
  const derived = derivedIsApproved(status);
  const approving = becomesApproved(form.approval_status, vendor.is_approved);

  // Choosing a status that makes a not-approved vendor approved records a NEW approval, so
  // the date on file (a Conditional vendor's, or one from an approval it has since lost) is
  // not the approval date any more: blank it, and the help text says today is recorded. A
  // date the person typed is theirs and is kept.
  const changeStatus = (next: AslStatus | '') => {
    const willApprove = becomesApproved(next, vendor.is_approved);
    let approvalDate = form.approval_date;
    let cleared = dateCleared;
    if (willApprove && !cleared && approvalDate && approvalDate === initial.approval_date) {
      approvalDate = '';
      cleared = true;
    } else if (!willApprove && cleared && approvalDate === '') {
      approvalDate = initial.approval_date;
      cleared = false;
    }
    setForm({ ...form, approval_status: next, approval_date: approvalDate });
    setDateCleared(cleared);
  };
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
        buildApprovalPayload(initial, form, reason, vendor.updated_at ?? null, vendor.is_approved)
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
          onChange={e => {
            if (key === 'approval_date') setDateCleared(false);
            setForm(prev => (prev ? { ...prev, [key]: e.target.value } : prev));
          }}
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
                onChange={e => changeStatus(e.target.value as AslStatus | '')}
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
              ? vendor.approval_status
                ? 'Taking the vendor off the ASL turns its Approved flag off; tick Approved on the vendor if it should stay buyable.'
                : 'Not on the ASL: the vendor’s Approved flag is left as it is.'
              : derived
                ? 'Purchase orders can be raised to this vendor (Approved flag on).'
                : status === 'disqualified'
                  ? 'Disqualified: purchase orders to this vendor are refused — new ones, and sending or approving existing drafts.'
                  : 'Conditional: purchase orders can still be raised, each carrying a Conditional warning (Approved flag off).'}
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
            approving
              ? form.approval_date
                ? 'Approving records this date.'
                : dateCleared
                  ? `Approving records today, not the ${formatCentralDate(initial.approval_date)} on file. Enter a date to record another.`
                  : 'Left blank, approving records today.'
              : !form.approval_date && !vendor.is_approved
                ? 'Left blank, approving records today.'
                : undefined
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

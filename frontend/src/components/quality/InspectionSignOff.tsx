/**
 * In-process inspection sign-off — the chip and the Sign off action shared by the
 * Work Order Detail operation rows and the Shop Floor operation cards.
 *
 * The server verb (`POST /shop-floor/operations/{id}/inspection`) is the ONLY writer
 * of `inspection_complete`, takes ADMIN/MANAGER/SUPERVISOR/QUALITY, refuses a
 * standing API token, and fails closed (503) when its audit row cannot be written.
 * Because the server may refuse, this control is NON-optimistic: the chip reads
 * "Inspected" only from row data the caller re-read, or from the 200 this control
 * received; a refusal keeps the dialog open with the server's own text.
 */

import React, { useId, useState } from 'react';
import api from '../../services/api';
import type { OperationInspectionResponse, User } from '../../types';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDateTime } from '../../utils/centralTime';
import { variantClass } from '../../utils/statusColors';
import { Button } from '../ui/Button';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { Modal } from '../ui/Modal';
import { StatusBadge } from '../ui/StatusBadge';
import { useToast } from '../ui/Toast';

/** The roles `mark_operation_inspected` allows (`require_role`), plus the platform principals it waves through. */
const SIGN_OFF_ROLES = new Set(['admin', 'manager', 'supervisor', 'quality', 'platform_admin']);

export function canSignOffInspection(user: Pick<User, 'role' | 'is_superuser'> | null | undefined): boolean {
  if (!user) return false;
  return user.is_superuser === true || SIGN_OFF_ROLES.has(String(user.role));
}

const CHIP_COLORS: Record<string, string> = {
  inspection_required: variantClass.amber,
  inspected: variantClass.green,
};

/** "Inspection required" (amber) or "Inspected" (green); nothing on an operation that is not an inspection point. */
export function InspectionStatusChip({
  requiresInspection,
  inspectionComplete,
  className = '',
}: {
  requiresInspection: boolean;
  inspectionComplete: boolean;
  className?: string;
}) {
  if (!requiresInspection && !inspectionComplete) return null;
  return (
    <StatusBadge
      status={inspectionComplete ? 'inspected' : 'inspection_required'}
      colorMap={CHIP_COLORS}
      className={className}
    />
  );
}

const INSPECTION_TYPES: { value: string; label: string }[] = [
  { value: 'in_process', label: 'In-process' },
  { value: 'first_article', label: 'First article' },
  { value: 'final', label: 'Final' },
];

const NOTES_MAX = 2000;

/**
 * The type the dialog starts at, and the options it offers. The server overwrites
 * `inspection_type` whenever one is sent, and a routing inspection point is stamped
 * `final`, so the dialog starts at the operation's own planned type (offering it even
 * when it is not one of the three listed) and falls back to In-process only when the
 * operation has none.
 */
export function inspectionTypeOptions(currentType: string | null | undefined): {
  initial: string;
  options: { value: string; label: string }[];
} {
  const current = currentType?.trim() || '';
  if (!current) return { initial: 'in_process', options: INSPECTION_TYPES };
  const known = INSPECTION_TYPES.some(t => t.value === current);
  const options = (known ? INSPECTION_TYPES : [...INSPECTION_TYPES, { value: current, label: current }]).map(t =>
    t.value === current ? { ...t, label: `${t.label} (planned)` } : t
  );
  return { initial: current, options };
}

export interface InspectionSignOffControlProps {
  operationId: number;
  /** Human label for the dialog title, e.g. "Op 20 — Deburr". */
  operationLabel: string;
  requiresInspection: boolean;
  inspectionComplete: boolean;
  /** The operation's planned `inspection_type` (a routing inspection point carries `final`). */
  inspectionType?: string | null;
  /** Role gate — pass `canSignOffInspection(user)`. */
  canSignOff: boolean;
  /** Called after a 200 so the caller re-reads its rows. */
  onSignedOff?: (result: OperationInspectionResponse) => void | Promise<void>;
  size?: 'sm' | 'md';
}

/**
 * Chip plus, for a signer on an unsigned inspection point, a Sign off button that
 * opens the notes dialog. Renders nothing for an operation that is not an
 * inspection point and has never been signed.
 */
export function InspectionSignOffControl({
  operationId,
  operationLabel,
  requiresInspection,
  inspectionComplete,
  inspectionType: currentType,
  canSignOff,
  onSignedOff,
  size = 'sm',
}: InspectionSignOffControlProps) {
  const { initial: initialType, options: typeOptions } = inspectionTypeOptions(currentType);
  const { showToast } = useToast();
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [inspectionType, setInspectionType] = useState(initialType);
  const [notes, setNotes] = useState('');
  const [signed, setSigned] = useState<OperationInspectionResponse | null>(null);

  const complete = inspectionComplete || signed?.inspection_complete === true;
  if (!requiresInspection && !complete) return null;

  const openDialog = () => {
    setInspectionType(initialType);
    setNotes('');
    setOpen(true);
  };

  const close = () => {
    if (!pending) setOpen(false);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    try {
      const trimmed = notes.trim();
      // Send a type only when it changes the operation's own: an unchanged one is left
      // for the server to keep, so the audit row records no relabel.
      const changesType = inspectionType && inspectionType !== (currentType?.trim() || '');
      const result = await api.signOffOperationInspection(operationId, {
        ...(changesType ? { inspection_type: inspectionType } : {}),
        ...(trimmed ? { notes: trimmed } : {}),
      });
      setSigned(result);
      setOpen(false);
      showToast(
        'success',
        `Inspection signed off by ${result.signed_off_by_name} at ${formatCentralDateTime(result.signed_off_at)}`
      );
      await onSignedOff?.(result);
    } catch (err) {
      showToast('error', apiErrorDetail(err, 'Could not sign off the inspection'));
    } finally {
      setPending(false);
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <InspectionStatusChip requiresInspection={requiresInspection} inspectionComplete={complete} />
      {signed && (
        <span className="text-xs text-slate-400">
          by {signed.signed_off_by_name}, {formatCentralDateTime(signed.signed_off_at)}
        </span>
      )}
      {canSignOff && requiresInspection && !complete && (
        <Button
          type="button"
          variant="secondary"
          size={size}
          onClick={openDialog}
          aria-label={`Sign off inspection for ${operationLabel}`}
        >
          Sign off
        </Button>
      )}
      <Modal
        open={open}
        onClose={close}
        size="md"
        ariaLabelledBy={titleId}
        closeOnBackdrop={!pending}
        closeOnEscape={!pending}
      >
        <h3 id={titleId} className="text-lg font-semibold text-white">
          Sign off inspection
        </h3>
        <p className="text-sm text-slate-300 mt-1">
          {operationLabel}. Records that you performed this inspection; your name and the time are written to the audit
          log.
        </p>
        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <FormField label="Inspection type">
            {field => (
              <select
                {...field}
                className="input"
                value={inspectionType}
                disabled={pending}
                onChange={e => setInspectionType(e.target.value)}
              >
                {typeOptions.map(t => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField label="Notes (optional)" help={`What was checked and the result. Up to ${NOTES_MAX} characters.`}>
            {field => (
              <textarea
                {...field}
                className="input min-h-[96px]"
                value={notes}
                maxLength={NOTES_MAX}
                disabled={pending}
                onChange={e => setNotes(e.target.value)}
              />
            )}
          </FormField>
          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" variant="secondary" onClick={close} disabled={pending}>
              Cancel
            </Button>
            <LoadingButton type="submit" loading={pending} loadingText="Signing off…">
              Sign off
            </LoadingButton>
          </div>
        </form>
      </Modal>
    </span>
  );
}

export default InspectionSignOffControl;

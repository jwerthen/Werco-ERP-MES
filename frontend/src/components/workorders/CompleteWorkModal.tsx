/**
 * CompleteWorkModal — the office "Complete" dialog for a work order or one of
 * its operations.
 *
 * Replaces the stacked native prompt() dialogs that used to collect quantity
 * complete + quantity scrapped on WorkOrderDetail. It adds the missing piece
 * the backend now enforces (HTTP 422 otherwise): when scrap > 0, a non-blank
 * `scrap_reason` is required for AS9100D defect traceability. The reason field
 * only appears (and is only required) once scrap is greater than zero, and the
 * Complete button stays disabled until the form is valid — so the UI never
 * fires a request it knows the server will reject.
 *
 * This is a SERVER-GATED completion: it is intentionally NON-optimistic. The
 * caller awaits the server, reflects only what comes back, and surfaces the
 * server's verbatim error on failure (see WorkOrderDetail handlers).
 *
 * The scrap-reason list reuses the canonical SCRAP_REASONS from kioskConstants
 * so a reason chosen here is stored in the same column, with the same labels,
 * as the kiosk and desktop shop-floor flows.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { CheckCircleIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { SelectField } from '../ui/SelectField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import { SCRAP_REASONS } from '../kiosk/kioskConstants';

export interface CompleteWorkSubmit {
  quantityComplete: number;
  quantityScrapped: number;
  /** Non-blank only when quantityScrapped > 0; undefined otherwise. */
  scrapReason?: string;
}

interface CompleteWorkModalProps {
  open: boolean;
  onClose: () => void;
  /** Awaited; throw to surface a server error. Caller closes on success. */
  onSubmit: (values: CompleteWorkSubmit) => Promise<void>;
  /** In-flight flag — disables inputs and spins the submit button. */
  submitting: boolean;
  /** Dialog title, e.g. "Complete Work Order WO-0042" or an operation name. */
  title: string;
  /** Optional context line under the title (e.g. "Ordered: 10"). */
  subtitle?: string;
  /** Pre-fills the quantity-complete field and caps it (the ordered/target qty). */
  defaultQuantityComplete: number;
}

const SCRAP_OPTIONS = SCRAP_REASONS.map((r) => ({ value: r.value, label: r.label }));

export function CompleteWorkModal({
  open,
  onClose,
  onSubmit,
  submitting,
  title,
  subtitle,
  defaultQuantityComplete,
}: CompleteWorkModalProps) {
  const [qtyComplete, setQtyComplete] = useState<string>(String(defaultQuantityComplete));
  const [qtyScrapped, setQtyScrapped] = useState<string>('0');
  const [scrapReason, setScrapReason] = useState<string>('');

  // Reset to defaults each time the dialog opens so a prior session's entries
  // never leak into the next completion.
  useEffect(() => {
    if (open) {
      setQtyComplete(String(defaultQuantityComplete));
      setQtyScrapped('0');
      setScrapReason('');
    }
  }, [open, defaultQuantityComplete]);

  const completeNum = Number(qtyComplete);
  const scrappedNum = Number(qtyScrapped);

  const completeError = useMemo(() => {
    if (qtyComplete.trim() === '') return 'Required';
    if (!Number.isFinite(completeNum) || completeNum < 0) return 'Must be a non-negative number';
    if (completeNum > defaultQuantityComplete) return `Cannot exceed ${defaultQuantityComplete}`;
    return null;
  }, [qtyComplete, completeNum, defaultQuantityComplete]);

  const scrappedError = useMemo(() => {
    if (qtyScrapped.trim() === '') return null; // blank reads as 0
    if (!Number.isFinite(scrappedNum) || scrappedNum < 0) return 'Must be a non-negative number';
    return null;
  }, [qtyScrapped, scrappedNum]);

  const needsReason = Number.isFinite(scrappedNum) && scrappedNum > 0;
  const reasonError = needsReason && !scrapReason ? 'Required when scrap is greater than zero.' : null;

  const canSubmit = !submitting && !completeError && !scrappedError && !reasonError;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    await onSubmit({
      quantityComplete: completeNum,
      quantityScrapped: Number.isFinite(scrappedNum) ? scrappedNum : 0,
      scrapReason: needsReason ? scrapReason : undefined,
    });
  };

  return (
    <Modal open={open} onClose={onClose} size="md" padded={false} ariaLabelledBy="complete-work-title">
      <form onSubmit={handleSubmit}>
        <div className="modal-header">
          <div>
            <h2 id="complete-work-title" className="text-lg font-semibold text-slate-100">
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
          </div>
        </div>

        <div className="modal-body space-y-4">
          <FormField label="Quantity completed" required error={completeError}>
            {(field) => (
              <input
                {...field}
                type="number"
                min={0}
                max={defaultQuantityComplete}
                step="any"
                autoFocus
                disabled={submitting}
                className={completeError ? 'input-error' : 'input'}
                value={qtyComplete}
                onChange={(e) => setQtyComplete(e.target.value)}
              />
            )}
          </FormField>

          <FormField label="Quantity scrapped" error={scrappedError}>
            {(field) => (
              <input
                {...field}
                type="number"
                min={0}
                step="any"
                disabled={submitting}
                className={scrappedError ? 'input-error' : 'input'}
                value={qtyScrapped}
                onChange={(e) => setQtyScrapped(e.target.value)}
              />
            )}
          </FormField>

          {/* Scrap reason is required for defect traceability when scrap > 0
              (reuses the shared SCRAP_REASONS — same column the kiosk writes). */}
          {needsReason && (
            // SelectField doesn't take native id/aria-* props, so it isn't wired
            // via FormField's render-prop spread; its own ariaLabel carries the
            // accessible name. FormField still renders the label/required/error
            // chrome consistently with the rest of the form.
            <FormField label="Scrap reason" required error={reasonError}>
              <SelectField
                value={scrapReason}
                onChange={(value) => setScrapReason(String(value))}
                options={SCRAP_OPTIONS}
                placeholder="Select a scrap reason"
                disabled={submitting}
                ariaLabel="Scrap reason"
              />
            </FormField>
          )}
        </div>

        <div className="modal-footer">
          <Button type="button" variant="secondary" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={submitting} loadingText="Completing..." disabled={!canSubmit}>
            <CheckCircleIcon className="mr-2 h-5 w-5" />
            Complete
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default CompleteWorkModal;

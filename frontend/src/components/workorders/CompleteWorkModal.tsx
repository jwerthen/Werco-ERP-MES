/**
 * CompleteWorkModal — the office "Complete" dialog for a work order or one of
 * its operations.
 *
 * Replaces the stacked native prompt() dialogs that used to collect quantity
 * complete + quantity scrapped on WorkOrderDetail. It adds the missing piece
 * the backend enforces (HTTP 422 otherwise): when scrap > 0, a scrap reason is
 * required for AS9100D defect traceability. Lean Phase 1: when the company has
 * ACTIVE scrap reason codes the reason is a REQUIRED code pick (+ optional
 * free-text detail) and the submit carries scrapReasonCodeId; with zero codes
 * the dialog keeps the legacy required SCRAP_REASONS picker. Either way the
 * fields only appear (and are only required) once scrap is greater than zero,
 * and the Complete button stays disabled until the form is valid — so the UI
 * never fires a request it knows the server will reject.
 *
 * This is a SERVER-GATED completion: it is intentionally NON-optimistic. The
 * caller awaits the server, reflects only what comes back, and surfaces the
 * server's verbatim error on failure (see WorkOrderDetail handlers).
 *
 * `scrapReason` is ALWAYS non-blank when scrap > 0 — typed detail, else the
 * chosen code's "CODE — Name" label, else the legacy reason — because the
 * operation-level complete endpoint only understands free text; the WO-level
 * endpoint additionally receives the structured code id.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { CheckCircleIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import {
  EMPTY_SCRAP_SELECTION,
  ScrapReasonFields,
  isScrapSelectionComplete,
  scrapSelectionText,
} from '../quality/ScrapReasonFields';
import { useScrapReasonCodes } from '../../hooks/useScrapReasonCodes';

export interface CompleteWorkSubmit {
  quantityComplete: number;
  quantityScrapped: number;
  /** Non-blank only when quantityScrapped > 0; undefined otherwise. */
  scrapReason?: string;
  /** Company scrap-code id, when one was chosen (codes mode only). */
  scrapReasonCodeId?: number;
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
  const [scrap, setScrap] = useState(EMPTY_SCRAP_SELECTION);
  // Company scrap codes ([] -> legacy SCRAP_REASONS fallback, fail-soft).
  const { codes: scrapCodes } = useScrapReasonCodes();

  // Reset to defaults each time the dialog opens so a prior session's entries
  // never leak into the next completion.
  useEffect(() => {
    if (open) {
      setQtyComplete(String(defaultQuantityComplete));
      setQtyScrapped('0');
      setScrap(EMPTY_SCRAP_SELECTION);
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
  const reasonMissing = needsReason && !isScrapSelectionComplete(scrapCodes, scrap);

  const canSubmit = !submitting && !completeError && !scrappedError && !reasonMissing;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    await onSubmit({
      quantityComplete: completeNum,
      quantityScrapped: Number.isFinite(scrappedNum) ? scrappedNum : 0,
      scrapReason: needsReason ? scrapSelectionText(scrapCodes, scrap) : undefined,
      scrapReasonCodeId: needsReason && scrapCodes.length > 0 && scrap.codeId != null ? scrap.codeId : undefined,
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

          {/* Scrap reason is required for defect traceability when scrap > 0 —
              company scrap codes when defined, legacy SCRAP_REASONS fallback
              otherwise (shared ScrapReasonFields fragment). */}
          {needsReason && (
            <ScrapReasonFields codes={scrapCodes} value={scrap} onChange={setScrap} disabled={submitting} />
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

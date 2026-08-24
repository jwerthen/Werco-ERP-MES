/**
 * Mark a part Inactive — or put it back.
 *
 * ---------------------------------------------------------------------------
 * THE GAP THIS CLOSES
 * ---------------------------------------------------------------------------
 * A numbering recut leaves empty SKUs behind: `0.0625-48X120-304SS` at zero,
 * nothing on order, nothing tied, and no reason for it to keep appearing in
 * every picker on the floor. Until these two verbs existed there was no way to
 * say so. `PartUpdate` carries neither `is_active` nor `status` — deliberately,
 * because it applies a blind `setattr` loop on handlers that do not filter
 * `is_deleted` — so the ONLY writer of `parts.is_active` was `delete_material`,
 * which also sets `is_deleted`. The choice was tombstone it or leave it. This
 * dialog is the middle: switched off, still in the catalog, every traveler, MTR
 * and closed PO bearing the number still resolving to it.
 *
 * ---------------------------------------------------------------------------
 * WHY BOTH VERBS REFUSE A SOFT-DELETED PART, AND WHY THIS SCREEN SAYS SO
 * ---------------------------------------------------------------------------
 * `is_active` doubles as the soft-delete MASK: `delete_material` sets
 * `is_deleted` AND `is_active=False` AND `status="obsolete"` together. A verb
 * that could write `is_active = True` on a tombstoned row would therefore be
 * clearing half a delete — the exact 2026-08-16 `Vendor` trap, in the parts
 * table. Both verbs 404 on a deleted part instead, and the copy below routes the
 * operator to Restore rather than leaving them to read a bare "not found".
 *
 * ---------------------------------------------------------------------------
 * SERVER-GATED, THEREFORE NON-OPTIMISTIC
 * ---------------------------------------------------------------------------
 * Deactivating is refused **409** while the part still holds stock and the
 * acknowledgement is unticked. Nothing here paints the new state and rolls back:
 * the caller is handed the SERVER's `{is_active, status}` pair, never a locally
 * flipped copy — which also keeps the two fields from drifting apart on screen,
 * since the server always writes them together.
 */

import React, { useEffect, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, StatusBadge, useToast } from '../ui';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { toDisplayString } from '../../utils/apiError';
import type { Part, PartActivationResult } from '../../types';

/** The part fields this dialog reads, so any list row can be handed straight in. */
export type ActivationPart = Pick<Part, 'id' | 'part_number' | 'name' | 'is_active' | 'status'>;

export interface PartActivationDialogProps {
  open: boolean;
  part: ActivationPart | null;
  onClose: () => void;
  /** Receives the SERVER's result — never a locally flipped part. */
  onChanged: (result: PartActivationResult) => void;
}

export default function PartActivationDialog({ open, part, onClose, onChanged }: PartActivationDialogProps) {
  const { showToast } = useToast();
  const [reason, setReason] = useState('');
  const [acknowledgeStock, setAcknowledgeStock] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState('');

  /**
   * The direction is derived from `is_active`, not passed in.
   *
   * `is_active` is the column these two verbs write, so deriving from it is what
   * keeps the button label, the copy and the request from ever disagreeing about
   * which way this is going. `status` is written in lock-step by the server and
   * is shown, not decided from.
   */
  const deactivating = Boolean(part?.is_active);

  useEffect(() => {
    if (open) return;
    setReason('');
    setAcknowledgeStock(false);
    setServerError('');
  }, [open]);

  const isDirty = open && !saving && (reason.trim().length > 0 || acknowledgeStock);
  const { confirmDiscard } = useUnsavedChanges(isDirty, 'Discard this change without saving?');

  const requestClose = () => {
    if (saving) return;
    if (!confirmDiscard()) return;
    onClose();
  };

  // A reason is mandatory on the way OUT and optional on the way back IN.
  // Switching a part off removes it from every picker on the floor and someone
  // will ask why; switching it back on is the permissive direction, and the
  // audit row records who did it either way.
  const canSubmit = Boolean(part) && !saving && (!deactivating || reason.trim().length > 0);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!part || !canSubmit) return;
    setSaving(true);
    setServerError('');
    try {
      const result = deactivating
        ? await api.deactivatePart(part.id, {
            reason: reason.trim(),
            acknowledge_remaining_stock: acknowledgeStock,
          })
        : await api.activatePart(part.id, reason.trim() ? { reason: reason.trim() } : undefined);

      // `no_op` is a SUCCESS — the record already says what was asked. Reporting
      // it as a win would imply a change that never happened, and reporting it as
      // a failure would send someone chasing a record that is already right.
      showToast(
        result.no_op ? 'info' : 'success',
        result.no_op
          ? `${result.part_number} was already ${result.is_active ? 'active' : 'inactive'} — nothing changed.`
          : result.is_active
            ? `${result.part_number} is active again and back on the pickers.`
            : `${result.part_number} is now inactive. It is still in the catalog and nothing was deleted.`
      );
      onChanged(result);
    } catch (err) {
      // Kept OPEN so a 409 does not cost the operator their typing, and so the
      // acknowledgement the refusal is asking for is one tick away.
      setServerError(
        toDisplayString((err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail) ||
          `Could not ${deactivating ? 'deactivate' : 'activate'} this item.`
      );
    } finally {
      setSaving(false);
    }
  };

  if (!part) return null;

  return (
    <Modal
      open={open}
      onClose={requestClose}
      closeOnBackdrop={!saving}
      closeOnEscape={!saving}
      size="lg"
      ariaLabelledBy="part-activation-title"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h3 id="part-activation-title" className="text-lg font-semibold text-white">
            {deactivating ? 'Mark inactive' : 'Mark active'} — {part.part_number}
          </h3>
          <StatusBadge status={part.status} />
        </div>
        <p className="text-sm text-slate-400">{part.name}</p>

        {deactivating ? (
          <div className="space-y-2 text-sm text-slate-300">
            <p>
              This takes <strong>{part.part_number}</strong> off the pickers — new work orders, BOM lines,
              purchase orders and receipts stop offering it. Nothing else changes.
            </p>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-400">
              <li>It is <strong>not deleted</strong>. The part stays in the catalog.</li>
              <li>Stock on hand is not moved, consumed or written off.</li>
              <li>
                Open work orders, POs, lots and closed history that already name it keep naming it — this is not
                retroactive.
              </li>
              <li>You can switch it back on from here at any time.</li>
            </ul>
          </div>
        ) : (
          <div className="space-y-2 text-sm text-slate-300">
            <p>
              This puts <strong>{part.part_number}</strong> back on the pickers so it can be ordered, received and
              built with again.
            </p>
            <p className="text-xs text-slate-400">
              If this item was <em>deleted</em> rather than switched off, this will be refused — restore it from
              the deleted view first. Undoing a delete is a different decision from switching an item back on, and
              this verb deliberately cannot do both.
            </p>
          </div>
        )}

        <FormField
          label="Reason"
          required={deactivating}
          help="Recorded on the audit trail with your name."
        >
          {(field) => (
            <input
              {...field}
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="input"
              disabled={saving}
              placeholder={
                deactivating
                  ? 'e.g. Combined onto SH-A240-304-0.0625-60X144-2B; number retired'
                  : 'e.g. Back in use for the Miratech job'
              }
              data-testid="part-activation-reason"
              autoFocus
              required={deactivating}
            />
          )}
        </FormField>

        {deactivating && (
          <div className="rounded-sm border border-fd-line px-3 py-2">
            <label className="flex items-start gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                className="mt-0.5 rounded border-slate-600 text-werco-navy-600"
                checked={acknowledgeStock}
                onChange={(event) => setAcknowledgeStock(event.target.checked)}
                disabled={saving}
                data-testid="part-activation-ack-stock"
              />
              <span>
                Switch it off even though it still has stock on hand.
                <span className="mt-0.5 block text-slate-500">
                  Leave this unticked for an empty item. The server refuses an item that still holds stock unless
                  you tick it — that stock is not moved, consumed or deleted either way; it just stops being
                  offered on new work.
                </span>
              </span>
            </label>
          </div>
        )}

        {/* The server's verbatim refusal — it names the condition (stock still on
            hand, or a deleted part that needs restoring first), which is exactly
            what the operator needs and what a generic message would throw away. */}
        {serverError && (
          <div
            role="alert"
            data-testid="part-activation-error"
            className="flex items-start gap-2 rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
          >
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden="true" />
            <span>{serverError}</span>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={requestClose} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton
            type="submit"
            variant={deactivating ? 'danger' : 'primary'}
            loading={saving}
            loadingText="Saving…"
            disabled={!canSubmit}
            data-testid="part-activation-submit"
          >
            {deactivating ? 'Mark inactive' : 'Mark active'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

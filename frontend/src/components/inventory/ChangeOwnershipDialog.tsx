import React, { useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton } from '../ui';
import { Modal } from '../ui/Modal';
import OwnershipFields from '../ownership/OwnershipFields';
import OwnershipChip from '../ownership/OwnershipChip';
import type { OwnerDirectory } from '../../hooks/useOwnerDirectory';
import type { InventoryOwnershipChangeResult, OwnershipInfo, OwnershipSelection } from '../../types/ownership';
import {
  isOwnershipComplete,
  ownershipLabel,
  ownershipOf,
  ownershipPayload,
  sameOwnership,
} from '../../utils/ownership';
import { apiErrorDetail } from '../../utils/apiError';

export interface OwnershipStockRow extends OwnershipInfo {
  id: number;
  part?: { part_number: string; name?: string };
  location: string;
  warehouse?: string;
  lot_number?: string | null;
  serial_number?: string | null;
  quantity_on_hand: number;
}

const selectionOf = (item: OwnershipStockRow): OwnershipSelection => ({
  ownership: ownershipOf(item),
  owner_customer_id: item.owner_customer_id ?? null,
  owner_vendor_id: item.owner_vendor_id ?? null,
});

/**
 * Change who owns one stock row — company, one customer, or one supplier (AS9100D 8.5.3).
 *
 * Server-GATED, so NON-optimistic: nothing on the page changes until the server
 * answers, and a refusal (409 inactive / zero-quantity row or "already ..." no-op, 404
 * owner outside the company, 422 kind/owner mismatch, 503 audit unavailable) is shown
 * verbatim with the dialog left open. While the request is in flight the dialog cannot
 * be dismissed. The reason is required: it goes on the audit row with old -> new.
 *
 * Mount it with `key={item.id}` so opening another row starts from that row's owner.
 */
export default function ChangeOwnershipDialog({
  item,
  directory,
  onClose,
  onChanged,
}: {
  item: OwnershipStockRow;
  directory: OwnerDirectory;
  onClose: () => void;
  /**
   * Called once the server has accepted the change, with the response — the updated row
   * and its two disclosures (`open_pinned_ties`, `unpinned_demand`), which the host must
   * surface (`ownershipChangeNotice`). Closing the dialog is the host's job.
   */
  onChanged: (item: OwnershipStockRow, result: InventoryOwnershipChangeResult) => void;
}) {
  const [selection, setSelection] = useState<OwnershipSelection>(() => selectionOf(item));
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  const close = () => {
    if (!pending) onClose();
  };

  const unchanged = sameOwnership(selection, item);
  const canSubmit = !pending && !unchanged && isOwnershipComplete(selection) && !!reason.trim();

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending) return;
    if (!isOwnershipComplete(selection)) {
      setError(selection.ownership === 'customer' ? 'Choose the owning customer.' : 'Choose the owning supplier.');
      return;
    }
    if (!reason.trim()) {
      setError('Enter the reason for the change of ownership.');
      return;
    }
    setPending(true);
    setError('');
    try {
      const result = await api.changeInventoryOwnership(item.id, {
        ...ownershipPayload(selection),
        reason: reason.trim(),
      });
      onChanged(item, result);
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'Unable to change ownership. Try again.'));
    } finally {
      setPending(false);
    }
  };

  const currentLabel = ownershipLabel(item);

  return (
    <Modal open onClose={close} size="lg" closeOnBackdrop={false} closeOnEscape={!pending} ariaLabel="Change ownership">
      <form onSubmit={submit} className="space-y-4">
        <h3 className="text-lg font-semibold">Change ownership</h3>
        <div className="border border-fd-line bg-fd-panel p-3 text-sm space-y-1">
          <p className="font-medium">
            {item.part?.part_number ?? `Record #${item.id}`}
            {item.part?.name ? <span className="text-slate-400"> · {item.part.name}</span> : null}
          </p>
          <p className="text-slate-400">
            {item.warehouse ? `${item.warehouse} / ` : ''}
            {item.location} · Lot {item.lot_number || '—'}
            {item.serial_number ? ` · Serial ${item.serial_number}` : ''} · On hand {item.quantity_on_hand}
          </p>
          <p>
            Currently:{' '}
            {currentLabel ? <OwnershipChip record={item} /> : <span className="text-slate-300">Company-owned</span>}
          </p>
        </div>
        <p className="text-sm text-slate-400">
          Changes who owns this whole stock row. No quantity moves and no ledger row is posted; the change and its
          reason are recorded on the audit trail.
        </p>
        <OwnershipFields
          value={selection}
          onChange={next => {
            setSelection(next);
            setError('');
          }}
          directory={directory}
          disabled={pending}
          currentOwnerName={item.owner_name}
        />
        <FormField label="Reason" required>
          {field => (
            <textarea
              {...field}
              className="input w-full"
              rows={2}
              maxLength={2000}
              value={reason}
              disabled={pending}
              onChange={event => {
                setReason(event.target.value);
                setError('');
              }}
            />
          )}
        </FormField>
        {unchanged && !error && <p className="text-sm text-slate-400">Choose a different owner to change this row.</p>}
        {error && (
          <p role="alert" className="border border-red-500/40 p-3 text-sm text-red-300">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-3">
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={pending} loadingText="Saving…" disabled={!canSubmit}>
            Change ownership
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

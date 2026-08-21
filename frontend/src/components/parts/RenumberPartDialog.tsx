/**
 * Renumber a part in place.
 *
 * NOT a ConfirmDialog: that primitive types `message` as a string, and this screen
 * has to render a computed before/after spec panel and a diagnostics list. NOT an
 * InputDialog either: that is single-field by contract and this needs two (the new
 * number and a required reason). Same reasoning the Receiving screen uses — Clear
 * Hold rides InputDialog because it captures one line; Void is hand-built because
 * it needs a required reason alongside other content.
 *
 * NON-OPTIMISTIC, mandatory here (the CLAUDE.md convention): this is a
 * server-GATED action whose whole point is that the server may refuse. Nothing is
 * mutated locally before the response; the caller receives the SERVER's part object
 * on success, never a locally patched copy. Closing is the caller's job via `open`,
 * so a 409 keeps the dialog up with the server's verbatim `detail` and the typed
 * value intact.
 *
 * The impact read is debounced through the shared `useDebouncedValue` hook (the
 * standard list-search pattern, ~14 adopters) rather than a hand-rolled setTimeout.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal, FormField, Button, LoadingButton, useToast } from '../ui';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import api from '../../services/api';
import { Part, PartRenumberImpact, PartRenumberResult, SheetSpecDelta } from '../../types';
import { toDisplayString } from '../../utils/apiError';

interface RenumberPartDialogProps {
  open: boolean;
  part: Part | null;
  onClose: () => void;
  /** Receives the SERVER's result — never a locally patched part. */
  onRenumbered: (result: PartRenumberResult) => void;
}

/** How the nest matcher reads a number, in a planner's words rather than field names. */
function describeSpec(
  thickness?: string | null,
  size?: string | null,
  alloy?: string | null
): string {
  const parts = [thickness, size, alloy].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'nothing — no thickness, size or grade';
}

function SheetSpecPanel({ sheet, oldNumber, newNumber }: { sheet: SheetSpecDelta; oldNumber: string; newNumber: string }) {
  // Only meaningful for flat stock. For everything else the matcher never reads
  // the number at all, and showing an empty spec panel would imply otherwise.
  if (!sheet.is_sheet_like_before && !sheet.is_sheet_like_after) return null;

  return (
    <div className="rounded border border-surface-200 bg-surface-50 p-3 text-sm">
      <p className="font-medium text-surface-900 mb-2">What the nest screen reads from the number</p>
      <dl className="space-y-1">
        <div className="flex gap-2">
          <dt className="w-28 shrink-0 text-surface-500 tabular-nums">{oldNumber}</dt>
          <dd className="text-surface-700">
            {describeSpec(sheet.thickness_before, sheet.sheet_size_before, sheet.alloy_before)}
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-28 shrink-0 text-surface-500 tabular-nums">{newNumber || '—'}</dt>
          <dd className="text-surface-700">
            {describeSpec(sheet.thickness_after, sheet.sheet_size_after, sheet.alloy_after)}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export default function RenumberPartDialog({ open, part, onClose, onRenumbered }: RenumberPartDialogProps) {
  const { showToast } = useToast();
  const [newNumber, setNewNumber] = useState('');
  const [reason, setReason] = useState('');
  const [impact, setImpact] = useState<PartRenumberImpact | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const debouncedNumber = useDebouncedValue(newNumber.trim(), 300);

  useEffect(() => {
    if (!open) {
      setNewNumber('');
      setReason('');
      setImpact(null);
      setServerError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open || !part) return;
    let cancelled = false;
    setImpactLoading(true);
    api
      .getPartRenumberImpact(part.id, debouncedNumber || undefined)
      .then(result => {
        if (!cancelled) setImpact(result);
      })
      .catch(() => {
        // A failed preview must not block the attempt — the server re-checks
        // everything on the write anyway, and refusing to let someone try because
        // the PREVIEW broke would be the worse failure.
        if (!cancelled) setImpact(null);
      })
      .finally(() => {
        if (!cancelled) setImpactLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, part, debouncedNumber]);

  const currentNumber = part?.part_number ?? '';
  const trimmedNew = newNumber.trim().toUpperCase();
  const isNoChange = trimmedNew !== '' && trimmedNew === currentNumber.toUpperCase();
  const blockers = impact?.blockers ?? [];

  const canSubmit = useMemo(
    () =>
      Boolean(part) &&
      trimmedNew.length > 0 &&
      !isNoChange &&
      reason.trim().length > 0 &&
      blockers.length === 0 &&
      !impactLoading &&
      !saving,
    [part, trimmedNew, isNoChange, reason, blockers.length, impactLoading, saving]
  );

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!part || !canSubmit) return;
    setSaving(true);
    setServerError(null);
    try {
      const result = await api.renumberPart(part.id, {
        new_part_number: trimmedNew,
        // The compare-and-swap precondition: what the client last READ.
        expected_part_number: currentNumber,
        reason: reason.trim(),
      });
      showToast(
        'success',
        `${result.previous_part_number} is now ${result.part_number}. The old number still finds this part.`
      );
      onRenumbered(result);
    } catch (err: any) {
      // Kept OPEN on failure so a 409 does not cost the operator their typing.
      setServerError(toDisplayString(err?.response?.data?.detail) || 'Could not renumber this part.');
    } finally {
      setSaving(false);
    }
  };

  if (!part) return null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      closeOnBackdrop={!saving}
      closeOnEscape={!saving}
      size="lg"
      ariaLabelledBy="renumber-part-title"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <h3 id="renumber-part-title" className="text-lg font-semibold text-white">
          Renumber {currentNumber}
        </h3>
        <p className="text-sm text-surface-700">
          This changes the number on the part itself. It is not a new part — the same stock, the same
          open jobs, the same drawings and the same BOM lines stay attached to it. The old number keeps
          working: anything typed, scanned or imported as <strong>{currentNumber}</strong> will still
          find this part.
        </p>

        <div className="grid gap-3 sm:grid-cols-2 text-sm">
          <div className="rounded border border-surface-200 p-3">
            <p className="font-medium text-surface-900 mb-1">Keeps the old number — on purpose</p>
            <ul className="list-disc pl-4 space-y-0.5 text-surface-600">
              <li>Certs, packing slips and travelers already printed or shipped.</li>
              <li>Closed POs, received lots and stock movements already posted.</li>
              <li>The audit trail — every entry made before today.</li>
            </ul>
          </div>
          <div className="rounded border border-surface-200 p-3">
            <p className="font-medium text-surface-900 mb-1">Moves to the new number</p>
            <ul className="list-disc pl-4 space-y-0.5 text-surface-600">
              <li>On-hand stock — every lot, every location.</li>
              <li>Open work orders, open POs, and every BOM that uses this part.</li>
              <li>Paperwork printed from now on.</li>
            </ul>
          </div>
        </div>

        <FormField label="New part number" required>
          {field => (
            <input
              {...field}
              type="text"
              value={newNumber}
              onChange={event => setNewNumber(event.target.value)}
              className="input font-mono"
              disabled={saving}
              autoFocus
              required
            />
          )}
        </FormField>

        {isNoChange && (
          <p className="text-sm text-surface-500">That is already this part&apos;s number.</p>
        )}

        {impact && trimmedNew && !isNoChange && (
          <SheetSpecPanel sheet={impact.sheet} oldNumber={currentNumber} newNumber={trimmedNew} />
        )}

        {/* Blockers render the server's verbatim detail, never a prettified code —
            so this list can never disagree with the 409 the operator would get. */}
        {blockers.map(blocker => (
          <div
            key={blocker.code}
            className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800"
            role="alert"
          >
            {blocker.detail}
          </div>
        ))}

        {(impact?.advisories ?? []).map(advisory => (
          <div
            key={advisory.code}
            className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 flex gap-2"
          >
            <ExclamationTriangleIcon className="h-5 w-5 shrink-0 text-amber-600" aria-hidden="true" />
            <span>{advisory.detail}</span>
          </div>
        ))}

        {/* The floor consequence, stated only when there is one. `operations_needing_repair`
            is the actionable count; the raw prefix count would put a large number in
            front of someone for rows that need nothing. */}
        {impact && impact.operations_with_stale_prefix > 0 && (
          <p className="text-sm text-surface-600">
            {impact.operations_with_stale_prefix === 1
              ? '1 operation on open jobs still shows '
              : `${impact.operations_with_stale_prefix} operations on open jobs still show `}
            <strong>{currentNumber}</strong> in its name. Those job cards are left exactly as they
            were printed — if a traveler in the rack matters, reprint it.
          </p>
        )}

        {impact && impact.existing_aliases.length > 0 && (
          <p className="text-sm text-surface-500">
            Also still finds this part: {impact.existing_aliases.join(', ')}
          </p>
        )}

        <FormField label="Reason" required help="Recorded on the audit trail and kept with the old number.">
          {field => (
            <input
              {...field}
              type="text"
              value={reason}
              onChange={event => setReason(event.target.value)}
              className="input"
              disabled={saving}
              placeholder="e.g. Customer revised the print number"
              required
            />
          )}
        </FormField>

        {serverError && (
          <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800" role="alert">
            {serverError}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={saving} disabled={!canSubmit}>
            Renumber part
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

/**
 * "Tie this sheet" for one untied laser nest.
 *
 * Posts through the EXISTING `POST /work-orders/{id}/material-allocations`
 * (via `api.tieNestSheet`) — there is no new write route. The body is the
 * worklist row's `proposed_tie` (the import's own tie shape: an operation-scoped
 * `nest` tie, one sheet per run, planned for the nest's planned runs, unpinned).
 *
 * - A **matched** row offers the matcher's one part.
 * - An **ambiguous** row offers the shortlist with NOTHING preselected: picking
 *   one is the planner's choice, not the matcher's, and it is sent as the same
 *   body with that `part_id`.
 *
 * Server-gated and NON-optimistic: the dialog stays open while the call is on
 * the wire, a refusal is shown verbatim inline (the dialog stays up for a
 * retry), and only a 201 closes it — the caller then refetches the list.
 */

import React, { useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { Button, LoadingButton, Modal } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import type { ProposedNestTie, SheetCandidate, UntiedNestRow } from '../../types/untiedNests';

export interface TieNestSheetDialogProps {
  row: UntiedNestRow | null;
  onClose: () => void;
  /** Called after a 201, with the part number that was tied. */
  onTied: (row: UntiedNestRow, partNumber: string) => void;
}

/** The parts this row may be tied to from the worklist, best first. */
export function tieOptions(row: UntiedNestRow): SheetCandidate[] {
  const suggestion = row.suggestion;
  if (!suggestion) return [];
  if (suggestion.status === 'matched' && suggestion.candidate) return [suggestion.candidate];
  if (suggestion.status === 'ambiguous') return suggestion.candidates;
  return [];
}

/** The tie body for `row` with `partId` — the server's proposal when present, else the same shape. */
export function buildNestTie(row: UntiedNestRow, partId: number): ProposedNestTie {
  if (row.proposed_tie) return { ...row.proposed_tie, part_id: partId };
  return {
    work_order_id: row.work_order_id,
    part_id: partId,
    work_order_operation_id: row.work_order_operation_id,
    source: 'nest',
    qty_per_run: 1,
    qty_planned: row.planned_runs || 1,
  };
}

function formatQty(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

export function TieNestSheetDialog({ row, onClose, onTied }: TieNestSheetDialogProps) {
  const [selectedPartId, setSelectedPartId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const options = useMemo(() => (row ? tieOptions(row) : []), [row]);
  const matched = row?.suggestion?.status === 'matched';

  useEffect(() => {
    setError('');
    setSaving(false);
    setSelectedPartId(matched && options.length === 1 ? options[0].part_id : null);
  }, [row, matched, options]);

  const close = () => {
    if (saving) return;
    onClose();
  };

  const submit = async () => {
    if (!row || selectedPartId == null) return;
    const chosen = options.find(option => option.part_id === selectedPartId);
    setSaving(true);
    setError('');
    try {
      await api.tieNestSheet(buildNestTie(row, selectedPartId));
      setSaving(false);
      onTied(row, chosen?.part_number ?? `Part #${selectedPartId}`);
    } catch (err) {
      setSaving(false);
      setError(apiErrorDetail(err, 'The sheet could not be tied.'));
    }
  };

  const qtyPlanned = row ? buildNestTie(row, 0).qty_planned : 0;
  const countedIds = row?.counted_since_completion_part_ids ?? [];
  const countedParts = options.filter(option => countedIds.includes(option.part_id));
  const selectedCounted = selectedPartId != null && countedIds.includes(selectedPartId);

  return (
    <Modal
      open={row !== null}
      onClose={close}
      closeOnBackdrop={!saving}
      closeOnEscape={!saving}
      size="lg"
      padded={false}
      scroll={false}
      ariaLabelledBy="tie-nest-sheet-title"
    >
      {row && (
        <>
          <div className="modal-header">
            <h3 id="tie-nest-sheet-title" className="text-lg font-semibold">
              Tie a sheet — {row.cnc_number || row.nest_name}
            </h3>
          </div>
          <div className="modal-body space-y-4">
            <p className="text-sm text-fd-body">
              {row.work_order_number}
              {hasOperationNumber(row.operation_number) && ` · ${formatOperationLabel(row.operation_number)}`} ·{' '}
              <span className="font-mono">{row.sheet_descriptor}</span>
            </p>
            <p className="text-xs text-fd-mute">
              Ties one sheet per run to this nest&apos;s operation, planned for {formatQty(qtyPlanned)}{' '}
              {qtyPlanned === 1 ? 'run' : 'runs'}, with no lot pinned (first-in, first-out when it consumes).
              {row.is_completed
                ? ' This nest has already completed: the sheet is deducted when its work order completes.'
                : ' The sheet is deducted when this nest completes.'}
            </p>

            {row.job_level_tie_count > 0 && (
              <div
                role="alert"
                className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 px-3 py-2 text-sm text-fd-amber"
              >
                This job already has {row.job_level_tie_count} whole-work-order material tie
                {row.job_level_tie_count === 1 ? '' : 's'}. If that tie is this sheet, tying the nest as well would draw
                it twice — check the job&apos;s Material Ties first.
              </div>
            )}

            {countedParts.length > 0 && (selectedPartId == null || selectedCounted) && (
              <div
                role="alert"
                data-testid="tie-nest-counted-warning"
                className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 px-3 py-2 text-sm text-fd-amber"
              >
                {countedParts.map(part => part.part_number).join(', ')} {countedParts.length === 1 ? 'was' : 'were'}{' '}
                cycle counted after this nest completed. That count already corrected on-hand for the sheets this nest
                burned; tying it now would deduct them again when the job completes. Tie it only if the count did not
                include these sheets.
              </div>
            )}

            {options.length === 0 ? (
              <p className="text-sm text-fd-mute">The sheet matcher proposed no part for this nest.</p>
            ) : (
              <fieldset>
                <legend className="label">
                  {matched ? "The sheet matcher's part" : 'Choose the sheet part (the matcher could not pick one)'}
                </legend>
                <div className="mt-1 space-y-2">
                  {options.map(option => {
                    const inputId = `tie-nest-option-${option.part_id}`;
                    return (
                      <div
                        key={option.part_id}
                        className="flex items-start gap-2 rounded-sm border border-fd-line bg-fd-panel px-3 py-2"
                      >
                        <input
                          id={inputId}
                          type="radio"
                          name="tie-nest-part"
                          className="mt-1"
                          checked={selectedPartId === option.part_id}
                          onChange={() => setSelectedPartId(option.part_id)}
                          disabled={saving}
                        />
                        <label htmlFor={inputId} className="min-w-0 text-sm text-fd-body">
                          <span className="font-mono font-semibold text-fd-ink">{option.part_number}</span>{' '}
                          {option.part_name}
                          <span className="block text-xs text-fd-mute">
                            {option.reason} · on hand{' '}
                            {option.on_hand_known
                              ? `${formatQty(option.on_hand)} ${option.unit_of_measure ?? ''}`
                              : 'unknown'}
                          </span>
                        </label>
                      </div>
                    );
                  })}
                </div>
              </fieldset>
            )}

            {error && (
              <div
                role="alert"
                data-testid="tie-nest-sheet-error"
                className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
              >
                {error}
              </div>
            )}
          </div>
          <div className="modal-footer">
            <Button variant="secondary" onClick={close} disabled={saving}>
              Cancel
            </Button>
            <LoadingButton
              loading={saving}
              loadingText="Tying…"
              disabled={selectedPartId == null}
              onClick={() => void submit()}
            >
              Tie this sheet
            </LoadingButton>
          </div>
        </>
      )}
    </Modal>
  );
}

export default TieNestSheetDialog;

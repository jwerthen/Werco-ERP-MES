/**
 * Start a cycle count for the sheet part behind a group of completed untied
 * nests — through the EXISTING `POST /inventory/cycle-counts` (one part per
 * count; ADMIN / MANAGER / SUPERVISOR server-side).
 *
 * The count is how the shop corrects sheet on-hand that left without a
 * deduction. This dialog posts NO stock adjustment and no back-dated ledger row:
 * the count itself, recorded and completed in Inventory → Cycle Counts, is what
 * moves on-hand, with its own reasons and audit.
 *
 * Non-optimistic: the dialog stays open while the call runs, a refusal is shown
 * verbatim inline, and only a success closes it.
 */

import React, { useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDate, getCentralTodayISODate } from '../../utils/centralTime';
import type { SheetCandidate, UntiedSheetGroup } from '../../types/untiedNests';

export interface StartSheetCountDialogProps {
  group: UntiedSheetGroup | null;
  onClose: () => void;
  /** Called after the count was created. */
  onStarted: (group: UntiedSheetGroup, countNumber: string | null, countId: number | null) => void;
}

/** Parts a count may be started for: the matcher's part, else its shortlist. */
export function countOptions(group: UntiedSheetGroup): SheetCandidate[] {
  const suggestion = group.suggestion;
  if (!suggestion) return [];
  if (suggestion.status === 'matched' && suggestion.candidate) return [suggestion.candidate];
  if (suggestion.status === 'ambiguous') return suggestion.candidates;
  return [];
}

function formatQty(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

export function StartSheetCountDialog({ group, onClose, onStarted }: StartSheetCountDialogProps) {
  const options = useMemo(() => (group ? countOptions(group) : []), [group]);
  const matched = group?.suggestion?.status === 'matched';
  const [partId, setPartId] = useState<number | null>(null);
  const [scheduledDate, setScheduledDate] = useState(getCentralTodayISODate());
  const [notes, setNotes] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setError('');
    setSaving(false);
    setScheduledDate(getCentralTodayISODate());
    setPartId(matched && options.length === 1 ? options[0].part_id : null);
    if (group) {
      const window =
        group.first_completed_at && group.last_completed_at
          ? ` between ${formatCentralDate(group.first_completed_at)} and ${formatCentralDate(group.last_completed_at)}`
          : '';
      setNotes(
        `Untied laser nests: ${group.nest_count} nest${group.nest_count === 1 ? '' : 's'} of ${
          group.sheet_descriptor
        } completed${window} with no sheet tied (${formatQty(group.sheets_burned)} sheets burned, not deducted).`
      );
    } else {
      setNotes('');
    }
  }, [group, matched, options]);

  const close = () => {
    if (saving) return;
    onClose();
  };

  const submit = async () => {
    if (!group || partId == null || !scheduledDate) return;
    setSaving(true);
    setError('');
    try {
      const created: { id?: number; count_number?: string } | null | undefined = await api.createCycleCount({
        part_id: partId,
        scheduled_date: scheduledDate,
        notes: notes.trim() || undefined,
      });
      setSaving(false);
      onStarted(group, created?.count_number ?? null, created?.id ?? null);
    } catch (err) {
      setSaving(false);
      setError(apiErrorDetail(err, 'The cycle count could not be started.'));
    }
  };

  return (
    <Modal
      open={group !== null}
      onClose={close}
      closeOnBackdrop={!saving}
      closeOnEscape={!saving}
      size="lg"
      padded={false}
      scroll={false}
      ariaLabelledBy="start-sheet-count-title"
    >
      {group && (
        <>
          <div className="modal-header">
            <h3 id="start-sheet-count-title" className="text-lg font-semibold">
              Start a cycle count — <span className="font-mono">{group.sheet_descriptor}</span>
            </h3>
          </div>
          <div className="modal-body space-y-4">
            <p className="text-xs text-fd-mute">
              Counts every stock row of the chosen part. Nothing is deducted here: the count is recorded and completed
              in Inventory → Cycle Counts, which is what corrects on-hand. No back-dated deduction is posted for these
              nests.
            </p>

            {options.length === 0 ? (
              <p className="text-sm text-fd-mute">
                The sheet matcher found no stock part for this spec, so there is nothing to count from here.
              </p>
            ) : (
              <fieldset>
                <legend className="label">
                  {matched ? "The sheet matcher's part" : 'Choose the part to count (the matcher could not pick one)'}
                </legend>
                <div className="mt-1 space-y-2">
                  {options.map(option => {
                    const inputId = `count-sheet-option-${option.part_id}`;
                    return (
                      <div
                        key={option.part_id}
                        className="flex items-start gap-2 rounded-sm border border-fd-line bg-fd-panel px-3 py-2"
                      >
                        <input
                          id={inputId}
                          type="radio"
                          name="count-sheet-part"
                          className="mt-1"
                          checked={partId === option.part_id}
                          onChange={() => setPartId(option.part_id)}
                          disabled={saving}
                        />
                        <label htmlFor={inputId} className="min-w-0 text-sm text-fd-body">
                          <span className="font-mono font-semibold text-fd-ink">{option.part_number}</span>{' '}
                          {option.part_name}
                          <span className="block text-xs text-fd-mute">
                            Recorded on hand{' '}
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

            <FormField label="Scheduled date" required>
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={scheduledDate}
                  onChange={e => setScheduledDate(e.target.value)}
                  disabled={saving}
                />
              )}
            </FormField>

            <FormField label="Notes" help="Saved on the count so the counter knows why it was started.">
              {field => (
                <textarea
                  {...field}
                  className="input min-h-[4.5rem]"
                  value={notes}
                  maxLength={1000}
                  onChange={e => setNotes(e.target.value)}
                  disabled={saving}
                />
              )}
            </FormField>

            {error && (
              <div
                role="alert"
                data-testid="start-sheet-count-error"
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
              loadingText="Starting…"
              disabled={partId == null || !scheduledDate}
              onClick={() => void submit()}
            >
              Start cycle count
            </LoadingButton>
          </div>
        </>
      )}
    </Modal>
  );
}

export default StartSheetCountDialog;

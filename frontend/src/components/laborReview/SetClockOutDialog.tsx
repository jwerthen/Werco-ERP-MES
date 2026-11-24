/**
 * SetClockOutDialog — close a forgotten clock, or correct a closed entry's clock-out,
 * on another person's UNAPPROVED time entry (`POST /shop-floor/time-entries/{id}/set-clock-out`).
 *
 * Shared by Labor Review and the dashboard's Live Shop Activity row. Hours only: pieces and
 * scrap are never touched. Admin / Manager / Supervisor signed in as themselves; the server
 * refuses the caller's own entry, an approved entry, an API token and a kiosk badge.
 *
 * Server-gated and therefore NON-optimistic: the dialog stays open while the request runs
 * (backdrop / Escape refused), a refusal is shown verbatim inside the dialog, and only a
 * successful response reaches `onSaved`. The time is entered as a Central wall clock and sent
 * as UTC through `centralWallClockToUtcISO`.
 *
 * Mount it with `key={`${id}:${version}`}` so each entry starts from its own default time.
 */
import React, { useId, useMemo, useState } from 'react';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import api from '../../services/api';
import { apiErrorDetail } from '../../utils/apiError';
import {
  centralWallClockToUtcISO,
  formatCentralDateTime,
  getCentralNowDateTimeLocal,
  toDate,
} from '../../utils/centralTime';
import type { HoursRederivation, SetClockOutResponse } from '../../types/laborReview';

export const SET_CLOCK_OUT_REASON_MAX = 500;

/** What the dialog needs to know about the entry being closed or corrected. */
export interface SetClockOutTarget {
  id: number;
  version: number;
  employeeName: string;
  clockIn: string;
  /** The entry's current clock-out; null when it is still clocked in. */
  clockOut: string | null;
  /** End of the shift the entry started in, when known: the default time for an open entry. */
  shiftEnd?: string | null;
  /** A short job description ("WO-1001 · Op 10 Deburr") for the dialog header. */
  jobLabel?: string | null;
}

export interface SetClockOutDialogProps {
  open: boolean;
  target: SetClockOutTarget | null;
  onClose: () => void;
  onSaved: (response: SetClockOutResponse) => void;
}

/** The dialog's default time: the current clock-out, else the shift end (if past), else now. */
export function defaultClockOutValue(target: SetClockOutTarget, now: Date = new Date()): string {
  if (target.clockOut) return getCentralNowDateTimeLocal(target.clockOut);
  const shiftEnd = toDate(target.shiftEnd ?? null);
  const clockIn = toDate(target.clockIn);
  if (shiftEnd && shiftEnd.getTime() <= now.getTime() && (!clockIn || shiftEnd.getTime() > clockIn.getTime())) {
    return getCentralNowDateTimeLocal(shiftEnd);
  }
  return getCentralNowDateTimeLocal(now);
}

const SKIPPED_HOURS_TEXT: Record<string, string> = {
  operation_hours_manual: "the operation's hours were entered by hand, so they were left alone",
  operation_hours_none: 'the operation carries no stored hours, so they were left alone',
  operation_hours_below_evidence: "the operation's stored hours do not match its time entries, so they were left alone",
  terminal_work_order: 'the work order is finished, so its stored hours were not raised',
};

/**
 * One sentence about what happened to the stored operation / work-order hours, or null when
 * there is nothing worth saying. `warn` is true when stored hours were deliberately NOT moved.
 */
export function describeHoursResult(hours: HoursRederivation | null): { text: string; warn: boolean } | null {
  if (!hours) return null;
  if (hours.applied) {
    const delta = hours.hours_delta;
    const sign = delta > 0 ? '+' : '';
    return { text: `Stored hours adjusted by ${sign}${delta.toFixed(2)} h.`, warn: false };
  }
  const reason = hours.skipped_reason ? SKIPPED_HOURS_TEXT[hours.skipped_reason] : undefined;
  if (!reason) return null;
  return { text: `Clock-out saved, but ${reason}.`, warn: true };
}

export function SetClockOutDialog({ open, target, onClose, onSaved }: SetClockOutDialogProps) {
  const titleId = useId();
  // Seeded once per mount: callers mount the dialog with `key={entry id:version}`, so a
  // dialog opened on another entry never carries the previous entry's time or reason.
  const [value, setValue] = useState(() => (target ? defaultClockOutValue(target) : ''));
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  const clockInMs = toDate(target?.clockIn ?? null)?.getTime() ?? null;
  const chosenIso = centralWallClockToUtcISO(value);
  const chosenMs = chosenIso ? new Date(chosenIso).getTime() : null;
  const trimmedReason = reason.trim();

  const timeError = useMemo(() => {
    if (!value) return 'Enter the time the person stopped.';
    if (chosenMs == null) return 'Enter a valid date and time.';
    if (clockInMs != null && chosenMs <= clockInMs) return "The clock-out must be after the entry's clock-in.";
    // The server allows two minutes of clock skew; refuse anything clearly in the future here.
    if (chosenMs > Date.now() + 2 * 60 * 1000) return 'The clock-out cannot be in the future.';
    return null;
  }, [value, chosenMs, clockInMs]);

  const hoursPreview =
    chosenMs != null && clockInMs != null && chosenMs > clockInMs ? (chosenMs - clockInMs) / 3_600_000 : null;

  const canSubmit = !!target && !timeError && trimmedReason.length > 0 && !pending;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!target || !chosenIso || !canSubmit) return;
    setPending(true);
    setError(null);
    try {
      const response = await api.setTimeEntryClockOut(target.id, {
        clock_out: chosenIso,
        reason: trimmedReason,
        version: target.version,
      });
      onSaved(response);
    } catch (err) {
      setError(apiErrorDetail(err, 'The clock-out could not be saved.'));
    } finally {
      setPending(false);
    }
  };

  const isCorrection = !!target?.clockOut;
  const title = isCorrection ? 'Correct clock-out' : 'Close clock';

  return (
    <Modal
      ariaLabelledBy={titleId}
      open={open && !!target}
      onClose={onClose}
      size="md"
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      {target && (
        <>
          <h3 id={titleId} className="text-lg font-semibold text-white">
            {title}
          </h3>
          <dl className="mt-2 grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-slate-400">Employee</dt>
            <dd className="text-slate-200">{target.employeeName}</dd>
            {target.jobLabel && (
              <>
                <dt className="text-slate-400">Job</dt>
                <dd className="text-slate-200">{target.jobLabel}</dd>
              </>
            )}
            <dt className="text-slate-400">Clocked in</dt>
            <dd className="text-slate-200 tabular-nums">{formatCentralDateTime(target.clockIn)}</dd>
            {target.clockOut && (
              <>
                <dt className="text-slate-400">Clock-out now</dt>
                <dd className="text-slate-200 tabular-nums">{formatCentralDateTime(target.clockOut)}</dd>
              </>
            )}
          </dl>
          <p className="mt-2 text-xs text-slate-400">
            Changes hours only. Pieces and scrap on this entry are not touched. The change and your reason are recorded
            in the audit log.
          </p>
          <form onSubmit={handleSubmit} className="mt-4 space-y-4" noValidate>
            <FormField
              label="Clock-out (Central time)"
              required
              error={touched || value ? timeError : null}
              help={hoursPreview != null ? `Entry length: ${hoursPreview.toFixed(2)} h` : undefined}
            >
              {field => (
                <input
                  {...field}
                  type="datetime-local"
                  required
                  className="input"
                  value={value}
                  max={getCentralNowDateTimeLocal()}
                  disabled={pending}
                  onChange={event => setValue(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              label="Reason"
              required
              error={touched && !trimmedReason ? 'A reason is required.' : null}
              help={`${reason.length}/${SET_CLOCK_OUT_REASON_MAX}`}
            >
              {field => (
                <textarea
                  {...field}
                  required
                  rows={3}
                  maxLength={SET_CLOCK_OUT_REASON_MAX}
                  className="input"
                  value={reason}
                  placeholder="e.g. Forgot to clock out; left at end of shift per supervisor."
                  disabled={pending}
                  onChange={event => setReason(event.target.value)}
                />
              )}
            </FormField>
            {error && (
              <p
                role="alert"
                className="rounded-sm border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-sm text-red-200"
              >
                {error}
              </p>
            )}
            <div className="flex justify-end gap-3">
              <Button variant="secondary" onClick={onClose} disabled={pending}>
                Cancel
              </Button>
              <LoadingButton type="submit" loading={pending} disabled={!canSubmit}>
                {isCorrection ? 'Save clock-out' : 'Close clock'}
              </LoadingButton>
            </div>
          </form>
        </>
      )}
    </Modal>
  );
}

export default SetClockOutDialog;

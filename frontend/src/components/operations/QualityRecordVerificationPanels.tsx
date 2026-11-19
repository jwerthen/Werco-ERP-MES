/**
 * The two server-stamped verification records on the quality-record detail:
 * - an NCR's re-verification of reworked or repaired product (AS9100D 8.7.1),
 *   POST /quality/ncr/{id}/reverification;
 * - a CAR's effectiveness verification (AS9100D 10.2.1 d), POST /quality/car/{id}/effectiveness.
 *
 * Each panel shows what is recorded (result, text, who, when) and, for a caller allowed to
 * record it (Admin / Manager / Quality, the quality:approve holders), opens the record dialog.
 * Both verbs are server-gated (role, record state, API token) and server-stamped, so they are
 * non-optimistic: nothing changes on screen until the server answers, and its saved record is
 * handed to `onRecorded`. A refusal stays in the dialog in the server's own words.
 */
import React, { useState } from 'react';
import api from '../../services/api';
import { Button } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDate, formatCentralDateTime } from '../../utils/centralTime';
import QualityRecordResultDialog from './QualityRecordResultDialog';
import {
  CAR_EFFECTIVENESS_RESULTS,
  NCR_REVERIFICATION_RESULTS,
  NCR_REVERIFIED_DISPOSITIONS,
  carEffectivenessResultLabel,
  ncrReverificationResultLabel,
  type CAREffectivenessResult,
  type CARRecord,
  type NCRRecord,
  type NCRReverificationResult,
} from '../../types/quality';

/** "{verb} by {name} on {Central date-time}", or "Not recorded" when the stamp is empty. */
export function stampText(
  verb: string,
  at: string | null | undefined,
  name: string | null | undefined,
  userId: number | null | undefined
): string {
  if (!at) return 'Not recorded';
  const who = name || (userId ? `user #${userId}` : 'an unknown user');
  return `${verb} by ${who} on ${formatCentralDateTime(at)}`;
}

const SAVE_FIRST = 'Save or discard your edits before recording this';
const panelClass = 'border border-fd-line bg-slate-900/40 px-3 py-2 text-sm';
const headingClass = 'text-xs font-semibold uppercase tracking-wider text-slate-400';

interface PanelProps<R> {
  record: R;
  /** The caller holds the write role (and the record is not VOID). */
  canWrite: boolean;
  /** Unsaved edits in the record form: recording replaces the form with the saved record. */
  dirty: boolean;
  /** The record form is saving or another action is in flight. */
  busy: boolean;
  onRecorded: (saved: R, message: string) => void;
}

export function NcrReverificationPanel({ record, canWrite, dirty, busy, onRecorded }: PanelProps<NCRRecord>) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  // The server judges the STORED disposition, so the panel does too; an unsaved change to
  // the disposition select makes the form dirty, which disables the action anyway.
  const applies = !!record.disposition && NCR_REVERIFIED_DISPOSITIONS.includes(record.disposition);
  const recorded = !!record.reverification_result || !!record.reverified_at;
  const canRecord = canWrite && applies && record.status !== 'closed' && record.status !== 'void';
  if (!applies && !recorded) return null;

  const submit = async ({ result, text }: { result: NCRReverificationResult; text: string }) => {
    if (pending) return;
    setPending(true);
    setError('');
    try {
      const saved = await api.recordNcrReverification(record.id, { result, notes: text });
      setOpen(false);
      onRecorded(
        saved,
        `Re-verification recorded: ${ncrReverificationResultLabel(saved.reverification_result).toLowerCase() || result}.`
      );
    } catch (err) {
      setError(apiErrorDetail(err, 'Unable to record the re-verification'));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className={panelClass} aria-label="Re-verification" data-testid="ncr-reverification">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className={headingClass}>Re-verification</p>
          {recorded ? (
            <>
              <p className="text-slate-100">
                Result: {ncrReverificationResultLabel(record.reverification_result) || '—'}
              </p>
              {record.reverification_notes && (
                <p className="whitespace-pre-wrap break-words text-slate-300">{record.reverification_notes}</p>
              )}
              <p className="text-xs text-slate-400">
                {stampText('Re-verified', record.reverified_at, record.reverified_by_name, record.reverified_by)}
              </p>
            </>
          ) : (
            <p className="text-slate-300">
              Not recorded. A conforming re-verification is required to close a rework or repair disposition.
            </p>
          )}
        </div>
        {canRecord && (
          <Button
            variant="secondary"
            size="sm"
            disabled={dirty || busy || pending}
            title={dirty ? SAVE_FIRST : undefined}
            onClick={() => {
              setError('');
              setOpen(true);
            }}
          >
            Record re-verification
          </Button>
        )}
      </div>
      <QualityRecordResultDialog
        open={open}
        title="Record re-verification"
        message={`Records the re-verification of ${record.ncr_number}'s corrected product, with you as the verifier, dated now. A new record replaces the last one.`}
        resultLabel="Result"
        options={NCR_REVERIFICATION_RESULTS}
        textLabel="Re-verification notes"
        textHelp="What was inspected or tested, and against which requirement."
        submitLabel="Record"
        pending={pending}
        error={error}
        onSubmit={value => void submit(value)}
        onCancel={() => {
          if (!pending) setOpen(false);
        }}
      />
    </section>
  );
}

export function CarEffectivenessPanel({ record, canWrite, dirty, busy, onRecorded }: PanelProps<CARRecord>) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const recorded = !!record.effectiveness_result || !!record.effectiveness_verified_at;
  const inVerification = record.status === 'verification';
  const canRecord = canWrite && inVerification;
  // NULL car_type means corrective; only corrective CARs need an effective result to close.
  const corrective = !record.car_type || record.car_type === 'corrective';

  const submit = async ({ result, text }: { result: CAREffectivenessResult; text: string }) => {
    if (pending) return;
    setPending(true);
    setError('');
    try {
      const saved = await api.recordCarEffectiveness(record.id, { result, evidence: text });
      setOpen(false);
      const resultText = carEffectivenessResultLabel(saved.effectiveness_result).toLowerCase() || result;
      // Report what the server did, not what was asked: the status change is its decision.
      const returned =
        record.status !== saved.status && saved.status === 'corrective_action'
          ? ' The CAR is back in Corrective Action.'
          : '';
      onRecorded(saved, `Effectiveness verification recorded: ${resultText}.${returned}`);
    } catch (err) {
      setError(apiErrorDetail(err, 'Unable to record the effectiveness verification'));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className={panelClass} aria-label="Effectiveness verification" data-testid="car-effectiveness">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className={headingClass}>Effectiveness verification</p>
          {recorded ? (
            <>
              <p className="text-slate-100">
                Result: {carEffectivenessResultLabel(record.effectiveness_result) || '—'}
              </p>
              {record.effectiveness_check && (
                <p className="whitespace-pre-wrap break-words text-slate-300">
                  Evidence reviewed: {record.effectiveness_check}
                </p>
              )}
              <p className="text-xs text-slate-400">
                {stampText(
                  'Verified',
                  record.effectiveness_verified_at,
                  record.effectiveness_verified_by_name,
                  record.effectiveness_verified_by
                )}
              </p>
            </>
          ) : (
            <p className="text-slate-300">
              Not recorded.
              {record.effectiveness_due ? ` Due ${formatCentralDate(record.effectiveness_due)}.` : ''}
              {corrective ? ' An effective verification is required to close a corrective CAR.' : ''}
              {!inVerification && record.status !== 'closed' && record.status !== 'void'
                ? ' It is recorded once the CAR is in Verification.'
                : ''}
            </p>
          )}
        </div>
        {canRecord && (
          <Button
            variant="secondary"
            size="sm"
            disabled={dirty || busy || pending}
            title={dirty ? SAVE_FIRST : undefined}
            onClick={() => {
              setError('');
              setOpen(true);
            }}
          >
            Record effectiveness
          </Button>
        )}
      </div>
      <QualityRecordResultDialog
        open={open}
        title="Record effectiveness verification"
        message={`Records whether ${record.car_number}'s corrective action was effective, with you as the verifier, dated now. Not effective returns the CAR to Corrective Action.`}
        resultLabel="Result"
        options={CAR_EFFECTIVENESS_RESULTS}
        textLabel="Evidence reviewed"
        textHelp="The records, data or follow-up checks that show whether the action worked."
        // Pre-fill only a draft typed through the form; a recorded verification's evidence
        // belongs to that verification, so a new one starts blank.
        defaultText={record.effectiveness_result ? '' : record.effectiveness_check || ''}
        submitLabel="Record"
        pending={pending}
        error={error}
        onSubmit={value => void submit(value)}
        onCancel={() => {
          if (!pending) setOpen(false);
        }}
      />
    </section>
  );
}

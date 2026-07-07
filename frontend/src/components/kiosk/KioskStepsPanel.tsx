import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeftCircleIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  LockClosedIcon,
} from '@heroicons/react/24/solid';
import { Modal } from '../ui/Modal';
import { formatCentralDateTime } from '../../utils/centralTime';
import {
  extractErrorStatus,
  extractOutOfTolerance,
  OutOfToleranceInfo,
} from '../../utils/processSheetErrors';
import type {
  MissingStepInfo,
  OperationStepRecord,
  OperationStepRecordInput,
  OperationStepSupersedeInput,
  OperationStepsView,
  OperationStepWithState,
  StepAttachmentResult,
} from '../../types/processSheet';
import KioskPhotoInput from './KioskPhotoInput';
import { kioskErrorMessage } from './kioskConstants';

/**
 * Transport seam so BOTH kiosks share one steps view:
 *  - OperatorKiosk binds the global api client (logged-in operator session).
 *  - CrewStationKiosk binds kioskStationClient with the badge-minted operator
 *    token, so every record is attributed to the badge identity.
 * Uploads MUST go through the in-fence step attachment endpoint — never
 * /documents/upload (403 for kiosk-scoped tokens).
 */
export interface StepsTransport {
  fetchView(operationId: number): Promise<OperationStepsView>;
  createRecord(operationId: number, stepId: number, data: OperationStepRecordInput): Promise<unknown>;
  supersedeRecord(
    operationId: number,
    stepId: number,
    recordId: number,
    data: OperationStepSupersedeInput
  ): Promise<unknown>;
  uploadAttachment(operationId: number, stepId: number, file: File): Promise<StepAttachmentResult>;
}

interface KioskStepsPanelProps {
  operationId: number;
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  transport: StepsTransport;
  /** Host mutationsBlocked (busy || offline) — hard-disables every write. */
  blocked: boolean;
  online: boolean;
  /** id of the host offline banner for aria-describedby, when offline. */
  offlineHintId?: string;
  /** Crew station: the badge-identified operator whose name records carry. */
  recordingAs?: string | null;
  /** From a 409 STEPS_INCOMPLETE completion refusal — rendered inline with jump-to-step. */
  missing?: MissingStepInfo[] | null;
  showToast: (type: 'success' | 'error' | 'info', message: string) => void;
  onBack: () => void;
  /** After every successful write — hosts refresh their queue (chip counts). */
  onRecorded?: () => void | Promise<void>;
  /** Report in-flight writes so the host busy guard (idle reset, lock/logout) covers them. */
  onBusyChange?: (busy: boolean) => void;
  /** Crew station: a 401 means the 5-minute badge token expired — host returns to the scan screen. */
  onAuthExpired?: (message: string) => void;
}

const BADGE_EXPIRED_MESSAGE = 'Badge session expired — scan your badge again to keep recording.';

/** Instrument-panel tone per step type (kiosk-sized twin of the desktop TYPE_BADGE). */
const STEP_TYPE_TONE: Record<string, string> = {
  measurement: 'border-fd-blue/50 text-fd-blue',
  checkbox: 'border-fd-green/50 text-fd-green',
  list: 'border-fd-cyan/50 text-fd-cyan',
  value: 'border-fd-blue/50 text-fd-blue',
  photo: 'border-fd-amber/50 text-fd-amber',
  file: 'border-fd-line-bright text-fd-body',
  instruction: 'border-fd-line text-fd-mute',
};

function StepTypeChip({ type }: { type: string }) {
  return (
    <span
      className={`rounded border px-2 py-0.5 font-mono text-xs font-semibold uppercase tracking-widest ${
        STEP_TYPE_TONE[type] || 'border-fd-line text-fd-mute'
      }`}
    >
      {type}
    </span>
  );
}

/** Round like the server does before its tolerance check (config.decimals). */
function roundLikeServer(value: number, decimals: number | undefined): number {
  if (typeof decimals === 'number' && Number.isInteger(decimals) && decimals >= 0) {
    const factor = 10 ** decimals;
    return Math.round(value * factor) / factor;
  }
  return value;
}

/** "LSL 0.4980 · NOM 0.5000 · USL 0.5020 in" — the measurement limits line. */
function measurementLimits(step: OperationStepWithState): string | null {
  const config = step.config;
  if (step.step_type !== 'measurement' || !config) return null;
  const parts: string[] = [];
  if (config.lsl != null) parts.push(`LSL ${config.lsl}`);
  if (config.nominal != null) parts.push(`NOM ${config.nominal}`);
  if (config.usl != null) parts.push(`USL ${config.usl}`);
  if (parts.length === 0) return null;
  return `${parts.join(' · ')}${config.unit ? ` ${config.unit}` : ''}`;
}

export function formatRecordValue(step: OperationStepWithState, record: OperationStepRecord): string {
  switch (step.step_type) {
    case 'measurement': {
      if (record.value_numeric == null) return '—';
      const unit = step.config?.unit;
      return `${record.value_numeric}${unit ? ` ${unit}` : ''}`;
    }
    case 'checkbox':
      return record.value_bool ? 'Done' : 'Not done';
    case 'photo':
      return 'Photo attached';
    case 'file':
      return 'File attached';
    default:
      return record.value_text ?? '—';
  }
}

// ---------------------------------------------------------------------------
// Draft value state — one active step at a time, reset on step/serial change.
// ---------------------------------------------------------------------------

interface DraftValue {
  numeric: string;
  text: string;
  option: string | null;
  file: File | null;
}

const EMPTY_DRAFT: DraftValue = { numeric: '', text: '', option: null, file: null };

/**
 * Type-shaped payload from the current draft, or null while invalid/incomplete.
 * PHOTO/FILE return an empty payload — the caller uploads the draft file first
 * and fills attachment_document_id from the response.
 */
function draftPayload(step: OperationStepWithState, draft: DraftValue): OperationStepRecordInput | null {
  switch (step.step_type) {
    case 'measurement': {
      const parsed = Number(draft.numeric);
      if (draft.numeric.trim() === '' || !Number.isFinite(parsed)) return null;
      return { value_numeric: parsed };
    }
    case 'checkbox':
      // The kiosk only ever records the affirmative — an unchecked box is
      // simply not recorded (an honest "not done" can arrive from elsewhere
      // and renders as unsatisfied evidence).
      return { value_bool: true };
    case 'list':
      return draft.option ? { value_text: draft.option } : null;
    case 'value':
      return draft.text.trim() ? { value_text: draft.text.trim() } : null;
    case 'photo':
    case 'file':
      return draft.file ? {} : null;
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Shared value editor (record + supersede paths).
// ---------------------------------------------------------------------------

interface StepValueEditorProps {
  step: OperationStepWithState;
  draft: DraftValue;
  onDraftChange: (draft: DraftValue) => void;
  disabled: boolean;
  idPrefix: string;
}

function StepValueEditor({ step, draft, onDraftChange, disabled, idPrefix }: StepValueEditorProps) {
  const config = step.config;

  if (step.step_type === 'measurement') {
    const limits = measurementLimits(step);
    const parsed = Number(draft.numeric);
    const hasValue = draft.numeric.trim() !== '' && Number.isFinite(parsed);
    const lsl = config?.lsl;
    const usl = config?.usl;
    const canPreview = hasValue && typeof lsl === 'number' && typeof usl === 'number';
    const rounded = canPreview ? roundLikeServer(parsed, config?.decimals) : null;
    const inTolerance = canPreview && rounded != null && rounded >= (lsl as number) && rounded <= (usl as number);
    const inputId = `${idPrefix}-measurement`;
    return (
      <div>
        {limits && <p className="font-mono text-lg text-fd-body">{limits}</p>}
        <label
          htmlFor={inputId}
          className="mt-3 block font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute"
        >
          Measured value{config?.unit ? ` (${config.unit})` : ''}
        </label>
        <input
          id={inputId}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={draft.numeric}
          onChange={(e) => onDraftChange({ ...draft, numeric: e.target.value })}
          disabled={disabled}
          className="mt-2 w-full rounded border border-fd-line-bright bg-fd-sunken px-4 py-4 font-mono text-3xl font-bold text-fd-ink focus:border-fd-blue focus:outline-none disabled:opacity-40"
        />
        {canPreview && (
          <p
            data-testid={`${idPrefix}-tolerance-preview`}
            className={`mt-2 text-lg font-bold ${inTolerance ? 'text-fd-green' : 'text-fd-red'}`}
          >
            {inTolerance
              ? `Within limits (${lsl} – ${usl})`
              : `Outside limits (${lsl} – ${usl}) — the server will refuse this value`}
            <span className="block text-sm font-semibold text-fd-mute">
              Preview only — the server verdict is final.
            </span>
          </p>
        )}
      </div>
    );
  }

  if (step.step_type === 'list') {
    const options = (config?.options ?? []).map(String);
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2" role="group" aria-label={`${step.label} options`}>
        {options.map((option) => {
          const selected = draft.option === option;
          return (
            <button
              key={option}
              type="button"
              aria-pressed={selected}
              disabled={disabled}
              onClick={() => onDraftChange({ ...draft, option })}
              className={`min-h-16 rounded border px-4 text-xl font-bold tracking-wide transition-colors disabled:opacity-40 ${
                selected
                  ? 'border-fd-blue bg-fd-blue/20 text-fd-blue'
                  : 'border-fd-line bg-fd-sunken text-fd-body hover:border-fd-line-bright'
              }`}
            >
              {option}
            </button>
          );
        })}
        {options.length === 0 && <p className="text-lg text-fd-mute">No options configured for this step.</p>}
      </div>
    );
  }

  if (step.step_type === 'value') {
    const inputId = `${idPrefix}-value`;
    return (
      <div>
        <label
          htmlFor={inputId}
          className="block font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute"
        >
          Recorded value
        </label>
        <input
          id={inputId}
          type="text"
          autoComplete="off"
          value={draft.text}
          onChange={(e) => onDraftChange({ ...draft, text: e.target.value })}
          disabled={disabled}
          className="mt-2 w-full rounded border border-fd-line-bright bg-fd-sunken px-4 py-4 font-mono text-2xl font-bold text-fd-ink focus:border-fd-blue focus:outline-none disabled:opacity-40"
        />
      </div>
    );
  }

  if (step.step_type === 'photo' || step.step_type === 'file') {
    return (
      <KioskPhotoInput
        stepType={step.step_type}
        value={draft.file}
        onChange={(file) => onDraftChange({ ...draft, file })}
        disabled={disabled}
        idPrefix={idPrefix}
        hint={config?.hint ?? null}
      />
    );
  }

  if (step.step_type === 'checkbox') {
    return (
      <p className="text-lg text-fd-body">
        Recording marks this step <span className="font-bold text-fd-green">Done</span>.
      </p>
    );
  }

  return null;
}

// ---------------------------------------------------------------------------
// Supersede (correction) modal — reason required + the replacement value.
// ---------------------------------------------------------------------------

interface KioskSupersedeModalProps {
  step: OperationStepWithState;
  record: OperationStepRecord;
  serialized: boolean;
  blocked: boolean;
  onCancel: () => void;
  /** Throws on server refusal — the modal renders the message and stays open. */
  onSubmit: (reason: string, values: Omit<OperationStepSupersedeInput, 'reason'>, file: File | null) => Promise<void>;
}

function KioskSupersedeModal({ step, record, serialized, blocked, onCancel, onSubmit }: KioskSupersedeModalProps) {
  const [reason, setReason] = useState('');
  const [draft, setDraft] = useState<DraftValue>(EMPTY_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const payload = draftPayload(step, draft);
  const isAttachment = step.step_type === 'photo' || step.step_type === 'file';
  const valid = reason.trim().length > 0 && payload != null;

  const handleSave = async () => {
    if (!valid || saving || blocked || !payload) return;
    setSaving(true);
    setError(null);
    try {
      await onSubmit(reason.trim(), payload, isAttachment ? draft.file : null);
    } catch (err) {
      const oot = extractOutOfTolerance(err);
      setError(
        oot
          ? `${oot.message} — the correction was NOT saved.`
          : kioskErrorMessage(err, 'Could not save the correction. Try again.')
      );
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onCancel} size="lg" closeOnBackdrop={false} ariaLabelledBy="kiosk-supersede-title">
      <h2 id="kiosk-supersede-title" className="text-3xl font-bold text-fd-ink">
        Correct record
      </h2>
      <p className="mt-1 text-xl text-fd-body">
        {step.label}
        {serialized && record.serial_number ? (
          <span className="font-mono text-fd-mute"> · {record.serial_number}</span>
        ) : null}
      </p>
      <p className="mt-3 rounded border border-fd-line bg-fd-sunken px-4 py-3 font-mono text-lg text-fd-body">
        Current: {formatRecordValue(step, record)} · {record.recorded_by_name || 'Operator'} ·{' '}
        {formatCentralDateTime(record.recorded_at)}
      </p>
      <p className="mt-2 text-base text-fd-mute">
        Corrections never erase evidence — the original stays on file, marked as superseded.
      </p>

      <label
        htmlFor="kiosk-supersede-reason"
        className="mt-5 block font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-amber"
      >
        Reason for correction — required
      </label>
      <input
        id="kiosk-supersede-reason"
        type="text"
        autoComplete="off"
        maxLength={255}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        disabled={saving || blocked}
        className="mt-2 w-full rounded border border-fd-line-bright bg-fd-sunken px-4 py-4 text-xl text-fd-ink focus:border-fd-amber focus:outline-none disabled:opacity-40"
      />

      <div className="mt-5">
        <StepValueEditor
          step={step}
          draft={draft}
          onDraftChange={setDraft}
          disabled={saving || blocked}
          idPrefix="kiosk-supersede"
        />
      </div>

      {error && (
        <div
          role="alert"
          className="mt-4 rounded border border-fd-red bg-fd-red/10 px-4 py-3 text-xl font-semibold text-fd-red"
        >
          {error}
        </div>
      )}

      <div className="mt-6 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="min-h-16 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="kiosk-supersede-save"
          onClick={() => void handleSave()}
          disabled={saving || blocked || !valid}
          className="min-h-16 rounded border border-fd-amber bg-fd-amber/15 text-xl font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {saving ? 'Saving…' : 'Save correction'}
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// The steps panel itself.
// ---------------------------------------------------------------------------

/**
 * Shared kiosk steps view (Process Sheets capture). Server-gated and therefore
 * NON-optimistic throughout: writes show an in-flight state, the view refetches
 * after every success (no websocket for records), and refusals surface the
 * server's detail — the OUT_OF_TOLERANCE 409 renders as an inline danger strip
 * (no record was written), everything else as a verbatim error toast.
 * Always readable regardless of operation state; inputs appear only while the
 * operation is IN_PROGRESS and the station is online.
 */
export default function KioskStepsPanel({
  operationId,
  jobLabel,
  transport,
  blocked,
  online,
  offlineHintId,
  recordingAs,
  missing,
  showToast,
  onBack,
  onRecorded,
  onBusyChange,
  onAuthExpired,
}: KioskStepsPanelProps) {
  // Transport + callbacks in refs: identities may churn per host render, but
  // the load effect must fire on operationId changes only.
  const transportRef = useRef(transport);
  transportRef.current = transport;
  const onAuthExpiredRef = useRef(onAuthExpired);
  onAuthExpiredRef.current = onAuthExpired;

  const [view, setView] = useState<OperationStepsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Stale-response guard (the CrewStationKiosk generationRef pattern): every
  // (re)fetch bumps the generation; a response minted under an older one is
  // discarded so it can never overwrite fresher post-record state.
  const generationRef = useRef(0);

  // undefined = no explicit choice yet (fall back to the derived default);
  // null = deliberately collapsed / no serial.
  const [selectedSerial, setSelectedSerial] = useState<string | null | undefined>(undefined);
  const [expandedStepId, setExpandedStepId] = useState<number | null | undefined>(undefined);
  const [draft, setDraft] = useState<DraftValue>(EMPTY_DRAFT);
  const [oot, setOot] = useState<{ stepId: number; serial: string | null; info: OutOfToleranceInfo } | null>(null);
  const [pending, setPending] = useState(false);
  const [supersedeTarget, setSupersedeTarget] = useState<{
    step: OperationStepWithState;
    record: OperationStepRecord;
  } | null>(null);

  const setBusy = useCallback(
    (busy: boolean) => {
      setPending(busy);
      onBusyChange?.(busy);
    },
    [onBusyChange]
  );

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const res = await transportRef.current.fetchView(operationId);
      if (generation !== generationRef.current) return;
      setView(res);
      setLoadError(null);
      setLoading(false);
    } catch (err) {
      if (generation !== generationRef.current) return;
      setLoading(false);
      if (extractErrorStatus(err) === 401 && onAuthExpiredRef.current) {
        onAuthExpiredRef.current(BADGE_EXPIRED_MESSAGE);
        return;
      }
      setLoadError(kioskErrorMessage(err, 'Could not load process steps.'));
    }
  }, [operationId]);

  useEffect(() => {
    setLoading(true);
    setView(null);
    setLoadError(null);
    setExpandedStepId(undefined);
    setSelectedSerial(undefined);
    void load();
  }, [load]);

  const serialized = Boolean(view?.is_serialized);
  const recordable = view?.operation_status === 'in_progress';
  const inputsDisabled = blocked || pending;

  // The first outstanding missing step from a completion refusal, else the
  // first incomplete required step — DERIVED, so the view auto-advances to the
  // next open step after each successful record (until the operator chooses).
  const firstMissing = view
    ? (missing ?? []).find((m) => view.steps.some((s) => s.id === m.step_id && !s.complete))
    : undefined;
  const autoExpandStepId = view
    ? (firstMissing?.step_id ??
      view.steps.find((s) => s.is_required && s.step_type !== 'instruction' && !s.complete)?.id ??
      null)
    : null;
  const effectiveExpandedId = expandedStepId === undefined ? autoExpandStepId : expandedStepId;

  // Default serial: the refusal's first outstanding serial, else the WO's first.
  const autoSerial = firstMissing?.serials[0] ?? view?.serial_numbers[0] ?? null;
  const effectiveSerial = !serialized
    ? null
    : selectedSerial !== undefined && selectedSerial !== null && view?.serial_numbers.includes(selectedSerial)
      ? selectedSerial
      : autoSerial;

  // Fresh inputs (and a cleared refusal strip) per step/serial slot.
  useEffect(() => {
    setDraft(EMPTY_DRAFT);
    setOot(null);
  }, [effectiveExpandedId, effectiveSerial]);

  const recordsForSlot = useCallback(
    (step: OperationStepWithState): OperationStepRecord[] =>
      serialized ? step.records.filter((r) => r.serial_number === effectiveSerial) : step.records,
    [serialized, effectiveSerial]
  );

  const slotSatisfied = useCallback(
    (step: OperationStepWithState): boolean => {
      if (!view) return false;
      if (serialized) {
        return Boolean(effectiveSerial && view.completeness[String(step.id)]?.[effectiveSerial]);
      }
      return step.complete;
    },
    [view, serialized, effectiveSerial]
  );

  const gatingSteps = (view?.steps ?? []).filter((s) => s.is_required && s.step_type !== 'instruction');
  const serialComplete = (serial: string): boolean =>
    gatingSteps.length > 0 && gatingSteps.every((s) => Boolean(view?.completeness[String(s.id)]?.[serial]));

  // A completion refusal stays visible only while its steps are outstanding.
  const visibleMissing = (missing ?? []).filter((m) => {
    const step = view?.steps.find((s) => s.id === m.step_id);
    return step ? !step.complete : false;
  });

  const jumpToStep = (m: MissingStepInfo) => {
    setExpandedStepId(m.step_id);
    if (serialized && m.serials.length > 0) setSelectedSerial(m.serials[0]);
    const el = document.getElementById(`kiosk-step-${m.step_id}`);
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
  };

  const finishWrite = async (successMessage: string) => {
    showToast('success', successMessage);
    setDraft(EMPTY_DRAFT);
    setOot(null);
    await load();
    if (onRecorded) await onRecorded();
  };

  const handleWriteError = (err: unknown, step: OperationStepWithState, fallback: string) => {
    const ootInfo = extractOutOfTolerance(err);
    if (ootInfo) {
      setOot({ stepId: step.id, serial: serialized ? effectiveSerial : null, info: ootInfo });
      return;
    }
    if (extractErrorStatus(err) === 401 && onAuthExpiredRef.current) {
      onAuthExpiredRef.current(BADGE_EXPIRED_MESSAGE);
      return;
    }
    showToast('error', kioskErrorMessage(err, fallback));
  };

  const submitRecord = async (step: OperationStepWithState) => {
    const payload = draftPayload(step, draft);
    if (!payload || inputsDisabled || !recordable) return;
    setOot(null);
    setBusy(true);
    try {
      const body: OperationStepRecordInput = { ...payload };
      if (serialized && effectiveSerial) body.serial_number = effectiveSerial;
      if (step.step_type === 'photo' || step.step_type === 'file') {
        if (!draft.file) return;
        const uploaded = await transportRef.current.uploadAttachment(operationId, step.id, draft.file);
        body.attachment_document_id = uploaded.document_id;
      }
      await transportRef.current.createRecord(operationId, step.id, body);
      await finishWrite(`Recorded — ${step.label}`);
    } catch (err) {
      handleWriteError(err, step, 'Could not record this step. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const submitSupersede = async (
    reason: string,
    values: Omit<OperationStepSupersedeInput, 'reason'>,
    file: File | null
  ) => {
    if (!supersedeTarget) return;
    const { step, record } = supersedeTarget;
    setBusy(true);
    try {
      const body: OperationStepSupersedeInput = { reason, ...values };
      if (step.step_type === 'photo' || step.step_type === 'file') {
        if (!file) return;
        const uploaded = await transportRef.current.uploadAttachment(operationId, step.id, file);
        body.attachment_document_id = uploaded.document_id;
      }
      await transportRef.current.supersedeRecord(operationId, step.id, record.id, body);
      setSupersedeTarget(null);
      await finishWrite(`Corrected — ${step.label}`);
    } catch (err) {
      if (extractErrorStatus(err) === 401 && onAuthExpiredRef.current) {
        setSupersedeTarget(null);
        onAuthExpiredRef.current(BADGE_EXPIRED_MESSAGE);
        return;
      }
      // The modal renders the refusal (incl. out-of-tolerance) and stays open.
      throw err;
    } finally {
      setBusy(false);
    }
  };

  // --- Loading / load-error shells -------------------------------------------
  if (loading && !view) {
    return (
      <section aria-label="Process steps" className="mx-auto w-full max-w-3xl">
        <p className="py-10 text-center text-xl text-fd-mute">Loading process steps…</p>
      </section>
    );
  }

  if (!view) {
    return (
      <section aria-label="Process steps" className="mx-auto w-full max-w-3xl">
        <div role="alert" className="rounded border border-fd-red bg-fd-red/10 px-5 py-6 text-center">
          <p className="text-xl font-semibold text-fd-red">{loadError || 'Could not load process steps.'}</p>
        </div>
        <div className="mt-5 grid grid-cols-2 gap-3">
          <button
            type="button"
            onClick={onBack}
            className="flex min-h-16 items-center justify-center gap-2 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright"
          >
            <ArrowLeftCircleIcon className="h-7 w-7" aria-hidden="true" />
            Back
          </button>
          <button
            type="button"
            onClick={() => {
              setLoading(true);
              void load();
            }}
            className="flex min-h-16 items-center justify-center gap-2 rounded border border-fd-blue bg-fd-blue/15 text-xl font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25"
          >
            <ArrowPathIcon className="h-7 w-7" aria-hidden="true" />
            Retry
          </button>
        </div>
      </section>
    );
  }

  const progressPct = view.steps_total > 0 ? Math.round((view.steps_recorded / view.steps_total) * 100) : 0;

  return (
    <section aria-label="Process steps" className="mx-auto w-full max-w-3xl">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-3xl font-bold text-fd-ink">Process steps</h2>
          <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>
          {recordingAs && (
            <p className="mt-1 text-base font-semibold text-fd-blue">Recording as {recordingAs}</p>
          )}
        </div>
        <div className="text-right">
          <p data-testid="kiosk-steps-progress" className="font-mono text-3xl font-bold tabular-nums text-fd-ink">
            {view.steps_recorded}/{view.steps_total}
          </p>
          <p className="mt-1 text-sm uppercase tracking-widest text-fd-faint">required recorded</p>
        </div>
      </div>

      {view.steps_total > 0 && (
        <div
          role="progressbar"
          aria-label="Required steps recorded"
          aria-valuemin={0}
          aria-valuemax={view.steps_total}
          aria-valuenow={view.steps_recorded}
          className="mt-3 h-2 w-full overflow-hidden rounded bg-fd-sunken"
        >
          <div
            className={`h-full rounded ${progressPct >= 100 ? 'bg-fd-green' : 'bg-fd-blue'}`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
      )}

      {!recordable && (
        <p
          data-testid="kiosk-steps-readonly"
          className="mt-4 rounded border border-fd-amber/50 bg-fd-amber/10 px-4 py-3 text-center text-lg font-semibold text-fd-amber"
        >
          Read-only — records can be added while the job is running
          {view.operation_status ? ` (this operation is ${view.operation_status.replace(/_/g, ' ')})` : ''}.
        </p>
      )}

      {visibleMissing.length > 0 && (
        <div role="alert" data-testid="kiosk-steps-missing" className="mt-4 rounded border-2 border-fd-red bg-fd-red/10 p-4">
          <p className="text-xl font-bold text-fd-red">Cannot complete — required steps are missing records:</p>
          <ul className="mt-3 space-y-2">
            {visibleMissing.map((m) => (
              <li key={m.step_id} className="flex flex-wrap items-center justify-between gap-3">
                <span className="text-lg text-fd-ink">
                  {m.label}
                  {m.serials.length > 0 && (
                    <span className="font-mono text-fd-mute"> — {m.serials.join(', ')}</span>
                  )}
                </span>
                <button
                  type="button"
                  onClick={() => jumpToStep(m)}
                  className="min-h-12 rounded border border-fd-red/60 bg-fd-red/10 px-4 text-base font-bold uppercase tracking-wide text-fd-red transition-colors hover:bg-fd-red/20"
                >
                  Go to step
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {serialized && view.serial_numbers.length > 0 && (
        <div className="mt-5">
          <p className="mb-2 font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute">
            Serial number — steps are recorded per unit
          </p>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Serial number">
            {view.serial_numbers.map((serial) => {
              const active = serial === effectiveSerial;
              const done = serialComplete(serial);
              return (
                <button
                  key={serial}
                  type="button"
                  aria-pressed={active}
                  data-testid={`kiosk-serial-${serial}`}
                  onClick={() => setSelectedSerial(serial)}
                  className={`flex min-h-14 items-center gap-2 rounded border px-4 font-mono text-lg font-bold transition-colors ${
                    active
                      ? 'border-fd-blue bg-fd-blue/20 text-fd-blue'
                      : 'border-fd-line bg-fd-sunken text-fd-body hover:border-fd-line-bright'
                  }`}
                >
                  {serial}
                  {done && <CheckCircleIcon className="h-5 w-5 text-fd-green" aria-hidden="true" />}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {view.steps.length === 0 ? (
        <p className="mt-5 rounded border border-fd-line bg-fd-panel py-10 text-center text-xl text-fd-mute">
          No process steps on this operation.
        </p>
      ) : (
        <ol className="mt-5 space-y-3">
          {view.steps.map((step, index) => {
            const slotRecords = recordsForSlot(step);
            const isInstruction = step.step_type === 'instruction';
            const satisfied = !isInstruction && slotSatisfied(step);
            const hasUnsatisfiedRecord = slotRecords.some((r) => r.is_conforming === false);
            const expanded = effectiveExpandedId === step.id;
            const limits = measurementLimits(step);
            const payloadReady = draftPayload(step, draft) != null;
            const showOot = oot != null && oot.stepId === step.id && oot.serial === (serialized ? effectiveSerial : null);
            return (
              <li
                key={step.id}
                id={`kiosk-step-${step.id}`}
                data-testid={`kiosk-step-${step.id}`}
                className={`rounded border bg-fd-panel ${
                  satisfied
                    ? 'border-fd-green/50'
                    : hasUnsatisfiedRecord
                      ? 'border-fd-red/60'
                      : 'border-fd-line'
                }`}
              >
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => setExpandedStepId(expanded ? null : step.id)}
                  className="flex min-h-16 w-full items-center gap-4 px-4 py-4 text-left"
                >
                  <span
                    aria-hidden="true"
                    className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full border-2 font-mono text-lg font-bold ${
                      satisfied
                        ? 'border-fd-green bg-fd-green/15 text-fd-green'
                        : hasUnsatisfiedRecord
                          ? 'border-fd-red bg-fd-red/10 text-fd-red'
                          : 'border-fd-line-bright text-fd-body'
                    }`}
                  >
                    {satisfied ? '✓' : index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-xl font-semibold text-fd-ink">{step.label}</span>
                      <StepTypeChip type={step.step_type} />
                      {step.is_required && !isInstruction && (
                        <span className="rounded border border-fd-amber/60 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-widest text-fd-amber">
                          Required
                        </span>
                      )}
                    </span>
                    {limits && <span className="mt-1 block font-mono text-base text-fd-mute">{limits}</span>}
                  </span>
                  <span className="shrink-0 text-right">
                    {isInstruction ? (
                      <span className="text-sm font-bold uppercase tracking-wider text-fd-faint">Read</span>
                    ) : satisfied ? (
                      <span className="text-sm font-bold uppercase tracking-wider text-fd-green">Recorded</span>
                    ) : hasUnsatisfiedRecord ? (
                      <span className="text-sm font-bold uppercase tracking-wider text-fd-red">Not done</span>
                    ) : (
                      <span className="text-sm font-bold uppercase tracking-wider text-fd-mute">Open</span>
                    )}
                  </span>
                </button>

                {expanded && (
                  <div className="border-t border-fd-line px-4 py-4">
                    {step.instruction_text && <p className="text-lg text-fd-body">{step.instruction_text}</p>}

                    {slotRecords.length > 0 && (
                      <ul aria-label={`Records for ${step.label}`} className={`space-y-2 ${step.instruction_text ? 'mt-3' : ''}`}>
                        {slotRecords.map((record) => (
                          <li
                            key={record.id}
                            className="flex flex-wrap items-center justify-between gap-3 rounded border border-fd-line bg-fd-sunken px-4 py-3"
                          >
                            <span className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
                              <LockClosedIcon className="h-5 w-5 shrink-0 text-fd-mute" aria-hidden="true" />
                              <span className="font-mono text-lg font-bold text-fd-ink">
                                {formatRecordValue(step, record)}
                              </span>
                              {record.is_conforming === false && (
                                <span className="rounded border border-fd-red/60 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-widest text-fd-red">
                                  Not satisfied
                                </span>
                              )}
                              <span className="text-base text-fd-mute">
                                {record.recorded_by_name || 'Operator'} · {formatCentralDateTime(record.recorded_at)}
                              </span>
                            </span>
                            {recordable && (
                              <button
                                type="button"
                                disabled={inputsDisabled}
                                aria-describedby={offlineHintId}
                                onClick={() => setSupersedeTarget({ step, record })}
                                className="min-h-12 shrink-0 rounded border border-fd-amber/60 bg-fd-amber/10 px-4 text-base font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/20 disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                Correct
                              </button>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}

                    {isInstruction ? (
                      <p className="mt-3 text-base text-fd-mute">Read and follow — no record needed.</p>
                    ) : slotRecords.length === 0 && recordable ? (
                      <div className="mt-4">
                        <StepValueEditor
                          step={step}
                          draft={draft}
                          onDraftChange={setDraft}
                          disabled={inputsDisabled}
                          idPrefix={`kiosk-step-${step.id}`}
                        />

                        {showOot && oot && (
                          <div
                            role="alert"
                            data-testid="kiosk-step-oot"
                            className="mt-4 rounded border-2 border-fd-red bg-fd-red/15 p-4"
                          >
                            <p className="text-xl font-bold uppercase tracking-wide text-fd-red">
                              Out of tolerance — not recorded
                            </p>
                            <p className="mt-1 font-mono text-lg text-fd-ink">
                              Measured {oot.info.measured} · limits {oot.info.lsl} – {oot.info.usl}
                            </p>
                            <p className="mt-2 text-base text-fd-body">
                              Re-measure and record again. If the part really is out of tolerance, use HOLD on the
                              job screen to stop it for quality review.
                            </p>
                          </div>
                        )}

                        <button
                          type="button"
                          data-testid={`kiosk-record-${step.id}`}
                          disabled={inputsDisabled || !payloadReady}
                          aria-describedby={offlineHintId}
                          onClick={() => void submitRecord(step)}
                          className="mt-4 min-h-16 w-full rounded border border-fd-green bg-fd-green/15 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {!online
                            ? 'Offline'
                            : pending
                              ? 'Recording…'
                              : step.step_type === 'photo' || step.step_type === 'file'
                                ? 'Save evidence'
                                : step.step_type === 'checkbox'
                                  ? 'Mark done'
                                  : 'Record'}
                        </button>
                      </div>
                    ) : slotRecords.length === 0 && !recordable ? (
                      <p className="mt-3 text-base text-fd-mute">
                        {!online ? 'Offline — recording is disabled.' : 'Start the job to record this step.'}
                      </p>
                    ) : null}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}

      <button
        type="button"
        onClick={onBack}
        disabled={pending}
        className="mt-5 flex min-h-16 w-full items-center justify-center gap-2 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
      >
        <ArrowLeftCircleIcon className="h-7 w-7" aria-hidden="true" />
        Back
      </button>

      {supersedeTarget && (
        <KioskSupersedeModal
          step={supersedeTarget.step}
          record={supersedeTarget.record}
          serialized={serialized}
          blocked={blocked}
          onCancel={() => setSupersedeTarget(null)}
          onSubmit={submitSupersede}
        />
      )}
    </section>
  );
}

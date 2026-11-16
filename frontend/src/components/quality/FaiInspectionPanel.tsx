/**
 * The body of the FAI detail modal: what the inspection is for, its record (inspection
 * date, inspector, notes), its characteristics (add / record a measurement / remove),
 * the final disposition and the customer's approval.
 *
 * Every write here is SERVER-GATED and therefore non-optimistic (CLAUDE.md, optimistic
 * UI convention): nothing changes on screen until the server answers, a refusal shows
 * the server's own words, and a success re-reads the FAI rather than patching it.
 *
 * - Authoring (record, characteristics) is the FAI write roles (Admin / Manager /
 *   Supervisor / Quality) and only while the FAI is unfinished: a final disposition
 *   freezes the evidence (the server answers 409 afterwards).
 * - The disposition (Passed / Conditional / Failed) is Admin / Manager / Quality and is
 *   final — it stamps the completion date (Central) and the approver.
 * - The customer's approval is its own audited verb, after a Passed or Conditional
 *   disposition, recorded once.
 * - One measurement editor is open at a time and is never replaced while it holds unsaved
 *   entries. A save sends only the fields the inspector changed, and an open editor follows
 *   a re-read of its row (e.g. Prefill from process steps), so it never writes stale blanks
 *   over saved evidence.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import EntityPicker from '../operations/EntityPicker';
import { Button, ConfirmDialog, FormField, LoadingButton, useToast } from '../ui';
import { formatCentralDate, getCentralTodayISODate } from '../../utils/centralTime';
import {
  FAI_FINAL_STATUSES,
  type FAICharacteristic,
  type FAICharacteristicUpdate,
  type FAIRecord,
  type FAIStatus,
} from '../../types/quality';

export type FAIDetailRecord = FAIRecord & { characteristics: FAICharacteristic[] };

interface FaiInspectionPanelProps {
  fai: FAIDetailRecord;
  /** FAI write roles: record, characteristics. */
  canEdit: boolean;
  /** FAI approve roles: disposition and customer approval. */
  canApprove: boolean;
  /** Re-read the FAI (and the list) after a successful write. */
  onChanged: () => Promise<void> | void;
  /** Unsaved entries, so the modal's close can confirm before discarding them. */
  onDirtyChange?: (dirty: boolean) => void;
}

const errorDetail = (err: any, fallback: string): string => {
  const detail = err?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const isFinal = (fai: FAIRecord) =>
  !!fai.completed_date || (FAI_FINAL_STATUSES as readonly string[]).includes(fai.status);

interface RecordForm {
  status: string;
  inspection_date: string;
  inspector_id: string;
  notes: string;
  deviations: string;
}

const recordFormOf = (fai: FAIRecord): RecordForm => ({
  status: fai.status === 'in_progress' ? 'in_progress' : 'pending',
  inspection_date: fai.inspection_date || '',
  inspector_id: fai.inspector_id ? String(fai.inspector_id) : '',
  notes: fai.notes || '',
  deviations: fai.deviations || '',
});

interface NewCharacteristic {
  char_number: string;
  characteristic: string;
  nominal: string;
  tolerance_plus: string;
  tolerance_minus: string;
  specification: string;
  is_critical: boolean;
  is_major: boolean;
}

const blankCharacteristic = (next: number): NewCharacteristic => ({
  char_number: String(next),
  characteristic: '',
  nominal: '',
  tolerance_plus: '',
  tolerance_minus: '',
  specification: '',
  is_critical: false,
  is_major: false,
});

interface Measurement {
  actual_value: string;
  measuring_device: string;
  result: '' | 'pass' | 'fail';
  notes: string;
}

const measurementOf = (char: FAICharacteristic): Measurement => ({
  actual_value: char.actual_value || '',
  measuring_device: char.measuring_device || '',
  result: char.is_conforming == null ? '' : char.is_conforming ? 'pass' : 'fail',
  notes: char.notes || '',
});

const MEASUREMENT_KEYS = ['actual_value', 'measuring_device', 'result', 'notes'] as const;

const nullable = (value: string) => (value.trim() === '' ? null : value);

/**
 * The open measurement editor. `baseline` is the row's saved measurement (JSON of a
 * `Measurement`) the entries are compared with: dirtiness, the fields a save sends, and
 * which fields follow a re-read all read it.
 */
interface MeasurementEditor {
  id: number;
  charNumber: number;
  value: Measurement;
  baseline: string;
  /** The saved values changed underneath the open editor (a prefill, another writer). */
  rebased: boolean;
}

const editorFor = (char: FAICharacteristic): MeasurementEditor => {
  const value = measurementOf(char);
  return { id: char.id, charNumber: char.char_number, value, baseline: JSON.stringify(value), rebased: false };
};

/**
 * Only the fields the inspector changed from the saved measurement. The server writes
 * every key it is sent, so sending an untouched field would put back the value the editor
 * opened with — erasing whatever was saved since (a prefill from process steps, say).
 */
const measurementChanges = (value: Measurement, saved: Measurement): FAICharacteristicUpdate => {
  const changes: FAICharacteristicUpdate = {};
  if (value.actual_value !== saved.actual_value) changes.actual_value = nullable(value.actual_value);
  if (value.measuring_device !== saved.measuring_device) changes.measuring_device = nullable(value.measuring_device);
  if (value.result !== saved.result) changes.is_conforming = value.result === '' ? null : value.result === 'pass';
  if (value.notes !== saved.notes) changes.notes = nullable(value.notes);
  return changes;
};

const DISPOSITIONS: Array<{ value: FAIStatus; label: string }> = [
  { value: 'passed', label: 'Passed' },
  { value: 'conditional', label: 'Conditional (passed with deviation)' },
  { value: 'failed', label: 'Failed' },
];

export default function FaiInspectionPanel({
  fai,
  canEdit,
  canApprove,
  onChanged,
  onDirtyChange,
}: FaiInspectionPanelProps) {
  const { showToast } = useToast();
  const final = isFinal(fai);
  const authoring = canEdit && !final;
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  // ── Inspection record ────────────────────────────────────────────────
  const [recordForm, setRecordForm] = useState<RecordForm>(() => recordFormOf(fai));
  const recordBaseline = JSON.stringify(recordFormOf(fai));
  // Re-seed only when the saved record itself changed — a characteristic write re-reads
  // the FAI too, and must not throw away inspection-record entries still being typed.
  useEffect(() => setRecordForm(JSON.parse(recordBaseline) as RecordForm), [recordBaseline]);
  const recordDirty = authoring && JSON.stringify(recordForm) !== recordBaseline;

  // ── Characteristics ──────────────────────────────────────────────────
  const nextCharNumber = useMemo(
    () => fai.characteristics.reduce((max, char) => Math.max(max, char.char_number), 0) + 1,
    [fai.characteristics]
  );
  const [adding, setAdding] = useState<NewCharacteristic | null>(null);
  const [editing, setEditing] = useState<MeasurementEditor | null>(null);
  const [removeTarget, setRemoveTarget] = useState<FAICharacteristic | null>(null);

  // The edited row's saved measurement as the latest read of the FAI has it ('' once the
  // row is gone). A re-read can change it under the open editor — Prefill from process
  // steps fills empty actual values — so the editor follows: fields the inspector has not
  // touched take the saved values, the inspector's own entries are kept, and the editor
  // says so. Never a stale editor that would write the old blanks back.
  const editingId = editing?.id ?? null;
  const editedSaved = useMemo(() => {
    if (editingId == null) return null;
    const row = fai.characteristics.find(char => char.id === editingId);
    return row ? JSON.stringify(measurementOf(row)) : '';
  }, [editingId, fai.characteristics]);
  useEffect(() => {
    if (!editing || editedSaved == null || editing.baseline === editedSaved) return;
    if (editedSaved === '') {
      // Removed by another writer: its entries can no longer be saved anywhere.
      if (JSON.stringify(editing.value) !== editing.baseline) {
        setError(
          `Characteristic ${editing.charNumber} is no longer on this FAI; its unsaved measurement was discarded.`
        );
      }
      setEditing(null);
      return;
    }
    const saved = JSON.parse(editedSaved) as Measurement;
    const opened = JSON.parse(editing.baseline) as Measurement;
    const follow = <K extends keyof Measurement>(key: K): Measurement[K] =>
      editing.value[key] === opened[key] ? saved[key] : editing.value[key];
    // Tell the inspector when what they see moved (an untouched field) or when the saved
    // value of a field they are editing changed — not when the re-read only echoes their
    // own save back, just before the editor closes.
    const changedUnderneath = MEASUREMENT_KEYS.some(
      key => saved[key] !== opened[key] && (editing.value[key] === opened[key] || saved[key] !== editing.value[key])
    );
    setEditing({
      ...editing,
      baseline: editedSaved,
      rebased: editing.rebased || changedUnderneath,
      value: {
        actual_value: follow('actual_value'),
        measuring_device: follow('measuring_device'),
        result: follow('result'),
        notes: follow('notes'),
      },
    });
  }, [editing, editedSaved]);

  // ── Disposition / customer approval ──────────────────────────────────
  const [disposition, setDisposition] = useState<FAIStatus | ''>('');
  const [confirmDisposition, setConfirmDisposition] = useState(false);
  const [approvalDate, setApprovalDate] = useState(() => getCentralTodayISODate());
  const [confirmCustomerApproval, setConfirmCustomerApproval] = useState(false);

  const addingDirty =
    !!adding &&
    JSON.stringify({ ...adding, char_number: '' }) !==
      JSON.stringify({ ...blankCharacteristic(nextCharNumber), char_number: '' });
  const editingDirty = !!editing && JSON.stringify(editing.value) !== editing.baseline;
  const dirty = recordDirty || addingDirty || editingDirty;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const run = async (key: string, action: () => Promise<unknown>, success: string, fallback: string) => {
    if (busy) return false;
    setBusy(key);
    setError('');
    try {
      await action();
      showToast('success', success);
      await onChanged();
      return true;
    } catch (err) {
      setError(errorDetail(err, fallback));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const saveRecord = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!recordDirty) return;
    await run(
      'record',
      () =>
        api.updateFAI(fai.id, {
          version: 0,
          status: recordForm.status as FAIStatus,
          inspection_date: recordForm.inspection_date || null,
          inspector_id: recordForm.inspector_id ? Number(recordForm.inspector_id) : null,
          notes: nullable(recordForm.notes),
          deviations: nullable(recordForm.deviations),
        }),
      'Inspection record saved',
      'Could not save the inspection record'
    );
  };

  const addCharacteristic = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!adding) return;
    const charNumber = Number.parseInt(adding.char_number, 10);
    if (!Number.isInteger(charNumber) || charNumber < 1) {
      setError('Characteristic number must be a whole number of 1 or more.');
      return;
    }
    if (adding.characteristic.trim().length < 5) {
      setError('Describe the characteristic in at least 5 characters.');
      return;
    }
    const added = await run(
      'add',
      () =>
        api.addFAICharacteristic(fai.id, {
          char_number: charNumber,
          characteristic: adding.characteristic.trim(),
          nominal: nullable(adding.nominal),
          tolerance_plus: nullable(adding.tolerance_plus),
          tolerance_minus: nullable(adding.tolerance_minus),
          specification: nullable(adding.specification),
          is_critical: adding.is_critical,
          is_major: adding.is_major,
        }),
      `Characteristic ${charNumber} added`,
      'Could not add the characteristic'
    );
    if (added) setAdding(null);
  };

  const saveMeasurement = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    const changes = measurementChanges(editing.value, JSON.parse(editing.baseline) as Measurement);
    if (Object.keys(changes).length === 0) {
      setEditing(null);
      return;
    }
    const saved = await run(
      'measure',
      () => api.updateFAICharacteristic(fai.id, editing.id, changes),
      `Characteristic ${editing.charNumber} recorded`,
      'Could not record the measurement'
    );
    if (saved) setEditing(null);
  };

  const removeCharacteristic = async () => {
    if (!removeTarget) return;
    const target = removeTarget;
    const removed = await run(
      'remove',
      () => api.deleteFAICharacteristic(fai.id, target.id),
      `Characteristic ${target.char_number} removed`,
      'Could not remove the characteristic'
    );
    if (removed && editing?.id === target.id) setEditing(null);
    setRemoveTarget(null);
  };

  const recordDisposition = async () => {
    if (!disposition) return;
    const label = DISPOSITIONS.find(option => option.value === disposition)?.label ?? disposition;
    const done = await run(
      'disposition',
      () => api.updateFAI(fai.id, { version: 0, status: disposition }),
      `${fai.fai_number} dispositioned ${label.toLowerCase()}`,
      'Could not record the disposition'
    );
    setConfirmDisposition(false);
    if (done) setDisposition('');
  };

  const recordCustomerApproval = async () => {
    await run(
      'customer',
      () => api.recordFAICustomerApproval(fai.id, approvalDate ? { customer_approval_date: approvalDate } : {}),
      `Customer approval recorded for ${fai.fai_number}`,
      'Could not record the customer approval'
    );
    setConfirmCustomerApproval(false);
  };

  const unrecorded = fai.characteristics.filter(char => char.is_conforming == null).length;
  const revisionDrift =
    !!fai.part_revision && !!fai.part_current_revision && fai.part_revision !== fai.part_current_revision;
  const canRecordCustomerApproval =
    canApprove && (fai.status === 'passed' || fai.status === 'conditional') && !fai.customer_approved;

  const summary: Array<[string, React.ReactNode]> = [
    [
      'Job #',
      fai.work_order_id ? (
        <Link className="text-fd-link underline" to={`/work-orders/${fai.work_order_id}`}>
          {fai.work_order_number || `Work order ${fai.work_order_id}`}
        </Link>
      ) : (
        '—'
      ),
    ],
    ['Customer', fai.customer_name || '—'],
    ['Customer PO', fai.customer_po || '—'],
    [
      'Revision inspected',
      <>
        {fai.part_revision || '—'}
        {revisionDrift && (
          <span className="ml-2 text-xs text-fd-amber">Part is now Rev {fai.part_current_revision}</span>
        )}
      </>,
    ],
    ['Serial #', fai.serial_number || '—'],
    ['Reason', fai.reason ? fai.reason.replace(/_/g, ' ') : '—'],
    ['Due', fai.due_date ? formatCentralDate(fai.due_date) : '—'],
    ['Inspection date', fai.inspection_date ? formatCentralDate(fai.inspection_date) : '—'],
    ['Inspector', fai.inspector_name || '—'],
    ['Completed', fai.completed_date ? formatCentralDate(fai.completed_date) : '—'],
    ['Dispositioned by', fai.approved_by_name || '—'],
    [
      'Customer approval',
      fai.customer_approved
        ? `Approved${fai.customer_approval_date ? ` ${formatCentralDate(fai.customer_approval_date)}` : ''}`
        : fai.customer_approval_required
          ? 'Required — not yet recorded'
          : 'Not required',
    ],
  ];

  return (
    <div className="space-y-5">
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4" data-testid="fai-summary">
        {summary.map(([term, value]) => (
          <div key={term}>
            <dt className="text-xs uppercase tracking-wider text-slate-400">{term}</dt>
            <dd className="break-words">{value}</dd>
          </div>
        ))}
      </dl>

      {error && (
        <p role="alert" className="border border-red-500/40 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {authoring && (
        <form onSubmit={saveRecord} className="space-y-3 border border-fd-line p-3" aria-label="Inspection record">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Inspection record</h4>
          <fieldset disabled={busy !== null} className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <FormField label="Inspection status">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={recordForm.status}
                  onChange={event => setRecordForm(form => ({ ...form, status: event.target.value }))}
                >
                  <option value="pending">Pending</option>
                  <option value="in_progress">In progress</option>
                </select>
              )}
            </FormField>
            <FormField label="Inspection date" help="The date the first article was inspected. Not in the future.">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  // The server refuses a future date (422): a final disposition would freeze it,
                  // and no customer approval date could then be recorded.
                  max={getCentralTodayISODate()}
                  value={recordForm.inspection_date}
                  onChange={event => setRecordForm(form => ({ ...form, inspection_date: event.target.value }))}
                />
              )}
            </FormField>
            <FormField label="Inspector">
              {field => (
                <EntityPicker
                  kind="user"
                  optional
                  id={field.id}
                  aria-describedby={field['aria-describedby']}
                  value={recordForm.inspector_id}
                  onChange={value => setRecordForm(form => ({ ...form, inspector_id: value }))}
                  pinned={
                    fai.inspector_id
                      ? { value: String(fai.inspector_id), label: fai.inspector_name || `User #${fai.inspector_id}` }
                      : null
                  }
                />
              )}
            </FormField>
            <FormField label="Notes" className="sm:col-span-3">
              {field => (
                <textarea
                  {...field}
                  className="input"
                  rows={2}
                  maxLength={2000}
                  value={recordForm.notes}
                  onChange={event => setRecordForm(form => ({ ...form, notes: event.target.value }))}
                />
              )}
            </FormField>
            <FormField label="Deviations" className="sm:col-span-3">
              {field => (
                <textarea
                  {...field}
                  className="input"
                  rows={2}
                  maxLength={2000}
                  value={recordForm.deviations}
                  onChange={event => setRecordForm(form => ({ ...form, deviations: event.target.value }))}
                />
              )}
            </FormField>
          </fieldset>
          <div className="flex justify-end">
            <LoadingButton type="submit" size="sm" loading={busy === 'record'} disabled={!recordDirty || busy !== null}>
              Save inspection record
            </LoadingButton>
          </div>
        </form>
      )}

      <section aria-label="Characteristics" className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Characteristics</h4>
          {authoring && !adding && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                setError('');
                setAdding(blankCharacteristic(nextCharNumber));
              }}
            >
              Add characteristic
            </Button>
          )}
        </div>

        {adding && (
          <form
            onSubmit={addCharacteristic}
            className="space-y-3 border border-fd-line p-3"
            aria-label="New characteristic"
          >
            <fieldset disabled={busy !== null} className="grid grid-cols-2 gap-3 sm:grid-cols-6">
              <FormField label="Char #" required>
                {field => (
                  <input
                    {...field}
                    type="number"
                    min="1"
                    step="1"
                    className="input"
                    required
                    value={adding.char_number}
                    onChange={event => setAdding(form => form && { ...form, char_number: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Characteristic" required className="col-span-2 sm:col-span-5">
                {field => (
                  <input
                    {...field}
                    className="input"
                    required
                    minLength={5}
                    maxLength={255}
                    value={adding.characteristic}
                    onChange={event => setAdding(form => form && { ...form, characteristic: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Nominal">
                {field => (
                  <input
                    {...field}
                    className="input font-mono"
                    maxLength={100}
                    value={adding.nominal}
                    onChange={event => setAdding(form => form && { ...form, nominal: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Tol +">
                {field => (
                  <input
                    {...field}
                    className="input font-mono"
                    maxLength={50}
                    value={adding.tolerance_plus}
                    onChange={event => setAdding(form => form && { ...form, tolerance_plus: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Tol −">
                {field => (
                  <input
                    {...field}
                    className="input font-mono"
                    maxLength={50}
                    value={adding.tolerance_minus}
                    onChange={event => setAdding(form => form && { ...form, tolerance_minus: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Specification" className="col-span-2 sm:col-span-3">
                {field => (
                  <input
                    {...field}
                    className="input"
                    maxLength={255}
                    value={adding.specification}
                    onChange={event => setAdding(form => form && { ...form, specification: event.target.value })}
                  />
                )}
              </FormField>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={adding.is_critical}
                  onChange={event => setAdding(form => form && { ...form, is_critical: event.target.checked })}
                />
                Critical
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={adding.is_major}
                  onChange={event => setAdding(form => form && { ...form, is_major: event.target.checked })}
                />
                Major
              </label>
            </fieldset>
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="secondary" disabled={busy !== null} onClick={() => setAdding(null)}>
                Cancel
              </Button>
              <LoadingButton type="submit" size="sm" loading={busy === 'add'} disabled={busy !== null}>
                Add
              </LoadingButton>
            </div>
          </form>
        )}

        {fai.characteristics.length === 0 ? (
          <p className="py-6 text-center text-sm text-slate-400">No characteristics on this FAI yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 text-left text-xs uppercase tracking-wider text-slate-400">
                  <th className="py-2 pr-3">#</th>
                  <th className="py-2 pr-3">Characteristic</th>
                  <th className="py-2 pr-3">Nominal / Spec</th>
                  <th className="py-2 pr-3">Actual</th>
                  <th className="py-2 pr-3">Device</th>
                  <th className="py-2 pr-3">Conforming</th>
                  {authoring && <th className="py-2 text-right">Actions</th>}
                </tr>
              </thead>
              <tbody>
                {fai.characteristics.map(char => (
                  <React.Fragment key={char.id}>
                    <tr className="border-b border-slate-800">
                      <td className="py-2 pr-3 font-mono text-slate-400">{char.char_number}</td>
                      <td className="py-2 pr-3">
                        {char.characteristic}
                        {char.is_critical && (
                          <span className="ml-1 rounded border border-fd-red/50 px-1 py-0.5 font-mono text-[10px] font-bold uppercase text-fd-red">
                            Critical
                          </span>
                        )}
                        {char.is_major && (
                          <span className="ml-1 rounded border border-fd-amber/50 px-1 py-0.5 font-mono text-[10px] font-bold uppercase text-fd-amber">
                            Major
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-3 font-mono text-slate-300">
                        {char.nominal || char.specification || '—'}
                        {char.nominal && (char.tolerance_plus || char.tolerance_minus) && (
                          <span className="ml-1 text-slate-500">
                            +{char.tolerance_plus || '0'} / −{char.tolerance_minus || '0'}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-3 font-mono">{char.actual_value || '—'}</td>
                      <td className="py-2 pr-3 text-slate-300">{char.measuring_device || '—'}</td>
                      <td className="py-2 pr-3">
                        {char.is_conforming == null ? (
                          <span className="text-slate-500">—</span>
                        ) : char.is_conforming ? (
                          <span className="text-green-500">Pass</span>
                        ) : (
                          <span className="text-red-400">Fail</span>
                        )}
                      </td>
                      {authoring && (
                        <td className="py-2 text-right whitespace-nowrap">
                          <Button
                            size="sm"
                            variant="ghost"
                            // One editor at a time, and opening another never drops typed
                            // entries: while the open one is unsaved, the other rows wait.
                            disabled={busy !== null || editing?.id === char.id || editingDirty}
                            title={
                              editingDirty && editing?.id !== char.id
                                ? `Save or cancel the measurement for characteristic ${editing?.charNumber} first`
                                : undefined
                            }
                            aria-label={`Record measurement for characteristic ${char.char_number}`}
                            onClick={() => {
                              setError('');
                              setEditing(editorFor(char));
                            }}
                          >
                            Record
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy !== null}
                            aria-label={`Remove characteristic ${char.char_number}`}
                            onClick={() => setRemoveTarget(char)}
                          >
                            Remove
                          </Button>
                        </td>
                      )}
                    </tr>
                    {authoring && editing?.id === char.id && (
                      <tr className="border-b border-slate-800 bg-slate-900/40">
                        <td colSpan={7} className="p-3">
                          <form
                            onSubmit={saveMeasurement}
                            className="space-y-3"
                            aria-label={`Measurement for characteristic ${char.char_number}`}
                          >
                            {editing.rebased && (
                              <p role="status" className="text-xs text-fd-amber">
                                The saved measurement changed while this was open (for example, Prefill from process
                                steps). Fields you had not changed now show the saved values; your own entries are kept.
                              </p>
                            )}
                            <fieldset disabled={busy !== null} className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                              <FormField label="Actual value">
                                {field => (
                                  <input
                                    {...field}
                                    className="input font-mono"
                                    maxLength={100}
                                    value={editing.value.actual_value}
                                    onChange={event =>
                                      setEditing(
                                        current =>
                                          current && {
                                            ...current,
                                            value: { ...current.value, actual_value: event.target.value },
                                          }
                                      )
                                    }
                                  />
                                )}
                              </FormField>
                              <FormField label="Measuring device">
                                {field => (
                                  <input
                                    {...field}
                                    className="input"
                                    maxLength={100}
                                    value={editing.value.measuring_device}
                                    onChange={event =>
                                      setEditing(
                                        current =>
                                          current && {
                                            ...current,
                                            value: { ...current.value, measuring_device: event.target.value },
                                          }
                                      )
                                    }
                                  />
                                )}
                              </FormField>
                              <FormField label="Result">
                                {field => (
                                  <select
                                    {...field}
                                    className="input"
                                    value={editing.value.result}
                                    onChange={event =>
                                      setEditing(
                                        current =>
                                          current && {
                                            ...current,
                                            value: {
                                              ...current.value,
                                              result: event.target.value as Measurement['result'],
                                            },
                                          }
                                      )
                                    }
                                  >
                                    <option value="">Not recorded</option>
                                    <option value="pass">Pass (conforming)</option>
                                    <option value="fail">Fail (nonconforming)</option>
                                  </select>
                                )}
                              </FormField>
                              <FormField label="Measurement notes">
                                {field => (
                                  <input
                                    {...field}
                                    className="input"
                                    maxLength={500}
                                    value={editing.value.notes}
                                    onChange={event =>
                                      setEditing(
                                        current =>
                                          current && {
                                            ...current,
                                            value: { ...current.value, notes: event.target.value },
                                          }
                                      )
                                    }
                                  />
                                )}
                              </FormField>
                            </fieldset>
                            <div className="flex justify-end gap-2">
                              <Button
                                size="sm"
                                variant="secondary"
                                disabled={busy !== null}
                                onClick={() => setEditing(null)}
                              >
                                Cancel
                              </Button>
                              <LoadingButton
                                type="submit"
                                size="sm"
                                loading={busy === 'measure'}
                                disabled={busy !== null || !editingDirty}
                              >
                                Save measurement
                              </LoadingButton>
                            </div>
                          </form>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {canApprove && !final && (
        <section aria-label="Final disposition" className="space-y-2 border border-fd-line p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Final disposition</h4>
          <p className="text-sm text-slate-300">
            Recording the disposition completes the FAI: it stamps today&apos;s date and you as the approver, and the
            inspection evidence can no longer be changed.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <FormField label="Disposition" className="min-w-[14rem]">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={disposition}
                  disabled={busy !== null}
                  onChange={event => setDisposition(event.target.value as FAIStatus | '')}
                >
                  <option value="">Select…</option>
                  {DISPOSITIONS.map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <Button
              disabled={!disposition || busy !== null || dirty}
              title={dirty ? 'Save or cancel your unsaved entries first' : undefined}
              onClick={() => setConfirmDisposition(true)}
            >
              Record disposition
            </Button>
          </div>
        </section>
      )}

      {canRecordCustomerApproval && (
        <section aria-label="Customer approval" className="space-y-2 border border-fd-line p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Customer approval</h4>
          <div className="flex flex-wrap items-end gap-3">
            <FormField label="Customer approval date" help="The date the customer approved the FAI. Not in the future.">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  max={getCentralTodayISODate()}
                  value={approvalDate}
                  disabled={busy !== null}
                  onChange={event => setApprovalDate(event.target.value)}
                />
              )}
            </FormField>
            <Button disabled={busy !== null} onClick={() => setConfirmCustomerApproval(true)}>
              Record customer approval
            </Button>
          </div>
        </section>
      )}

      <ConfirmDialog
        open={confirmDisposition}
        title={`Disposition ${fai.fai_number}?`}
        message={`${DISPOSITIONS.find(option => option.value === disposition)?.label ?? ''}: this is the final disposition. The completion date and you as the approver are recorded, and the inspection evidence becomes read-only.${
          unrecorded > 0
            ? ` ${unrecorded} characteristic${unrecorded === 1 ? ' has' : 's have'} no result recorded.`
            : ''
        }`}
        variant="warning"
        confirmLabel="Record disposition"
        pending={busy === 'disposition'}
        onConfirm={() => void recordDisposition()}
        onCancel={() => {
          if (busy !== 'disposition') setConfirmDisposition(false);
        }}
      />
      <ConfirmDialog
        open={confirmCustomerApproval}
        title="Record the customer's approval?"
        message={`The customer's approval of ${fai.fai_number}${
          approvalDate ? `, dated ${formatCentralDate(approvalDate)}` : ''
        }, is recorded once and cannot be edited afterwards.`}
        variant="info"
        confirmLabel="Record customer approval"
        pending={busy === 'customer'}
        onConfirm={() => void recordCustomerApproval()}
        onCancel={() => {
          if (busy !== 'customer') setConfirmCustomerApproval(false);
        }}
      />
      <ConfirmDialog
        open={removeTarget !== null}
        title={`Remove characteristic ${removeTarget?.char_number ?? ''}?`}
        message={`"${removeTarget?.characteristic ?? ''}" is removed from ${fai.fai_number}. Its contents are kept in the audit log.`}
        variant="danger"
        confirmLabel="Remove characteristic"
        pending={busy === 'remove'}
        onConfirm={() => void removeCharacteristic()}
        onCancel={() => {
          if (busy !== 'remove') setRemoveTarget(null);
        }}
      />
    </div>
  );
}

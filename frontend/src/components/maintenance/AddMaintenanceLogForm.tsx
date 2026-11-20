import React, { useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { useToast } from '../ui/Toast';
import { toDisplayString } from '../../utils/apiError';
import { getCentralNowDateTimeLocal } from '../../utils/centralTime';
import {
  BLANK_MAINTENANCE_LOG_FORM,
  MAINTENANCE_LOG_ENTRY_TYPE_LABELS,
  MAINTENANCE_LOG_MAX_BACKDATE_DAYS,
  MaintenanceLogForm,
  maintenanceLogCostProblem,
  maintenanceLogFormProblem,
  maintenanceLogPayload,
  maintenanceLogWhenProblem,
} from '../../utils/maintenance';
import type { MaintenanceLogEntry, MaintenanceLogEntryType } from '../../types/maintenance';

const MINUTE_MS = 60 * 1000;
const DAY_MS = 24 * 60 * MINUTE_MS;

/**
 * Picker guides for the When input, read from the clock when it is focused (the form is
 * noValidate: the server judges the date against its own clock). The earliest offered minute is
 * two minutes inside the 30-day limit, so picking it is not a certain refusal by the time it is sent.
 */
const pickerBounds = () => ({
  max: getCentralNowDateTimeLocal(),
  min: getCentralNowDateTimeLocal(new Date(Date.now() - MAINTENANCE_LOG_MAX_BACKDATE_DAYS * DAY_MS + 2 * MINUTE_MS)),
});

interface Props {
  /** The work order the entry is filed against; the server takes its target. */
  workOrderId: number;
  /** Called with the server's entry once it is saved, so the timeline can re-read. */
  onAdded: (entry: MaintenanceLogEntry) => void;
  /** Reports whether the form holds anything typed, for the unsaved-changes guard. */
  onDirtyChange: (dirty: boolean) => void;
  /** Reports while an entry is being filed, so the dialog cannot be closed under it. */
  onSavingChange?: (saving: boolean) => void;
}

/**
 * Files a log entry -- inspection, repair, part replacement, observation or note -- against one
 * maintenance work order (`POST /maintenance/log`, Admin / Manager / Supervisor / Operator; render
 * it only for those). It sends the work order and never a target. Server-gated, so not optimistic:
 * the timeline shows the entry only after the server saved it, and a refusal keeps everything typed
 * with the server's words above it.
 */
export function AddMaintenanceLogForm({ workOrderId, onAdded, onDirtyChange, onSavingChange }: Props) {
  const { showToast } = useToast();
  const [form, setForm] = useState<MaintenanceLogForm>(BLANK_MAINTENANCE_LOG_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [bounds, setBounds] = useState(pickerBounds);
  const costInput = useRef<HTMLInputElement>(null);
  const whenInput = useRef<HTMLInputElement>(null);
  // Bad input (see MaintenanceLogForm) is part of the form, so it is a problem AND a change.
  const dirty = JSON.stringify(form) !== JSON.stringify(BLANK_MAINTENANCE_LOG_FORM);
  const problem = maintenanceLogFormProblem(form);
  const costProblem = maintenanceLogCostProblem(form);
  const whenProblem = maintenanceLogWhenProblem(form);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    onSavingChange?.(saving);
  }, [saving, onSavingChange]);

  // An input's bad-input state, read whenever it may have changed: a partly typed date-time does
  // not always fire `input`, so blur and submit read it again.
  const syncBadInput = () =>
    setForm(f => ({
      ...f,
      cost_bad_input: costInput.current?.validity.badInput ?? false,
      when_bad_input: whenInput.current?.validity.badInput ?? false,
    }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const live: MaintenanceLogForm = {
      ...form,
      cost_bad_input: costInput.current?.validity.badInput ?? false,
      when_bad_input: whenInput.current?.validity.badInput ?? false,
    };
    if (live.cost_bad_input !== form.cost_bad_input || live.when_bad_input !== form.when_bad_input) setForm(live);
    if (maintenanceLogFormProblem(live)) return;
    setSaving(true);
    setError('');
    try {
      const entry = await api.createMaintenanceLog(maintenanceLogPayload(workOrderId, live));
      setForm(BLANK_MAINTENANCE_LOG_FORM);
      showToast('success', 'Log entry added');
      onAdded(entry);
    } catch (err: any) {
      setError(toDisplayString(err?.response?.data?.detail) || 'Unable to add the log entry.');
    } finally {
      setSaving(false);
    }
  };

  const labelClass = 'block text-sm font-medium text-slate-300 mb-1';
  return (
    <form
      onSubmit={submit}
      noValidate
      aria-label="Add log entry"
      className="space-y-3 rounded-sm border border-slate-700 p-3"
    >
      <h4 className="text-sm font-semibold text-slate-200">Add log entry</h4>
      {error && (
        <p role="alert" className="rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {error}
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <FormField label="Type" labelClassName={labelClass}>
          {field => (
            <select
              {...field}
              className="input w-full"
              value={form.event_type}
              onChange={e => setForm(f => ({ ...f, event_type: e.target.value as MaintenanceLogEntryType }))}
            >
              {(Object.keys(MAINTENANCE_LOG_ENTRY_TYPE_LABELS) as MaintenanceLogEntryType[]).map(type => (
                <option key={type} value={type}>
                  {MAINTENANCE_LOG_ENTRY_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <FormField label="Cost ($)" error={costProblem} labelClassName={labelClass}>
          {field => (
            <input
              {...field}
              ref={costInput}
              type="number"
              min={0}
              step="0.01"
              className="input w-full"
              value={form.cost}
              onChange={e => {
                const { value, validity } = e.currentTarget;
                setForm(f => ({ ...f, cost: value, cost_bad_input: validity.badInput }));
              }}
              onBlur={syncBadInput}
            />
          )}
        </FormField>
        <FormField
          label="When (Central)"
          help={`Leave blank for now. At most ${MAINTENANCE_LOG_MAX_BACKDATE_DAYS} days back.`}
          error={whenProblem}
          labelClassName={labelClass}
        >
          {field => (
            <input
              {...field}
              ref={whenInput}
              type="datetime-local"
              className="input w-full"
              min={bounds.min}
              max={bounds.max}
              value={form.when}
              onFocus={() => setBounds(pickerBounds())}
              onChange={e => {
                const { value, validity } = e.currentTarget;
                setForm(f => ({ ...f, when: value, when_bad_input: validity.badInput }));
              }}
              onBlur={syncBadInput}
            />
          )}
        </FormField>
      </div>
      <FormField label="Description" required labelClassName={labelClass}>
        {field => (
          <textarea
            {...field}
            rows={3}
            maxLength={4000}
            className="input w-full"
            value={form.description}
            onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
          />
        )}
      </FormField>
      <div className="flex justify-end">
        <LoadingButton
          type="submit"
          size="sm"
          loading={saving}
          loadingText="Adding…"
          disabled={!!problem}
          title={problem ?? undefined}
        >
          Add entry
        </LoadingButton>
      </div>
    </form>
  );
}

export default AddMaintenanceLogForm;

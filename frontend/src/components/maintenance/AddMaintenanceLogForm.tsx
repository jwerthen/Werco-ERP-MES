import React, { useEffect, useMemo, useState } from 'react';
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
  maintenanceLogFormProblem,
  maintenanceLogPayload,
} from '../../utils/maintenance';
import type { MaintenanceLogEntry, MaintenanceLogEntryType } from '../../types/maintenance';

const DAY_MS = 24 * 60 * 60 * 1000;

interface Props {
  /** The work order the entry is filed against; the server takes its target. */
  workOrderId: number;
  /** Called with the server's entry once it is saved, so the timeline can re-read. */
  onAdded: (entry: MaintenanceLogEntry) => void;
  /** Reports whether the form holds anything typed, for the unsaved-changes guard. */
  onDirtyChange: (dirty: boolean) => void;
}

/**
 * Files a log entry -- inspection, repair, part replacement, observation or note -- against one
 * maintenance work order (`POST /maintenance/log`, Admin / Manager / Supervisor / Operator; render
 * it only for those). It sends the work order and never a target. Server-gated, so not optimistic:
 * the timeline shows the entry only after the server saved it, and a refusal keeps everything typed
 * with the server's words above it.
 */
export function AddMaintenanceLogForm({ workOrderId, onAdded, onDirtyChange }: Props) {
  const { showToast } = useToast();
  const [form, setForm] = useState<MaintenanceLogForm>(BLANK_MAINTENANCE_LOG_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const dirty = JSON.stringify(form) !== JSON.stringify(BLANK_MAINTENANCE_LOG_FORM);
  const problem = maintenanceLogFormProblem(form);
  const costInvalid = !!form.cost.trim() && !(Number(form.cost) >= 0);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  // Picker guides only (the form is noValidate): the server judges the date against its own clock.
  const bounds = useMemo(
    () => ({
      max: getCentralNowDateTimeLocal(),
      min: getCentralNowDateTimeLocal(new Date(Date.now() - MAINTENANCE_LOG_MAX_BACKDATE_DAYS * DAY_MS)),
    }),
    []
  );

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || problem) return;
    setSaving(true);
    setError('');
    try {
      const entry = await api.createMaintenanceLog(maintenanceLogPayload(workOrderId, form));
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
        <FormField
          label="Cost ($)"
          error={costInvalid ? 'Cost must be zero or more.' : null}
          labelClassName={labelClass}
        >
          {field => (
            <input
              {...field}
              type="number"
              min={0}
              step="0.01"
              className="input w-full"
              value={form.cost}
              onChange={e => setForm(f => ({ ...f, cost: e.target.value }))}
            />
          )}
        </FormField>
        <FormField
          label="When (Central)"
          help={`Leave blank for now. At most ${MAINTENANCE_LOG_MAX_BACKDATE_DAYS} days back.`}
          labelClassName={labelClass}
        >
          {field => (
            <input
              {...field}
              type="datetime-local"
              className="input w-full"
              min={bounds.min}
              max={bounds.max}
              value={form.when}
              onChange={e => setForm(f => ({ ...f, when: e.target.value }))}
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

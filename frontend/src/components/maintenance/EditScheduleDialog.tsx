import React, { useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { useToast } from '../ui/Toast';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { toDisplayString } from '../../utils/apiError';
import {
  MAINTENANCE_FREQUENCY_LABELS,
  MAINTENANCE_PRIORITY_LABELS,
  checklistLines,
  checklistPayload,
  targetText,
} from '../../utils/maintenance';
import type {
  MaintenanceFrequency,
  MaintenancePriority,
  MaintenanceSchedule,
  MaintenanceScheduleUpdate,
} from '../../types/maintenance';

/** The editable fields of a PM schedule, as the form holds them (strings for the inputs). */
export interface ScheduleEditForm {
  next_due_date: string;
  frequency: string;
  frequency_days: string;
  priority: string;
  estimated_duration_hours: string;
  requires_shutdown: boolean;
  /** One checklist item per line. */
  checklist: string;
}

export const scheduleEditForm = (schedule: MaintenanceSchedule): ScheduleEditForm => ({
  next_due_date: schedule.next_due_date ?? '',
  frequency: String(schedule.frequency ?? 'monthly'),
  frequency_days: schedule.frequency_days != null ? String(schedule.frequency_days) : '',
  priority: String(schedule.priority ?? 'medium'),
  estimated_duration_hours: String(schedule.estimated_duration_hours ?? 1),
  requires_shutdown: !!schedule.requires_shutdown,
  checklist: (schedule.checklist_items ?? []).join('\n'),
});

/** Why the form cannot be saved yet, or null when it can. */
export const scheduleEditProblem = (form: ScheduleEditForm): string | null => {
  if (!form.next_due_date) return 'Next due date is required.';
  if (form.frequency === 'custom') {
    const days = Number(form.frequency_days);
    if (!Number.isInteger(days) || days < 1 || days > 3650)
      return 'Every N days must be a whole number from 1 to 3650.';
  }
  const hours = Number(form.estimated_duration_hours);
  if (form.estimated_duration_hours.trim() === '' || !Number.isFinite(hours) || hours < 0) {
    return 'Estimated duration must be zero or more hours.';
  }
  return null;
};

/**
 * Only the fields the planner actually changed. The PUT applies only the fields it is sent, so a
 * dialog left open while someone completes this PM (which moves `next_due_date` forward) cannot
 * put the old due date back unless the planner changed the due date themselves.
 */
export const scheduleEditPayload = (before: ScheduleEditForm, after: ScheduleEditForm): MaintenanceScheduleUpdate => {
  const payload: MaintenanceScheduleUpdate = {};
  if (after.next_due_date !== before.next_due_date) payload.next_due_date = after.next_due_date;
  if (after.frequency !== before.frequency) payload.frequency = after.frequency as MaintenanceFrequency;
  if (
    after.frequency === 'custom' &&
    (after.frequency !== before.frequency || after.frequency_days !== before.frequency_days)
  ) {
    payload.frequency_days = Number(after.frequency_days);
  }
  if (after.priority !== before.priority) payload.priority = after.priority as MaintenancePriority;
  if (Number(after.estimated_duration_hours) !== Number(before.estimated_duration_hours)) {
    payload.estimated_duration_hours = Number(after.estimated_duration_hours);
  }
  if (after.requires_shutdown !== before.requires_shutdown) payload.requires_shutdown = after.requires_shutdown;
  // Compared as normalized items: a spacing or bullet edit changes nothing the server would store,
  // while a change of case or wording is a real edit (the stored spelling is what is shown).
  if (JSON.stringify(checklistLines(after.checklist)) !== JSON.stringify(checklistLines(before.checklist))) {
    payload.checklist = checklistPayload(after.checklist);
  }
  return payload;
};

interface Props {
  schedule: MaintenanceSchedule;
  onClose: () => void;
  onSaved: (schedule: MaintenanceSchedule) => void;
}

const labelClass = 'block text-sm font-medium text-slate-300 mb-1';
const inputClass = 'w-full px-3 py-2 border rounded-sm';

/**
 * Edit a PM schedule's due date, cadence, priority, duration, shutdown flag and checklist
 * (`PUT /maintenance/schedules/{id}`, Admin / Manager / Supervisor). The schedule's name and
 * target are not editable here: the PUT never moves a target, and the PM import matches a
 * schedule by target + name, so a rename would make the next import create a duplicate.
 */
export function EditScheduleDialog({ schedule, onClose, onSaved }: Props) {
  const { showToast } = useToast();
  const [baseline] = useState(() => scheduleEditForm(schedule));
  const [form, setForm] = useState(baseline);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const payload = scheduleEditPayload(baseline, form);
  const changed = Object.keys(payload).length > 0;
  const problem = scheduleEditProblem(form);
  const { confirmDiscard, markSaved } = useUnsavedChanges(changed);

  const close = () => {
    if (saving || !confirmDiscard()) return;
    onClose();
  };

  const save = async () => {
    if (saving || !changed || problem) return;
    setSaving(true);
    setError('');
    try {
      const updated = await api.updateMaintenanceSchedule(schedule.id, payload);
      markSaved();
      showToast('success', `${schedule.name} updated`);
      onSaved(updated);
    } catch (err: any) {
      const message =
        toDisplayString(err?.response?.data?.detail) || 'Unable to save. Check your entries and try again.';
      setError(message);
      showToast('error', message);
    } finally {
      setSaving(false);
    }
  };

  const isCustom = form.frequency === 'custom';

  return (
    <Modal
      open
      onClose={close}
      size="lg"
      scroll={false}
      padded={false}
      closeOnBackdrop={false}
      ariaLabelledBy="edit-schedule-title"
    >
      {error && (
        <p role="alert" className="text-red-300 p-3">
          {error}
        </p>
      )}
      <div className="flex justify-between items-center p-4 border-b border-slate-700">
        <div>
          <h3 id="edit-schedule-title" className="text-lg font-semibold">
            Edit PM schedule
          </h3>
          <p className="text-sm text-slate-400">
            {schedule.name} · {targetText(schedule)}
          </p>
        </div>
        <button type="button" onClick={close} aria-label="Close dialog">
          <XMarkIcon className="w-5 h-5" aria-hidden="true" />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
        <p className="text-xs text-slate-400">
          Completing a work order started from this schedule moves the next due date forward on its own. Change it here
          to reschedule or correct it.
        </p>
        <div className="grid grid-cols-2 gap-3">
          <FormField label="Next Due Date" required labelClassName={labelClass}>
            {field => (
              <input
                {...field}
                type="date"
                required
                value={form.next_due_date}
                onChange={e => setForm(f => ({ ...f, next_due_date: e.target.value }))}
                className={inputClass}
              />
            )}
          </FormField>
          <FormField label="Priority" labelClassName={labelClass}>
            {field => (
              <select
                {...field}
                value={form.priority}
                onChange={e => setForm(f => ({ ...f, priority: e.target.value }))}
                className={inputClass}
              >
                {Object.entries(MAINTENANCE_PRIORITY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField label="Frequency" labelClassName={labelClass}>
            {field => (
              <select
                {...field}
                value={form.frequency}
                onChange={e => setForm(f => ({ ...f, frequency: e.target.value }))}
                className={inputClass}
              >
                {Object.entries(MAINTENANCE_FREQUENCY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          {isCustom ? (
            <FormField label="Every N days" required labelClassName={labelClass}>
              {field => (
                <input
                  {...field}
                  type="number"
                  min={1}
                  max={3650}
                  step={1}
                  required
                  value={form.frequency_days}
                  onChange={e => setForm(f => ({ ...f, frequency_days: e.target.value }))}
                  className={inputClass}
                />
              )}
            </FormField>
          ) : (
            <div />
          )}
          <FormField label="Est. Duration (hours)" labelClassName={labelClass}>
            {field => (
              <input
                {...field}
                type="number"
                min={0}
                step="0.5"
                value={form.estimated_duration_hours}
                onChange={e => setForm(f => ({ ...f, estimated_duration_hours: e.target.value }))}
                className={inputClass}
              />
            )}
          </FormField>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.requires_shutdown}
            onChange={e => setForm(f => ({ ...f, requires_shutdown: e.target.checked }))}
          />
          <span>Requires machine shutdown</span>
        </label>
        <FormField label="Checklist" help="One item per line, in the order they are done." labelClassName={labelClass}>
          {field => (
            <textarea
              {...field}
              value={form.checklist}
              onChange={e => setForm(f => ({ ...f, checklist: e.target.value }))}
              rows={6}
              className={inputClass}
            />
          )}
        </FormField>
        {changed && problem && (
          <p className="text-sm text-amber-300" role="status">
            {problem}
          </p>
        )}
      </div>
      <div className="flex justify-end gap-2 p-4 border-t border-slate-700">
        <Button variant="secondary" onClick={close} disabled={saving}>
          Cancel
        </Button>
        <LoadingButton onClick={save} loading={saving} loadingText="Saving…" disabled={!changed || !!problem}>
          Save
        </LoadingButton>
      </div>
    </Modal>
  );
}

export default EditScheduleDialog;

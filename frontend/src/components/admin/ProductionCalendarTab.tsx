/**
 * Admin > Production Calendar tab.
 *
 * Edits the company's production calendar (`/production-calendar`, migration 120):
 * when a production day starts on the local clock, the shift patterns, which work
 * centers each pattern covers, and each pattern's shifts. OEE, the wallboard "today"
 * pulse, the daily-output / employee-time reports, downtime filters and job-cost labor
 * dates all bucket work by production day, and the header HUD reads the default
 * pattern's current shift from here.
 *
 * Writes are server-GATED, so nothing here is optimistic:
 *   - Save sends the WHOLE document with `expected_version`; a 409 (someone saved in
 *     between) opens a reload prompt, a 422 shows the server's reason verbatim.
 *   - "Sync working calendars" is a dry run first (a current-vs-new table, writes
 *     nothing), then a confirmed commit carrying the dry run's calendar version.
 *
 * RBAC mirrors the server: saving is Admin only (CALENDAR_EDIT_ROLES), the sync is
 * Admin / Manager / Supervisor (WORKING_CALENDAR_SYNC_ROLES). The tab sits inside the
 * Admin-only AdminSettings route; the checks below are defense in depth.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowPathIcon,
  CalendarDaysIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
  PlusIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { ConfirmDialog, ErrorState, FormField, LoadingButton, useToast } from '../ui';
import type { WorkCenter } from '../../types';
import type { ProductionCalendar, WorkingCalendarSyncResult } from '../../types/productionCalendar';
import {
  buildCalendarUpdate,
  draftFromCalendar,
  draftIssues,
  formatHours,
  formatShiftDays,
  MINUTES_PER_DAY,
  newDraftKey,
  shiftSpan,
  WEEKDAY_SHORT,
  type CalendarDraft,
  type PatternDraft,
  type ShiftDraft,
} from '../../utils/productionCalendar';
import { canEditProductionCalendar, canSyncWorkingCalendars } from '../../utils/recordWriteAccess';
import { apiErrorDetail, apiErrorStatus } from '../../utils/apiError';
import { formatCentralDateTime, formatCentralTime } from '../../utils/centralTime';

const WEEKDAY_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

const formatLength = (minutes: number | null): string => {
  if (minutes == null) return '—';
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
};

const blankShift = (): ShiftDraft => ({
  key: newDraftKey('s'),
  code: '',
  name: '',
  start: '07:00',
  end: '15:30',
  days: [0, 1, 2, 3, 4],
  planned_minutes: 480,
  is_active: true,
  sort_order: 0,
});

const blankPattern = (isDefault: boolean): PatternDraft => ({
  key: newDraftKey('p'),
  name: '',
  is_default: isDefault,
  sort_order: 0,
  work_center_ids: [],
  shifts: [blankShift()],
});

/** The draft as the PUT body without a version -- what "unchanged" is compared on. */
const documentKey = (draft: CalendarDraft | null) => (draft ? JSON.stringify(buildCalendarUpdate(draft, 0)) : '');

export default function ProductionCalendarTab() {
  const { user } = useAuth();
  const { showToast } = useToast();
  const canEdit = canEditProductionCalendar(user);
  const canSync = canSyncWorkingCalendars(user);

  const [calendar, setCalendar] = useState<ProductionCalendar | null>(null);
  const [draft, setDraft] = useState<CalendarDraft | null>(null);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [workCentersFailed, setWorkCentersFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [staleOpen, setStaleOpen] = useState(false);

  const [syncResult, setSyncResult] = useState<WorkingCalendarSyncResult | null>(null);
  const [syncPreviewing, setSyncPreviewing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [confirmCommit, setConfirmCommit] = useState(false);
  const [committing, setCommitting] = useState(false);

  const applyCalendar = useCallback((next: ProductionCalendar) => {
    setCalendar(next);
    setDraft(draftFromCalendar(next));
    setSaveError(null);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    setSyncResult(null);
    setSyncError(null);
    const [calendarRes, workCentersRes] = await Promise.allSettled([
      api.getProductionCalendar(),
      api.getWorkCenters(false),
    ]);
    if (calendarRes.status === 'fulfilled') {
      applyCalendar(calendarRes.value);
    } else {
      setLoadError(true);
    }
    if (workCentersRes.status === 'fulfilled') {
      setWorkCenters(Array.isArray(workCentersRes.value) ? workCentersRes.value : []);
      setWorkCentersFailed(false);
    } else {
      setWorkCenters([]);
      setWorkCentersFailed(true);
    }
    setLoading(false);
  }, [applyCalendar]);

  useEffect(() => {
    void load();
  }, [load]);

  const baselineKey = useMemo(() => (calendar ? documentKey(draftFromCalendar(calendar)) : ''), [calendar]);
  const isDirty = !!draft && documentKey(draft) !== baselineKey;
  const { confirmDiscard } = useUnsavedChanges(isDirty);
  const issues = useMemo(() => (draft ? draftIssues(draft) : []), [draft]);

  // Work centers: active ones, plus any inactive one a pattern already names.
  const memberIds = useMemo(
    () => new Set((draft?.patterns ?? []).flatMap(pattern => pattern.work_center_ids)),
    [draft]
  );
  const listedWorkCenters = useMemo(
    () => workCenters.filter(wc => wc.is_active || memberIds.has(wc.id)).sort((a, b) => a.code.localeCompare(b.code)),
    [workCenters, memberIds]
  );
  const workCenterLabel = useMemo(() => {
    const byId = new Map(workCenters.map(wc => [wc.id, wc]));
    return (id: number) => byId.get(id)?.code ?? `WC #${id}`;
  }, [workCenters]);

  // ---- draft edits -------------------------------------------------------------------

  const updatePattern = (key: string, patch: Partial<PatternDraft>) =>
    setDraft(current =>
      current ? { ...current, patterns: current.patterns.map(p => (p.key === key ? { ...p, ...patch } : p)) } : current
    );

  const setDefaultPattern = (key: string) =>
    setDraft(current =>
      current ? { ...current, patterns: current.patterns.map(p => ({ ...p, is_default: p.key === key })) } : current
    );

  const removePattern = (key: string) =>
    setDraft(current => (current ? { ...current, patterns: current.patterns.filter(p => p.key !== key) } : current));

  const addPattern = () =>
    setDraft(current =>
      current
        ? { ...current, patterns: [...current.patterns, blankPattern(!current.patterns.some(p => p.is_default))] }
        : current
    );

  const toggleWorkCenter = (patternKey: string, workCenterId: number, checked: boolean) =>
    setDraft(current =>
      current
        ? {
            ...current,
            patterns: current.patterns.map(p =>
              p.key !== patternKey
                ? p
                : {
                    ...p,
                    work_center_ids: checked
                      ? [...p.work_center_ids.filter(id => id !== workCenterId), workCenterId]
                      : p.work_center_ids.filter(id => id !== workCenterId),
                  }
            ),
          }
        : current
    );

  const updateShift = (patternKey: string, shiftKey: string, patch: Partial<ShiftDraft>) =>
    setDraft(current =>
      current
        ? {
            ...current,
            patterns: current.patterns.map(p =>
              p.key !== patternKey
                ? p
                : { ...p, shifts: p.shifts.map(s => (s.key === shiftKey ? { ...s, ...patch } : s)) }
            ),
          }
        : current
    );

  const toggleShiftDay = (patternKey: string, shift: ShiftDraft, day: number, checked: boolean) =>
    updateShift(patternKey, shift.key, {
      days: checked
        ? [...shift.days.filter(d => d !== day), day].sort((a, b) => a - b)
        : shift.days.filter(d => d !== day),
    });

  const addShift = (patternKey: string) =>
    setDraft(current =>
      current
        ? {
            ...current,
            patterns: current.patterns.map(p =>
              p.key === patternKey ? { ...p, shifts: [...p.shifts, blankShift()] } : p
            ),
          }
        : current
    );

  const removeShift = (patternKey: string, shiftKey: string) =>
    setDraft(current =>
      current
        ? {
            ...current,
            patterns: current.patterns.map(p =>
              p.key === patternKey ? { ...p, shifts: p.shifts.filter(s => s.key !== shiftKey) } : p
            ),
          }
        : current
    );

  const discardChanges = () => {
    if (!calendar || !confirmDiscard()) return;
    applyCalendar(calendar);
  };

  // ---- save (server-gated, non-optimistic) --------------------------------------------

  const save = async () => {
    if (!calendar || !draft || !canEdit || saving || issues.length > 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await api.updateProductionCalendar(buildCalendarUpdate(draft, calendar.version));
      applyCalendar(saved);
      setSyncResult(null);
      setSyncError(null);
      showToast(
        'success',
        `Production calendar saved (version ${saved.version}). Run the working-calendar sync to update scheduling hours.`
      );
    } catch (err) {
      const detail = apiErrorDetail(err, 'Failed to save the production calendar');
      if (apiErrorStatus(err) === 409) {
        setStaleOpen(true);
      } else {
        setSaveError(detail);
      }
      showToast('error', detail);
    } finally {
      setSaving(false);
    }
  };

  // ---- working-calendar sync: dry run, then confirmed commit ---------------------------

  const previewSync = async () => {
    if (!calendar || !canSync || syncPreviewing) return;
    setSyncPreviewing(true);
    setSyncError(null);
    try {
      setSyncResult(await api.syncWorkingCalendarsFromShifts({ dry_run: true, expected_version: calendar.version }));
    } catch (err) {
      const detail = apiErrorDetail(err, 'Failed to preview the working-calendar sync');
      setSyncResult(null);
      setSyncError(detail);
      showToast('error', detail);
    } finally {
      setSyncPreviewing(false);
    }
  };

  const commitSync = async () => {
    if (!syncResult || !syncResult.dry_run || !canSync || committing) return;
    setCommitting(true);
    setSyncError(null);
    try {
      const result = await api.syncWorkingCalendarsFromShifts({
        dry_run: false,
        expected_version: syncResult.calendar_version,
      });
      setSyncResult(result);
      setConfirmCommit(false);
      showToast(
        'success',
        `Working calendars updated for ${result.changed_count} work center${result.changed_count === 1 ? '' : 's'}.`
      );
    } catch (err) {
      const detail = apiErrorDetail(err, 'Failed to sync working calendars');
      setConfirmCommit(false);
      if (apiErrorStatus(err) === 409) {
        // The calendar changed since the dry run: the preview no longer describes the write.
        setSyncResult(null);
        setSyncError(`${detail} Run the preview again.`);
      } else {
        setSyncError(detail);
      }
      showToast('error', detail);
    } finally {
      setCommitting(false);
    }
  };

  // ---- render ---------------------------------------------------------------------------

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  if (loadError || !calendar || !draft) {
    return <ErrorState message="Could not load the production calendar." onRetry={() => void load()} />;
  }

  const inputsDisabled = !canEdit || saving;
  const today = calendar.today;

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
              <CalendarDaysIcon className="h-5 w-5 text-werco-600" aria-hidden="true" />
              Production Calendar
            </h3>
            <p className="text-xs text-surface-500 mt-0.5 max-w-3xl">
              When a production day starts and which shifts run where. A shift that runs past midnight belongs to the
              day it started. OEE, the wallboard, the daily-output and employee-time reports, downtime filters and
              job-cost labor dates count work by production day.
            </p>
          </div>
          <div className="text-xs text-surface-500 font-mono text-right" data-testid="calendar-version">
            {calendar.configured ? (
              <>
                v{calendar.version}
                {calendar.updated_at ? ` · saved ${formatCentralDateTime(calendar.updated_at)}` : ''}
              </>
            ) : (
              'Not configured'
            )}
            <div>Clock: {calendar.timezone}</div>
          </div>
        </div>

        {!calendar.configured && (
          <p className="rounded-sm border border-fd-line bg-fd-sunken px-3 py-2 text-xs text-surface-600">
            Nothing is configured yet: a production day is the calendar day in {calendar.timezone} and there are no
            shifts. Set the day start and shifts below, then save.
          </p>
        )}

        {calendar.configured && (
          <p className="text-xs text-surface-600" data-testid="calendar-today">
            <span className="text-surface-500">Today&apos;s production day </span>
            <span className="font-mono">{today.production_day}</span>
            {today.occurrences.length > 0 ? (
              <>
                {': '}
                {today.occurrences
                  .map(occ => `${occ.code} ${formatCentralTime(occ.start_utc)}–${formatCentralTime(occ.end_utc)}`)
                  .join(' · ')}
              </>
            ) : (
              ': no shifts'
            )}
          </p>
        )}

        {!canEdit && (
          <div className="flex items-start gap-2 rounded-sm border border-surface-200 bg-surface-50 px-3 py-2 text-xs text-surface-500">
            <LockClosedIcon className="h-4 w-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
            <span>Only an Administrator can change the production calendar. It is shown here read-only.</span>
          </div>
        )}
      </section>

      <section className="grid grid-cols-1 sm:grid-cols-3 gap-4 max-w-2xl">
        <FormField
          label="Production day starts"
          required
          help="Local wall-clock time. Every shift must start at or after it and end by the next day's start."
        >
          {field => (
            <input
              {...field}
              type="time"
              className="input"
              value={draft.production_day_start}
              disabled={inputsDisabled}
              onChange={e => setDraft({ ...draft, production_day_start: e.target.value })}
            />
          )}
        </FormField>
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-surface-600">Shift patterns</h4>
          {canEdit && (
            <button type="button" className="btn-secondary btn-sm" onClick={addPattern} disabled={saving}>
              <PlusIcon className="h-4 w-4 mr-1" aria-hidden="true" />
              Add pattern
            </button>
          )}
        </div>

        {draft.patterns.length === 0 && (
          <p className="text-sm text-surface-500">
            No shift patterns. Add one — the default pattern covers every work center not named in another pattern.
          </p>
        )}

        {draft.patterns.map((pattern, patternIndex) => {
          const patternLabel = pattern.name.trim() || `Pattern ${patternIndex + 1}`;
          const ownerOf = (wcId: number) =>
            draft.patterns.find(other => other.key !== pattern.key && other.work_center_ids.includes(wcId));
          return (
            <div
              key={pattern.key}
              className="rounded-sm border border-surface-200 bg-fd-panel p-3 space-y-3"
              data-testid={`pattern-${patternIndex}`}
            >
              <div className="flex flex-wrap items-end gap-3">
                <FormField label="Pattern name" required className="min-w-[14rem] flex-1">
                  {field => (
                    <input
                      {...field}
                      type="text"
                      className="input"
                      maxLength={50}
                      value={pattern.name}
                      disabled={inputsDisabled}
                      onChange={e => updatePattern(pattern.key, { name: e.target.value })}
                    />
                  )}
                </FormField>
                <label className="flex items-center gap-2 pb-2 text-sm text-surface-700">
                  <input
                    type="radio"
                    name="production-calendar-default-pattern"
                    className="radio radio-sm"
                    checked={pattern.is_default}
                    disabled={inputsDisabled}
                    onChange={() => setDefaultPattern(pattern.key)}
                  />
                  Default pattern
                </label>
                {canEdit && (
                  <button
                    type="button"
                    className="btn-ghost btn-sm text-red-500 mb-1"
                    onClick={() => removePattern(pattern.key)}
                    disabled={saving}
                    aria-label={`Remove pattern ${patternLabel}`}
                  >
                    <TrashIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
              </div>

              <fieldset className="space-y-1">
                <legend className="text-xs font-medium text-surface-600">
                  Work centers{' '}
                  <span className="font-normal text-surface-500">
                    {pattern.is_default
                      ? '— plus every work center not in another pattern'
                      : `— ${pattern.work_center_ids.length} selected`}
                  </span>
                </legend>
                {workCentersFailed && (
                  <p className="text-xs text-amber-600">
                    Work centers could not be loaded; members show by id.{' '}
                    {pattern.work_center_ids.map(workCenterLabel).join(', ')}
                  </p>
                )}
                <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-x-3 gap-y-1">
                  {listedWorkCenters.map(wc => {
                    const other = ownerOf(wc.id);
                    const checked = pattern.work_center_ids.includes(wc.id);
                    return (
                      <label
                        key={wc.id}
                        className={`flex items-center gap-1.5 text-xs ${other ? 'text-surface-400' : 'text-surface-700'}`}
                        title={other ? `${wc.name} — in ${other.name.trim() || 'another pattern'}` : wc.name}
                      >
                        <input
                          type="checkbox"
                          className="checkbox checkbox-xs"
                          checked={checked}
                          disabled={inputsDisabled || !!other}
                          onChange={e => toggleWorkCenter(pattern.key, wc.id, e.target.checked)}
                        />
                        <span className="font-mono">{wc.code}</span>
                        {!wc.is_active && <span className="text-surface-400">(inactive)</span>}
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              <div className="overflow-x-auto">
                <table className="table table-compact text-xs">
                  <thead>
                    <tr>
                      <th>Code</th>
                      <th>Name</th>
                      <th>Start</th>
                      <th>End</th>
                      <th>Length</th>
                      <th>Days</th>
                      <th>Planned min</th>
                      <th>Active</th>
                      <th aria-label="Shift actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {pattern.shifts.length === 0 && (
                      <tr>
                        <td colSpan={9} className="text-surface-500">
                          No shifts in this pattern.
                        </td>
                      </tr>
                    )}
                    {pattern.shifts.map((shift, shiftIndex) => {
                      const shiftLabel = shift.code.trim() || `${patternLabel} shift ${shiftIndex + 1}`;
                      const span = shiftSpan(shift.start, shift.end);
                      const length = span ? span.endMinute - span.startMinute : null;
                      // The server's `ends_next_day`: past midnight, not merely AT it.
                      const crossesMidnight = !!span && span.endMinute > MINUTES_PER_DAY;
                      return (
                        <tr key={shift.key} className={shift.is_active ? '' : 'opacity-60'}>
                          <td>
                            <input
                              type="text"
                              className="input input-sm w-20 font-mono"
                              maxLength={20}
                              value={shift.code}
                              disabled={inputsDisabled}
                              aria-label={`Code for ${shiftLabel}`}
                              onChange={e => updateShift(pattern.key, shift.key, { code: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              className="input input-sm w-36"
                              maxLength={50}
                              value={shift.name}
                              disabled={inputsDisabled}
                              aria-label={`Name for ${shiftLabel}`}
                              onChange={e => updateShift(pattern.key, shift.key, { name: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="time"
                              className="input input-sm w-28"
                              value={shift.start}
                              disabled={inputsDisabled}
                              aria-label={`Start for ${shiftLabel}`}
                              onChange={e => updateShift(pattern.key, shift.key, { start: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="time"
                              className="input input-sm w-28"
                              value={shift.end}
                              disabled={inputsDisabled}
                              aria-label={`End for ${shiftLabel}`}
                              onChange={e => updateShift(pattern.key, shift.key, { end: e.target.value })}
                            />
                            {crossesMidnight && <div className="text-[10px] text-surface-500">next day</div>}
                          </td>
                          <td className="tabular-nums whitespace-nowrap">{formatLength(length)}</td>
                          <td>
                            <div className="flex gap-0.5" role="group" aria-label={`Days for ${shiftLabel}`}>
                              {WEEKDAY_SHORT.map((dayLabel, day) => (
                                <label
                                  key={dayLabel}
                                  className="flex flex-col items-center text-[10px] text-surface-500"
                                  title={WEEKDAY_LONG[day]}
                                >
                                  <span aria-hidden="true">{dayLabel.charAt(0)}</span>
                                  <input
                                    type="checkbox"
                                    className="checkbox checkbox-xs"
                                    checked={shift.days.includes(day)}
                                    disabled={inputsDisabled}
                                    aria-label={`${WEEKDAY_LONG[day]} for ${shiftLabel}`}
                                    onChange={e => toggleShiftDay(pattern.key, shift, day, e.target.checked)}
                                  />
                                </label>
                              ))}
                            </div>
                            <div className="text-[10px] text-surface-500 mt-0.5">{formatShiftDays(shift.days)}</div>
                          </td>
                          <td>
                            <input
                              type="number"
                              className="input input-sm w-20 tabular-nums"
                              min={1}
                              max={1440}
                              value={Number.isFinite(shift.planned_minutes) ? shift.planned_minutes : ''}
                              disabled={inputsDisabled}
                              aria-label={`Planned minutes for ${shiftLabel}`}
                              onChange={e =>
                                updateShift(pattern.key, shift.key, { planned_minutes: parseInt(e.target.value, 10) })
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="checkbox"
                              className="checkbox checkbox-xs"
                              checked={shift.is_active}
                              disabled={inputsDisabled}
                              aria-label={`${shiftLabel} active`}
                              onChange={e => updateShift(pattern.key, shift.key, { is_active: e.target.checked })}
                            />
                          </td>
                          <td>
                            {canEdit && (
                              <button
                                type="button"
                                className="btn-ghost btn-xs text-red-500"
                                onClick={() => removeShift(pattern.key, shift.key)}
                                disabled={saving}
                                aria-label={`Remove ${shiftLabel}`}
                              >
                                <TrashIcon className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {canEdit && (
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <button
                    type="button"
                    className="btn-secondary btn-xs"
                    onClick={() => addShift(pattern.key)}
                    disabled={saving || pattern.shifts.length >= 12}
                  >
                    <PlusIcon className="h-3.5 w-3.5 mr-1" aria-hidden="true" />
                    Add shift
                  </button>
                  <p className="text-[11px] text-surface-500">
                    A shift used by OEE records can&apos;t be removed or re-coded — clear Active to retire it.
                  </p>
                </div>
              )}
            </div>
          );
        })}
      </section>

      {canEdit && (
        <section className="space-y-2 border-t border-surface-200 pt-4">
          {issues.length > 0 && isDirty && (
            <ul className="rounded-sm border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-600 space-y-0.5">
              {issues.map(issue => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          )}
          {saveError && (
            <p role="alert" className="rounded-sm border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-500">
              {saveError}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-end gap-2">
            {isDirty && <span className="text-xs text-amber-600 mr-auto">Unsaved changes</span>}
            <button type="button" className="btn-secondary" onClick={discardChanges} disabled={!isDirty || saving}>
              Discard changes
            </button>
            <LoadingButton
              type="button"
              variant="primary"
              loading={saving}
              loadingText="Saving…"
              disabled={!isDirty || issues.length > 0}
              onClick={() => void save()}
            >
              Save calendar
            </LoadingButton>
          </div>
        </section>
      )}

      {canSync && (
        <section className="space-y-3 border-t border-surface-200 pt-4" aria-labelledby="working-calendar-sync-heading">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h4
                id="working-calendar-sync-heading"
                className="text-xs font-semibold uppercase tracking-wide text-surface-600"
              >
                Sync working calendars
              </h4>
              <p className="text-xs text-surface-500 mt-0.5 max-w-3xl">
                Sets each covered work center&apos;s weekly scheduling hours (Mon–Sun) to the planned minutes of the
                active shifts that start each day. Dated overrides are kept. Scheduling, capacity and delivery
                projections move with the new hours — review the preview before committing.
              </p>
            </div>
            <LoadingButton
              type="button"
              variant="secondary"
              size="sm"
              loading={syncPreviewing}
              loadingText="Previewing…"
              disabled={isDirty || !calendar.configured || committing}
              onClick={() => void previewSync()}
            >
              <ArrowPathIcon className="h-4 w-4 mr-1" aria-hidden="true" />
              Preview sync
            </LoadingButton>
          </div>
          {(isDirty || !calendar.configured) && (
            <p className="text-xs text-surface-500">
              {isDirty
                ? 'Save the calendar first — the sync uses the saved calendar.'
                : 'Save a calendar with shifts before syncing.'}
            </p>
          )}
          {syncError && (
            <p role="alert" className="rounded-sm border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-500">
              {syncError}
            </p>
          )}
          {syncResult && <SyncTable result={syncResult} />}
          {syncResult?.dry_run && (
            <div className="flex items-center justify-end gap-2">
              <span className="text-xs text-surface-500 mr-auto">
                {syncResult.changed_count} of {syncResult.rows.length} work center
                {syncResult.rows.length === 1 ? '' : 's'} change.
              </span>
              <LoadingButton
                type="button"
                variant="primary"
                size="sm"
                loading={committing}
                disabled={syncResult.changed_count === 0}
                onClick={() => setConfirmCommit(true)}
              >
                Commit {syncResult.changed_count} change{syncResult.changed_count === 1 ? '' : 's'}
              </LoadingButton>
            </div>
          )}
        </section>
      )}

      <ConfirmDialog
        open={staleOpen}
        title="Production calendar changed"
        message="Someone saved the production calendar after you loaded it. Reload to see the saved version — your unsaved edits here will be discarded."
        confirmLabel="Reload"
        cancelLabel="Keep editing"
        variant="warning"
        onConfirm={() => {
          setStaleOpen(false);
          void load();
        }}
        onCancel={() => setStaleOpen(false)}
      />

      <ConfirmDialog
        open={confirmCommit}
        title="Write new working hours?"
        message={`Write the new weekly hours to ${syncResult?.changed_count ?? 0} work center${
          syncResult?.changed_count === 1 ? '' : 's'
        }? Scheduling, capacity and delivery projections for them move with the new hours. Dated overrides are kept.`}
        confirmLabel="Commit"
        variant="warning"
        pending={committing}
        onConfirm={() => void commitSync()}
        onCancel={() => setConfirmCommit(false)}
      />
    </div>
  );
}

function HoursCell({ current, next }: { current: number; next: number }) {
  if (current === next) return <span className="tabular-nums">{formatHours(next)}</span>;
  return (
    <span className="tabular-nums whitespace-nowrap">
      <span className="text-surface-400 line-through">{formatHours(current)}</span>{' '}
      <span className="font-semibold text-werco-600">{formatHours(next)}</span>
    </span>
  );
}

function SyncTable({ result }: { result: WorkingCalendarSyncResult }) {
  if (result.rows.length === 0) {
    return (
      <p className="flex items-center gap-2 text-xs text-surface-500">
        <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
        No active work center is covered by a shift pattern.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="table table-compact text-xs" aria-label={result.dry_run ? 'Sync preview' : 'Sync result'}>
        <thead>
          <tr>
            <th>Work center</th>
            <th>Pattern</th>
            {WEEKDAY_SHORT.map(day => (
              <th key={day} className="text-right">
                {day}
              </th>
            ))}
            <th className="text-right">Overrides kept</th>
            <th>{result.dry_run ? 'Change' : 'Result'}</th>
          </tr>
        </thead>
        <tbody>
          {result.rows.map(row => (
            <tr key={row.work_center_id} className={row.changed ? '' : 'opacity-60'}>
              <td className="whitespace-nowrap">
                <span className="font-mono">{row.work_center_code}</span>{' '}
                <span className="text-surface-500">{row.work_center_name}</span>
              </td>
              <td className="whitespace-nowrap">{row.pattern_name}</td>
              {row.new_weekly_hours.map((hours, day) => (
                <td key={WEEKDAY_SHORT[day]} className="text-right">
                  <HoursCell current={row.current_weekly_hours[day] ?? 0} next={hours} />
                </td>
              ))}
              <td className="text-right tabular-nums">{row.overrides_kept}</td>
              <td className="whitespace-nowrap">
                {!row.changed
                  ? 'No change'
                  : result.dry_run
                    ? row.current_version === 0
                      ? 'New calendar'
                      : 'Changes'
                    : 'Written'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import {
  checklistResultsPayload,
  completionChecklist,
  openWorkOrdersBySchedule,
  parseChecklistResults,
} from '../maintenance';
import {
  scheduleEditForm,
  scheduleEditPayload,
  scheduleEditProblem,
} from '../../components/maintenance/EditScheduleDialog';
import type { MaintenanceSchedule } from '../../types/maintenance';

describe('maintenance checklist results', () => {
  it('opens a work order with every non-blank line unticked', () => {
    expect(JSON.parse(checklistResultsPayload('  Grease \n\nCheck belt\n') as string)).toEqual([
      { item: 'Grease', done: false },
      { item: 'Check belt', done: false },
    ]);
    expect(checklistResultsPayload(' \n ')).toBeNull();
  });

  it('reads the [{item, done}] shape, a plain string array, and one item per line', () => {
    expect(parseChecklistResults('[{"item":"Grease","done":true},{"item":"Belt"}]')).toEqual([
      { item: 'Grease', done: true },
      { item: 'Belt', done: false },
    ]);
    expect(parseChecklistResults('["Grease", " ", "Belt"]')).toEqual([
      { item: 'Grease', done: false },
      { item: 'Belt', done: false },
    ]);
    expect(parseChecklistResults('Grease\nBelt')).toEqual([
      { item: 'Grease', done: false },
      { item: 'Belt', done: false },
    ]);
    expect(parseChecklistResults(null)).toEqual([]);
    expect(parseChecklistResults('')).toEqual([]);
  });
});

describe('openWorkOrdersBySchedule', () => {
  it('maps each schedule to its open work order and ignores closed and unscheduled ones', () => {
    const map = openWorkOrdersBySchedule([
      { id: 1, schedule_id: 7, status: 'completed' },
      { id: 2, schedule_id: 7, status: 'on_hold' },
      { id: 3, schedule_id: 8, status: 'cancelled' },
      { id: 4, schedule_id: null, status: 'scheduled' },
    ]);
    expect(map.get(7)?.id).toBe(2);
    expect(map.has(8)).toBe(false);
    expect(map.size).toBe(1);
  });
});

describe('schedule edit payload', () => {
  const schedule = {
    id: 7,
    name: 'PM',
    maintenance_type: 'preventive',
    frequency: 'monthly',
    frequency_days: null,
    estimated_duration_hours: 2,
    priority: 'medium',
    checklist_items: ['A', 'B'],
    requires_shutdown: false,
    is_active: true,
    work_center_id: 1,
    asset_id: null,
    target_type: 'work_center',
    next_due_date: '2026-12-01',
  } as MaintenanceSchedule;

  it('is empty until something changes, and whitespace-only checklist edits are not a change', () => {
    const before = scheduleEditForm(schedule);
    expect(scheduleEditPayload(before, before)).toEqual({});
    expect(scheduleEditPayload(before, { ...before, checklist: ' A \n\nB\n' })).toEqual({});
    expect(scheduleEditPayload(before, { ...before, estimated_duration_hours: '2.0' })).toEqual({});
  });

  it('sends a cleared checklist as null', () => {
    const before = scheduleEditForm(schedule);
    expect(scheduleEditPayload(before, { ...before, checklist: '' })).toEqual({ checklist: null });
  });

  it('sends frequency_days only for a custom cadence', () => {
    const before = scheduleEditForm({ ...schedule, frequency: 'custom', frequency_days: 45 });
    expect(scheduleEditPayload(before, { ...before, frequency_days: '60' })).toEqual({ frequency_days: 60 });
    expect(scheduleEditPayload(before, { ...before, frequency: 'annual' })).toEqual({ frequency: 'annual' });
  });

  it('refuses a missing due date, a custom cadence without days, and a negative duration', () => {
    const form = scheduleEditForm(schedule);
    expect(scheduleEditProblem(form)).toBeNull();
    expect(scheduleEditProblem({ ...form, next_due_date: '' })).toMatch(/Next due date/);
    expect(scheduleEditProblem({ ...form, frequency: 'custom', frequency_days: '' })).toMatch(/Every N days/);
    expect(scheduleEditProblem({ ...form, frequency: 'custom', frequency_days: '2.5' })).toMatch(/Every N days/);
    expect(scheduleEditProblem({ ...form, estimated_duration_hours: '-1' })).toMatch(/duration/);
    expect(scheduleEditProblem({ ...form, estimated_duration_hours: '' })).toMatch(/duration/);
  });
});

describe('completionChecklist', () => {
  it('appends, unticked, the schedule items the work order no longer carries', () => {
    expect(completionChecklist([{ item: 'Grease', done: true }], ['Grease', 'Check belt', ' ', 'Check oil'])).toEqual([
      { item: 'Grease', done: true },
      { item: 'Check belt', done: false },
      { item: 'Check oil', done: false },
    ]);
  });

  it('counts repeated schedule items as a multiset, like the server', () => {
    expect(completionChecklist([{ item: 'Grease bearing', done: true }], ['Grease bearing', 'Grease bearing'])).toEqual(
      [
        { item: 'Grease bearing', done: true },
        { item: 'Grease bearing', done: false },
      ]
    );
  });

  it('leaves a work order with no schedule checklist as it is', () => {
    const items = [{ item: 'Drain tank', done: false }];
    expect(completionChecklist(items)).toEqual(items);
    expect(completionChecklist(items, [])).toEqual(items);
  });
});

import {
  carryChecklistTicks,
  checklistPayload,
  checklistResultsPayload,
  completionChecklist,
  completionNotesPayload,
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

  it('normalizes every item it reads (bullets, whitespace runs) and keeps numbering and case', () => {
    expect(
      parseChecklistResults(
        '[{"item":"- Grease  bearing","done":true},{"item":"1.  Check oil"},"\u2022 Drain\u00a0tank",{"item":" - "}]'
      )
    ).toEqual([
      { item: 'Grease bearing', done: true },
      { item: '1. Check oil', done: false },
      { item: 'Drain tank', done: false },
    ]);
    expect(parseChecklistResults('* Grease\r\n-40 F check\n  -  ')).toEqual([
      { item: 'Grease', done: false },
      { item: '-40 F check', done: false },
    ]);
  });

  it('writes normalized items, one per non-blank line', () => {
    const typed = '\u2022 Drain  tank\r\n1. Check oil\n - \n';
    expect(JSON.parse(checklistPayload(typed) as string)).toEqual(['Drain tank', '1. Check oil']);
    expect(JSON.parse(checklistResultsPayload(typed) as string)).toEqual([
      { item: 'Drain tank', done: false },
      { item: '1. Check oil', done: false },
    ]);
    expect(checklistPayload('- \n*')).toBeNull();
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

  it('treats a bullet or spacing edit as no change, and a change of case as an edit', () => {
    const before = scheduleEditForm({ ...schedule, checklist_items: ['Check oil', 'Drain tank'] });
    expect(scheduleEditPayload(before, { ...before, checklist: '\u2022 Check  oil\n- Drain tank' })).toEqual({});
    expect(scheduleEditPayload(before, { ...before, checklist: 'Check Oil\nDrain tank' })).toEqual({
      checklist: JSON.stringify(['Check Oil', 'Drain tank']),
    });
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

  it('matches schedule steps by normalized, case-insensitive text, showing the work order’s spelling', () => {
    expect(
      completionChecklist(
        [
          { item: 'Grease bearing', done: true },
          { item: '1. Check oil', done: false },
        ],
        ['grease  bearing', '- 1. CHECK OIL', '\u2022 Bleed lines']
      )
    ).toEqual([
      { item: 'Grease bearing', done: true },
      { item: '1. Check oil', done: false },
      { item: 'Bleed lines', done: false },
    ]);
  });

  it('still counts copies across spellings: two on the work order and three on the schedule make three', () => {
    expect(
      completionChecklist(
        [
          { item: 'Grease bearing', done: true },
          { item: 'GREASE BEARING', done: false },
        ],
        ['grease bearing', 'Grease Bearing', '- grease bearing']
      )
    ).toEqual([
      { item: 'Grease bearing', done: true },
      { item: 'GREASE BEARING', done: false },
      // The added copy is named as the server names it: the first spelling met.
      { item: 'Grease bearing', done: false },
    ]);
  });

  it('leaves a work order with no schedule checklist as it is', () => {
    const items = [{ item: 'Drain tank', done: false }];
    expect(completionChecklist(items)).toEqual(items);
    expect(completionChecklist(items, [])).toEqual(items);
  });
});

describe('carryChecklistTicks', () => {
  const ticked = (item: string) => ({ item, done: true });
  const open = (item: string) => ({ item, done: false });

  it('keeps the ticks by item text and counts the items the dialog did not list', () => {
    expect(
      carryChecklistTicks(
        [ticked('Drain tank'), ticked('Check belt')],
        [open('Drain tank'), open('Check belt'), open('Check oil')]
      )
    ).toEqual({ checklist: [ticked('Drain tank'), ticked('Check belt'), open('Check oil')], added: 1 });
  });

  it('matches repeated items as a multiset: two ticked copies stay ticked when a third arrives', () => {
    expect(
      carryChecklistTicks(
        [ticked('Grease bearing'), ticked('Grease bearing')],
        [open('Grease bearing'), open('Grease bearing'), open('Grease bearing')]
      )
    ).toEqual({ checklist: [ticked('Grease bearing'), ticked('Grease bearing'), open('Grease bearing')], added: 1 });
  });

  it('matches ticked copies first, so an unticked twin never takes a tick', () => {
    expect(carryChecklistTicks([open('Grease bearing'), ticked('Grease bearing')], [open('Grease bearing')])).toEqual({
      checklist: [ticked('Grease bearing')],
      added: 0,
    });
  });

  it('keeps the dialog’s answer on a matched item and the server’s on a new one', () => {
    expect(carryChecklistTicks([open('Drain tank')], [ticked('Drain tank'), ticked('Check oil')])).toEqual({
      checklist: [open('Drain tank'), ticked('Check oil')],
      added: 0,
    });
  });

  it('counts as added only the new items still to answer, never one the server holds ticked', () => {
    expect(
      carryChecklistTicks(
        [ticked('Drain tank')],
        [open('Drain tank'), ticked('Check oil'), open('Bleed lines'), open('Bleed lines')]
      )
    ).toEqual({
      checklist: [ticked('Drain tank'), ticked('Check oil'), open('Bleed lines'), open('Bleed lines')],
      added: 2,
    });
  });

  it('drops items the fresh list no longer carries and reports nothing new', () => {
    expect(carryChecklistTicks([ticked('Drain tank'), ticked('Check belt')], [open('Check belt')])).toEqual({
      checklist: [ticked('Check belt')],
      added: 0,
    });
  });

  it('carries a tick across a difference of case or spacing, in the fresh list’s spelling', () => {
    expect(
      carryChecklistTicks(
        [ticked('grease  bearing'), ticked('1. check OIL')],
        [open('Grease Bearing'), open('1. Check oil')]
      )
    ).toEqual({
      checklist: [ticked('Grease Bearing'), ticked('1. Check oil')],
      added: 0,
    });
  });

  it('does not change the lists it is given', () => {
    const previous = [ticked('Drain tank')];
    const fresh = [open('Drain tank')];
    carryChecklistTicks(previous, fresh);
    expect(previous).toEqual([ticked('Drain tank')]);
    expect(fresh).toEqual([open('Drain tank')]);
  });
});

describe('completionNotesPayload', () => {
  it('omits notes the technician left as they opened (the server keeps them)', () => {
    expect(completionNotesPayload('Belt squeals', 'Belt squeals')).toEqual({});
    expect(completionNotesPayload('  Belt squeals \n', 'Belt squeals')).toEqual({});
    expect(completionNotesPayload('', '')).toEqual({});
  });

  it('sends changed notes trimmed, replacing the work order’s', () => {
    expect(completionNotesPayload('  Belt replaced  ', 'Belt squeals')).toEqual({ notes: 'Belt replaced' });
    expect(completionNotesPayload('First note', '')).toEqual({ notes: 'First note' });
  });

  it('sends null for emptied notes, which clears them', () => {
    expect(completionNotesPayload('', 'Belt squeals')).toEqual({ notes: null });
    expect(completionNotesPayload('   ', 'Belt squeals')).toEqual({ notes: null });
  });
});

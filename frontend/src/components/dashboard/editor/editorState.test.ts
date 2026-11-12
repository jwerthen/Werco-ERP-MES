/**
 * The edit-mode reducer: one Undo step per real change, a pointer drag is one step (or
 * none), a cancel never touches the draft, history is bounded, and a reload starts over.
 */
import { moveWidgetTo, removeWidget, setPanelSize, type LayoutDraft } from '../dashboardLayout';
import {
  HISTORY_LIMIT,
  applySession,
  editorReducer,
  initialEditorCore,
  movedLabel,
  undoLabel,
  type EditorAction,
  type EditorCore,
} from './editorState';

const DRAFT: LayoutDraft = {
  banners: ['attention'],
  kpis: ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs'],
  panels: [
    { id: 'capacity_overview', size: 'wide' },
    { id: 'presence', size: 'narrow' },
    { id: 'recent_completions', size: 'full' },
  ],
};

const run = (core: EditorCore, ...actions: EditorAction[]) => actions.reduce(editorReducer, core);

describe('apply', () => {
  it('records the previous draft and the label for a real change', () => {
    const core = initialEditorCore(DRAFT);
    const next = removeWidget(DRAFT, 'kpi', 'kpi.due_today');
    const after = editorReducer(core, { type: 'apply', next, label: 'removed Due Today' });
    expect(after.draft).toBe(next);
    expect(after.past).toEqual([{ draft: DRAFT, label: 'removed Due Today' }]);
    expect(undoLabel(after)).toBe('removed Due Today');
  });

  it('accepts an updater, applied to the current draft', () => {
    const core = run(initialEditorCore(DRAFT), {
      type: 'apply',
      next: draft => removeWidget(draft, 'kpi', 'kpi.overdue'),
      label: 'removed Overdue',
    });
    const after = editorReducer(core, {
      type: 'apply',
      next: draft => removeWidget(draft, 'kpi', 'kpi.open_ncrs'),
      label: 'removed Open NCRs',
    });
    expect(after.draft.kpis).toEqual(['kpi.due_today']);
    expect(after.past.map(entry => entry.label)).toEqual(['removed Overdue', 'removed Open NCRs']);
  });

  it('folds an apply with the same coalesce key into the last step, labelled with the latest change', () => {
    const resize = (size: 'third' | 'half' | 'full' | 'narrow'): EditorAction => ({
      type: 'apply',
      next: draft => setPanelSize(draft, 'presence', size),
      label: `Presence width ${size}`,
      coalesceKey: 'resize:presence:1',
    });
    const after = run(initialEditorCore(DRAFT), resize('third'), resize('half'), resize('full'));
    expect(after.draft.panels.find(panel => panel.id === 'presence')?.size).toBe('full');
    expect(after.past).toHaveLength(1);
    expect(after.past[0].draft).toBe(DRAFT);
    expect(undoLabel(after)).toBe('Presence width full');
    expect(editorReducer(after, { type: 'undo' }).draft).toBe(DRAFT);

    // Folding back to where the step started drops the step.
    const back = run(after, resize('narrow'));
    expect(back.past).toHaveLength(0);
    expect(back.draft).toBe(DRAFT);
  });

  it('never folds across another step or a different key', () => {
    const resize = (size: 'third' | 'half', key: string): EditorAction => ({
      type: 'apply',
      next: draft => setPanelSize(draft, 'presence', size),
      label: `Presence width ${size}`,
      coalesceKey: key,
    });
    const otherVisit = run(
      initialEditorCore(DRAFT),
      resize('third', 'resize:presence:1'),
      resize('half', 'resize:presence:2')
    );
    expect(otherVisit.past).toHaveLength(2);
    const between = run(
      initialEditorCore(DRAFT),
      resize('third', 'resize:presence:1'),
      { type: 'apply', next: draft => removeWidget(draft, 'kpi', 'kpi.overdue'), label: 'removed Overdue' },
      resize('half', 'resize:presence:1')
    );
    expect(between.past.map(entry => entry.label)).toEqual([
      'Presence width third',
      'removed Overdue',
      'Presence width half',
    ]);
  });

  it('returns the same state for a no-op (same draft object)', () => {
    const core = initialEditorCore(DRAFT);
    expect(editorReducer(core, { type: 'apply', next: DRAFT, label: 'nothing' })).toBe(core);
    expect(
      editorReducer(core, { type: 'apply', next: draft => setPanelSize(draft, 'presence', 'narrow'), label: 'x' })
    ).toBe(core);
  });
});

describe('pointer drag session', () => {
  it('starts from the zone order and renders the live order for that zone only', () => {
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'sessionStart', zone: 'panel', activeId: 'presence' },
      { type: 'sessionReorder', zone: 'panel', order: ['presence', 'capacity_overview', 'recent_completions'] }
    );
    expect(core.session).toEqual({
      zone: 'panel',
      activeId: 'presence',
      startOrder: ['capacity_overview', 'presence', 'recent_completions'],
      order: ['presence', 'capacity_overview', 'recent_completions'],
    });
    // The draft is untouched until the drop.
    expect(core.draft).toBe(DRAFT);
    expect(core.past).toEqual([]);
    const rendered = applySession(core.draft, core.session);
    expect(rendered.panels).toEqual([
      { id: 'presence', size: 'narrow' },
      { id: 'capacity_overview', size: 'wide' },
      { id: 'recent_completions', size: 'full' },
    ]);
    expect(rendered.kpis).toBe(DRAFT.kpis);
    expect(rendered.banners).toBe(DRAFT.banners);
  });

  it('applySession is the same draft without a session or with an unchanged order', () => {
    expect(applySession(DRAFT, null)).toBe(DRAFT);
    const started = editorReducer(initialEditorCore(DRAFT), {
      type: 'sessionStart',
      zone: 'kpi',
      activeId: 'kpi.overdue',
    });
    expect(applySession(started.draft, started.session)).toBe(DRAFT);
  });

  it('commits a session as ONE history entry, however many live reorders it had', () => {
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'sessionStart', zone: 'kpi', activeId: 'kpi.overdue' },
      { type: 'sessionReorder', zone: 'kpi', order: ['kpi.due_today', 'kpi.overdue', 'kpi.open_ncrs'] },
      { type: 'sessionReorder', zone: 'kpi', order: ['kpi.due_today', 'kpi.open_ncrs', 'kpi.overdue'] },
      { type: 'sessionEnd', title: 'Overdue' }
    );
    expect(core.session).toBeNull();
    expect(core.draft.kpis).toEqual(['kpi.due_today', 'kpi.open_ncrs', 'kpi.overdue']);
    expect(core.past).toEqual([{ draft: DRAFT, label: movedLabel('Overdue') }]);
    expect(undoLabel(core)).toBe('moved Overdue');
  });

  it('adds no history for a drop at the starting order (dragged away and back)', () => {
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'sessionStart', zone: 'kpi', activeId: 'kpi.overdue' },
      { type: 'sessionReorder', zone: 'kpi', order: ['kpi.due_today', 'kpi.overdue', 'kpi.open_ncrs'] },
      { type: 'sessionReorder', zone: 'kpi', order: ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs'] },
      { type: 'sessionEnd', title: 'Overdue' }
    );
    expect(core.session).toBeNull();
    expect(core.draft).toBe(DRAFT);
    expect(core.past).toEqual([]);
  });

  it('cancel drops the live order and leaves the draft and history untouched', () => {
    const withHistory = run(initialEditorCore(DRAFT), {
      type: 'apply',
      next: draft => removeWidget(draft, 'panel', 'recent_completions'),
      label: 'removed Recent Completions',
    });
    const core = run(
      withHistory,
      { type: 'sessionStart', zone: 'panel', activeId: 'presence' },
      { type: 'sessionReorder', zone: 'panel', order: ['presence', 'capacity_overview'] },
      { type: 'sessionCancel' }
    );
    expect(core.session).toBeNull();
    expect(core.draft).toBe(withHistory.draft);
    expect(core.past).toBe(withHistory.past);
    // Cancelling with no session changes nothing.
    expect(editorReducer(core, { type: 'sessionCancel' })).toBe(core);
  });

  it('ignores a reorder that changes membership, names the other zone or repeats the live order', () => {
    const started = editorReducer(initialEditorCore(DRAFT), {
      type: 'sessionStart',
      zone: 'kpi',
      activeId: 'kpi.overdue',
    });
    expect(
      editorReducer(started, { type: 'sessionReorder', zone: 'kpi', order: ['kpi.overdue', 'kpi.due_today'] })
    ).toBe(started);
    expect(
      editorReducer(started, {
        type: 'sessionReorder',
        zone: 'kpi',
        order: ['kpi.overdue', 'kpi.due_today', 'kpi.signed_in'],
      })
    ).toBe(started);
    expect(
      editorReducer(started, {
        type: 'sessionReorder',
        zone: 'panel',
        order: ['presence', 'capacity_overview', 'recent_completions'],
      })
    ).toBe(started);
    expect(
      editorReducer(started, {
        type: 'sessionReorder',
        zone: 'kpi',
        order: ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs'],
      })
    ).toBe(started);
  });

  it('ignores a pickup of a widget that is not in the zone, and reorders or drops without a session', () => {
    const core = initialEditorCore(DRAFT);
    expect(editorReducer(core, { type: 'sessionStart', zone: 'kpi', activeId: 'presence' })).toBe(core);
    expect(
      editorReducer(core, {
        type: 'sessionReorder',
        zone: 'kpi',
        order: ['kpi.due_today', 'kpi.overdue', 'kpi.open_ncrs'],
      })
    ).toBe(core);
    expect(editorReducer(core, { type: 'sessionEnd', title: 'Overdue' })).toBe(core);
  });
});

describe('undo', () => {
  it('restores the previous draft, one step at a time, and is a no-op when empty', () => {
    const first = moveWidgetTo(DRAFT, 'kpi', 'kpi.open_ncrs', 0);
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'apply', next: first, label: 'moved Open NCRs' },
      { type: 'apply', next: draft => setPanelSize(draft, 'presence', 'third'), label: 'resized Presence' }
    );
    const once = editorReducer(core, { type: 'undo' });
    expect(once.draft).toBe(first);
    expect(undoLabel(once)).toBe('moved Open NCRs');
    const twice = editorReducer(once, { type: 'undo' });
    expect(twice.draft).toBe(DRAFT);
    expect(undoLabel(twice)).toBeNull();
    expect(editorReducer(twice, { type: 'undo' })).toBe(twice);
  });

  it(`is bounded at ${HISTORY_LIMIT} steps and keeps every label in order`, () => {
    expect(HISTORY_LIMIT).toBe(50);
    const drafts: LayoutDraft[] = [DRAFT];
    let core = initialEditorCore(DRAFT);
    for (let step = 1; step <= HISTORY_LIMIT + 10; step += 1) {
      const next = { ...core.draft, banners: [`banner_${step}`] };
      drafts.push(next);
      core = editorReducer(core, { type: 'apply', next, label: `step ${step}` });
    }
    expect(core.past).toHaveLength(HISTORY_LIMIT);
    expect(core.past[0].label).toBe('step 11');
    for (let step = HISTORY_LIMIT + 10; step > 10; step -= 1) {
      expect(undoLabel(core)).toBe(`step ${step}`);
      core = editorReducer(core, { type: 'undo' });
      expect(core.draft).toBe(drafts[step - 1]);
    }
    // The oldest ten steps fell off: nothing further to undo.
    expect(undoLabel(core)).toBeNull();
    expect(core.draft).toBe(drafts[10]);
  });

  it('ends a running session', () => {
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'apply', next: draft => removeWidget(draft, 'kpi', 'kpi.open_ncrs'), label: 'removed Open NCRs' },
      { type: 'sessionStart', zone: 'kpi', activeId: 'kpi.overdue' },
      { type: 'undo' }
    );
    expect(core.session).toBeNull();
    expect(core.draft).toBe(DRAFT);
  });
});

describe('replace', () => {
  it('starts over: new draft, no history, no session', () => {
    const reloaded: LayoutDraft = { banners: [], kpis: ['kpi.signed_in'], panels: [] };
    const core = run(
      initialEditorCore(DRAFT),
      { type: 'apply', next: draft => removeWidget(draft, 'kpi', 'kpi.overdue'), label: 'removed Overdue' },
      { type: 'sessionStart', zone: 'panel', activeId: 'presence' },
      { type: 'replace', draft: reloaded }
    );
    expect(core).toEqual({ draft: reloaded, past: [], session: null });
    expect(core.draft).toBe(reloaded);
  });
});

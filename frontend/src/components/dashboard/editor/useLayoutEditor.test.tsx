/**
 * useLayoutEditor: the edit session's lifecycle (scope tagging, generation guard, read-only
 * refusal), every verb's draft change / announcement / Undo step, and the non-optimistic
 * save, reset and reload with their 409 recovery. Ports the former Customize dialog's cases
 * that were about the draft rather than the markup (§10.4 #5, #7, #8, #10, #16, #17, #20).
 */
import React from 'react';
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import { createMemoryRouter, Link, Outlet, RouterProvider } from 'react-router-dom';
import { UnsavedChangesProvider } from '../../../context/UnsavedChangesContext';
import type { DashboardLayoutValues } from '../../../types/dashboardLayout';
import type { Permission } from '../../../utils/permissions';
import {
  createFakeLayout,
  EditorHarness,
  layoutResponse,
  useFakeLayout,
  type FakeLayoutController,
} from '../../../test-utils/layoutEditorHarness';
import { buildSavePayload, draftFromLayout } from '../dashboardLayout';
import { DashboardLayoutCompanyChangedError } from '../useDashboardLayout';
import { CLASSIC_LAYOUT_VALUES, DASHBOARD_REGISTRY, WIDGETS_BY_ID } from '../widgetRegistry';
import type { WidgetEligibilityContext } from '../widgetTypes';
import { POSITION_ANNOUNCE_DEBOUNCE_MS } from './dragPlanning';
import * as copy from './editorCopy';
import { HISTORY_LIMIT } from './editorState';
import { useLayoutEditor, type LayoutEditor } from './useLayoutEditor';

jest.mock('../../../services/api', () => ({ __esModule: true, default: {} }));

const ctxFor = (permissions: Permission[] | 'all', isAdmin = false): WidgetEligibilityContext => ({
  can: permission => permissions === 'all' || permissions.includes(permission),
  hasRole: () => isAdmin,
  isAdmin,
});
const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'receiving:view', 'shipping:view']);
const SHIPPING = ctxFor(['work_orders:view', 'inventory:view', 'shipping:view']);

const httpError = (status: number, detail: string) =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

interface SetupOptions {
  layout?: FakeLayoutController;
  scope?: string | null;
  eligibility?: WidgetEligibilityContext;
}

interface RenderProps {
  scope: string | null;
  eligibility: WidgetEligibilityContext;
}

function setup({ layout = createFakeLayout(), scope = 'scope-a', eligibility = MANAGER }: SetupOptions = {}) {
  const showToast = jest.fn();
  const announce = jest.fn();
  const onBeforeChange = jest.fn();
  /** Every render's (scope, active, draft) — to prove what a single render showed. */
  const renders: { scope: string | null; active: boolean; hasDraft: boolean }[] = [];
  const utils = renderHook(
    ({ scope: currentScope, eligibility: currentEligibility }: RenderProps) => {
      const result = useFakeLayout(layout);
      const editor = useLayoutEditor({
        scope: currentScope,
        layout: result,
        eligibility: currentEligibility,
        showToast,
        announce,
        onBeforeChange,
      });
      renders.push({ scope: currentScope, active: editor.active, hasDraft: editor.draft !== null });
      return editor;
    },
    { initialProps: { scope, eligibility } }
  );
  const editor = () => utils.result.current;
  const enter = () => act(() => void editor().enter());
  return { ...utils, layout, showToast, announce, onBeforeChange, renders, editor, enter };
}

const kpis = (editor: LayoutEditor) => editor.draft?.kpis ?? [];
const panels = (editor: LayoutEditor) => editor.draft?.panels.map(panel => panel.id) ?? [];
const lastAnnouncement = (announce: jest.Mock) => announce.mock.calls.at(-1)?.[0];

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('entering', () => {
  it('seeds the draft once from the saved layout and announces the session', () => {
    const { editor, enter, announce } = setup();
    expect(editor().active).toBe(false);
    expect(editor().draft).toBeNull();
    enter();
    expect(editor().active).toBe(true);
    expect(editor().draft).toEqual(draftFromLayout(null, MANAGER));
    expect(editor().isDirty).toBe(false);
    expect(editor().canUndo).toBe(false);
    expect(announce).toHaveBeenLastCalledWith(copy.ENTER_ANNOUNCEMENT);
  });

  it('is not re-seeded when the layout result or the eligibility changes identity', () => {
    const { editor, enter, rerender, layout } = setup();
    enter();
    act(() => editor().remove('kpi.overdue'));
    const draft = editor().draft;
    // A fresh eligibility object and a re-emitted (same-content) layout response.
    rerender({ scope: 'scope-a', eligibility: { ...MANAGER } });
    act(() => layout.set({ response: { ...layout.getSnapshot().response! } }));
    expect(editor().draft).toBe(draft);
    expect(kpis(editor())).not.toContain('kpi.overdue');
  });

  it('refuses a read-only session with the info toast and never enters', () => {
    const { editor, enter, showToast } = setup({ layout: createFakeLayout({ canEdit: false }) });
    enter();
    expect(editor().active).toBe(false);
    expect(showToast).toHaveBeenCalledWith('info', copy.READ_ONLY_LAYOUT_NOTICE);
  });

  it('refuses silently while the layout is not ready, unless handed the re-read response', () => {
    const layout = createFakeLayout({ status: 'error' });
    const { editor, showToast } = setup({ layout });
    let entered = true;
    act(() => {
      entered = editor().enter();
    });
    expect(entered).toBe(false);
    expect(showToast).not.toHaveBeenCalled();

    const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.due_today' }] };
    act(() => {
      entered = editor().enter(layoutResponse(saved));
    });
    expect(entered).toBe(true);
    expect(kpis(editor())).toEqual(['kpi.due_today']);
  });
});

describe('editing verbs', () => {
  it('moves by one place and to the ends, announcing positions and the edges', () => {
    const { editor, enter, announce, onBeforeChange } = setup();
    enter();
    act(() => editor().moveBy('kpi.overdue', 1));
    expect(kpis(editor()).slice(0, 2)).toEqual(['kpi.idle_operators', 'kpi.overdue']);
    expect(lastAnnouncement(announce)).toBe('Overdue, position 2 of 10.');
    expect(onBeforeChange).toHaveBeenCalledTimes(1);

    act(() => editor().moveTo('kpi.overdue', 'end'));
    expect(kpis(editor()).at(-1)).toBe('kpi.overdue');
    expect(lastAnnouncement(announce)).toBe('Overdue, position 10 of 10.');

    act(() => editor().moveBy('kpi.overdue', 1));
    expect(lastAnnouncement(announce)).toBe('Overdue is already last.');
    act(() => editor().moveTo('kpi.idle_operators', 'start'));
    expect(lastAnnouncement(announce)).toBe('Signed In, Idle is already first.');
    // Edge presses change nothing: no FLIP capture, no Undo step.
    expect(onBeforeChange).toHaveBeenCalledTimes(2);
    expect(editor().canUndo).toBe(true);
  });

  it('removes, adds at the end at the default width, and resizes — each one Undo step', () => {
    jest.useFakeTimers();
    const { editor, enter, announce } = setup();
    enter();
    act(() => editor().remove('capacity_overview'));
    expect(panels(editor())).not.toContain('capacity_overview');
    expect(lastAnnouncement(announce)).toBe(copy.removedMessage('Capacity Overview'));

    let message: string | null = null;
    act(() => {
      message = editor().add(WIDGETS_BY_ID.get('quality_pulse')!);
    });
    expect(message).toBe('Quality Pulse added at position 5 of 5.');
    expect(editor().draft?.panels.at(-1)).toEqual({ id: 'quality_pulse', size: 'half' });
    // Adding what is already there is not a step.
    act(() => {
      message = editor().add(WIDGETS_BY_ID.get('quality_pulse')!);
    });
    expect(message).toBeNull();

    act(() => editor().setPanelSize('live_activity', 'third'));
    expect(editor().draft?.panels.find(panel => panel.id === 'live_activity')?.size).toBe('third');
    // A width is announced once it has settled (the radios check each width as you arrow).
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS));
    expect(lastAnnouncement(announce)).toBe('Live Shop Activity width: Third.');

    act(() => editor().undo());
    expect(lastAnnouncement(announce)).toBe('Undone: Live Shop Activity width Third.');
    act(() => editor().undo());
    expect(lastAnnouncement(announce)).toBe('Undone: added Quality Pulse.');
    act(() => editor().undo());
    expect(lastAnnouncement(announce)).toBe('Undone: removed Capacity Overview.');
    expect(editor().isDirty).toBe(false);
    expect(editor().canUndo).toBe(false);
    // Nothing left: a no-op, no announcement.
    const calls = announce.mock.calls.length;
    act(() => editor().undo());
    expect(announce.mock.calls).toHaveLength(calls);
  });

  it('makes every width chosen in one Move-menu visit ONE Undo step, and a new visit a new step', () => {
    jest.useFakeTimers();
    const { editor, enter, announce } = setup();
    enter();
    const sizeOf = () => editor().draft?.panels.find(panel => panel.id === 'presence')?.size;
    const before = sizeOf();
    act(() => editor().openMenu('presence'));
    act(() => editor().setPanelSize('presence', 'third'));
    act(() => editor().setPanelSize('presence', 'half'));
    act(() => editor().setPanelSize('presence', 'full'));
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS));
    expect(announce.mock.calls.filter(([message]) => String(message).includes('width:'))).toEqual([
      ['Signed In Right Now width: Full width.'],
    ]);
    act(() => editor().closeMenu());
    act(() => editor().openMenu('presence'));
    act(() => editor().setPanelSize('presence', 'third'));
    act(() => editor().undo());
    expect(sizeOf()).toBe('full');
    expect(lastAnnouncement(announce)).toBe('Undone: Signed In Right Now width Third.');
    act(() => editor().undo());
    expect(sizeOf()).toBe(before);
    expect(editor().canUndo).toBe(false);
  });

  it('keeps at most HISTORY_LIMIT Undo steps', () => {
    const { editor, enter } = setup();
    enter();
    for (let step = 0; step < HISTORY_LIMIT + 5; step += 1) {
      act(() => editor().moveBy('kpi.overdue', step % 2 === 0 ? 1 : -1));
    }
    let undone = 0;
    while (editor().canUndo) {
      act(() => editor().undo());
      undone += 1;
    }
    expect(undone).toBe(HISTORY_LIMIT);
  });

  it('is clean again once a banner is toggled off and back on, and saves banners in registry order', async () => {
    const { editor, enter, layout } = setup();
    enter();
    act(() => editor().setBannerVisible('attention', false));
    expect(editor().isDirty).toBe(true);
    act(() => editor().setBannerVisible('attention', true));
    expect(editor().isDirty).toBe(false);

    act(() => editor().setBannerVisible('alert_chips', false));
    act(() => editor().setBannerVisible('attention', false));
    act(() => editor().setBannerVisible('attention', true));
    await act(async () => {
      await editor().done();
    });
    const ids = layout.save.mock.calls[0][0].widgets.map(widget => widget.id);
    expect(ids.slice(0, 3)).toEqual(['attention', 'setup_nudge', 'morning_brief']);
    expect(ids).not.toContain('alert_chips');
  });

  it('announces a banner Undo by what that toggle did (hid, then showed)', () => {
    const { editor, enter, announce } = setup();
    enter();
    act(() => editor().setBannerVisible('attention', false));
    act(() => editor().setBannerVisible('attention', true));
    act(() => editor().undo());
    expect(lastAnnouncement(announce)).toBe('Undone: showed Needs attention.');
    expect(editor().draft?.banners).not.toContain('attention');
    act(() => editor().undo());
    expect(lastAnnouncement(announce)).toBe('Undone: hid Needs attention.');
    expect(editor().draft?.banners).toContain('attention');
  });

  it("exposes a layout key that changes with geometry, and the committed draft's needed sources", () => {
    const { editor, enter } = setup();
    expect(editor().layoutKey).toBe('');
    enter();
    const initial = editor().layoutKey;
    expect(editor().draftNeededSources).toContain('capacity');
    act(() => editor().setPanelSize('presence', 'third'));
    const resized = editor().layoutKey;
    expect(resized).not.toBe(initial);
    act(() => editor().moveBy('kpi.overdue', 1));
    expect(editor().layoutKey).not.toBe(resized);
    act(() => editor().remove('capacity_overview'));
    expect(editor().draftNeededSources).not.toContain('capacity');
    // A live drag reorders but never changes membership, so the sources stay as committed.
    const sources = editor().draftNeededSources;
    act(() => editor().dragStart('panel', 'presence'));
    act(() => editor().dragReorder('panel', [...editor().orders.panel].reverse()));
    expect(editor().draftNeededSources).toEqual(sources);
  });

  it('describes a widget for the drag layer (title and width label)', () => {
    const { editor, enter } = setup();
    enter();
    expect(editor().describe('capacity_overview')).toEqual({ title: 'Capacity Overview', sizeLabel: 'Wide' });
    expect(editor().describe('kpi.overdue')).toEqual({ title: 'Overdue', sizeLabel: copy.KPI_SIZE_LABEL });
  });
});

describe('FLIP capture (spec §5.5): once before every committed change, never for a no-op', () => {
  it('captures for remove, add, width, banner and undo', () => {
    const { editor, enter, onBeforeChange } = setup();
    enter();
    const captures = () => onBeforeChange.mock.calls.length;
    act(() => editor().remove('kpi.overdue'));
    expect(captures()).toBe(1);
    act(() => {
      editor().add(WIDGETS_BY_ID.get('quality_pulse')!);
    });
    expect(captures()).toBe(2);
    act(() => editor().setPanelSize('presence', 'third'));
    expect(captures()).toBe(3);
    act(() => editor().setBannerVisible('attention', false));
    expect(captures()).toBe(4);
    act(() => editor().undo());
    expect(captures()).toBe(5);
    // No-ops capture nothing.
    act(() => editor().remove('kpi.overdue'));
    act(() => editor().setPanelSize('presence', 'third'));
    act(() => editor().setBannerVisible('attention', true));
    expect(captures()).toBe(5);
  });

  it('captures for a drag cancel that moved something, and not for one that did not', () => {
    const { editor, enter, onBeforeChange } = setup();
    enter();
    act(() => editor().dragStart('panel', 'presence'));
    act(() => editor().dragCancel('panel', 'presence'));
    expect(onBeforeChange).not.toHaveBeenCalled();

    act(() => editor().dragStart('panel', 'presence'));
    act(() => editor().dragReorder('panel', [...editor().orders.panel].reverse()));
    expect(onBeforeChange).toHaveBeenCalledTimes(1);
    act(() => editor().dragCancel('panel', 'presence'));
    expect(onBeforeChange).toHaveBeenCalledTimes(2);
  });

  it('captures for a Cancel that discards a dirty draft, and not for a clean one', () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { editor, enter, onBeforeChange } = setup();
    enter();
    act(() => editor().requestCancel());
    expect(onBeforeChange).not.toHaveBeenCalled();
    enter();
    act(() => editor().remove('kpi.overdue'));
    act(() => editor().requestCancel());
    expect(onBeforeChange).toHaveBeenCalledTimes(2);
  });

  it('captures once a reset succeeds', async () => {
    const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
    const { editor, enter, onBeforeChange } = setup({ layout: createFakeLayout({ saved }) });
    enter();
    act(() => editor().requestReset());
    await act(async () => {
      await editor().reset();
    });
    expect(onBeforeChange).toHaveBeenCalledTimes(1);
  });
});

describe('pointer drag sessions', () => {
  it('renders the live order, commits it as ONE Undo step and announces pickup, position and drop', () => {
    jest.useFakeTimers();
    const { editor, enter, announce, onBeforeChange } = setup();
    enter();
    const start = editor().orders.panel;
    act(() => editor().dragStart('panel', 'capacity_overview'));
    expect(editor().dragging).toEqual({ zone: 'panel', id: 'capacity_overview' });
    expect(lastAnnouncement(announce)).toBe('Picked up Capacity Overview, position 1 of 5.');

    const live = ['live_activity', 'work_center_status', 'capacity_overview', 'presence', 'recent_completions'];
    act(() => editor().dragReorder('panel', live));
    // The render shows the live order; the committed draft does not have it yet.
    expect(editor().orders.panel).toEqual(live);
    expect(panels(editor())).toEqual(start);
    expect(onBeforeChange).toHaveBeenCalledTimes(1);
    // The live position is announced on a 500 ms trailing debounce.
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS - 1));
    expect(lastAnnouncement(announce)).toBe('Picked up Capacity Overview, position 1 of 5.');
    act(() => jest.advanceTimersByTime(1));
    expect(lastAnnouncement(announce)).toBe('Capacity Overview, position 3 of 5.');

    act(() => editor().dragEnd('panel', 'capacity_overview'));
    expect(editor().dragging).toBeNull();
    expect(panels(editor())).toEqual(live);
    expect(lastAnnouncement(announce)).toBe('Capacity Overview dropped at position 3 of 5.');
    act(() => editor().undo());
    expect(panels(editor())).toEqual(start);
    expect(lastAnnouncement(announce)).toBe('Undone: moved Capacity Overview.');
  });

  it.each([
    ['a save is in flight', 'save'],
    ['a reset is in flight', 'reset'],
    ['a stale write needs a reload', 'conflict'],
  ])('refuses to start a drag while %s', async (_label, state) => {
    const saved: DashboardLayoutValues = {
      format_version: 1,
      widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.due_today' }, { id: 'presence' }, { id: 'live_activity' }],
    };
    const layout = createFakeLayout({ saved });
    if (state === 'save') layout.save.mockImplementation(() => new Promise(() => undefined));
    if (state === 'reset') layout.reset.mockImplementation(() => new Promise(() => undefined));
    if (state === 'conflict') layout.save.mockRejectedValue(httpError(409, 'Changed in another tab.'));
    const { editor, enter, announce } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.due_today'));
    if (state === 'reset') {
      act(() => editor().requestReset());
      act(() => {
        void editor().reset();
      });
    } else {
      await act(async () => {
        void editor().done();
      });
    }
    expect(editor().controlsDisabled).toBe(true);
    const draft = editor().draft;
    const calls = announce.mock.calls.length;

    act(() => editor().dragStart('panel', 'presence'));
    act(() => editor().dragReorder('panel', ['live_activity', 'presence']));
    act(() => editor().dragEnd('panel', 'presence'));
    expect(editor().dragging).toBeNull();
    expect(editor().draft).toBe(draft);
    expect(editor().orders.panel).toEqual(['presence', 'live_activity']);
    expect(announce.mock.calls).toHaveLength(calls);
  });

  it('a drop at the starting place adds no Undo step, and a cancel restores the order', () => {
    jest.useFakeTimers();
    const { editor, enter, announce } = setup();
    enter();
    const start = editor().orders.kpi;
    act(() => editor().dragStart('kpi', 'kpi.overdue'));
    act(() => editor().dragEnd('kpi', 'kpi.overdue'));
    expect(lastAnnouncement(announce)).toBe('Overdue stays at position 1 of 10.');
    expect(editor().canUndo).toBe(false);

    act(() => editor().dragStart('kpi', 'kpi.overdue'));
    act(() => editor().dragReorder('kpi', [...start.slice(1, 3), 'kpi.overdue', ...start.slice(3)]));
    act(() => editor().dragCancel('kpi', 'kpi.overdue'));
    // The cancelled reorder's debounced position is never spoken.
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS * 2));
    expect(lastAnnouncement(announce)).toBe('Move cancelled. Overdue is back at position 1 of 10.');
    expect(editor().orders.kpi).toEqual(start);
    expect(editor().canUndo).toBe(false);
  });

  it('a pickup closes the Move menu and silences a width still settling; a quick drop silences the live position', () => {
    jest.useFakeTimers();
    const { editor, enter, announce } = setup();
    enter();
    act(() => editor().openMenu('presence'));
    act(() => editor().setPanelSize('presence', 'third'));
    act(() => editor().dragStart('panel', 'capacity_overview'));
    expect(editor().menuFor).toBeNull();
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS * 2));
    expect(lastAnnouncement(announce)).toBe('Picked up Capacity Overview, position 1 of 5.');

    // Dropped inside the debounce window: the drop is the last word, not a stale position.
    act(() => editor().dragReorder('panel', [...editor().orders.panel].reverse()));
    act(() => editor().dragEnd('panel', 'capacity_overview'));
    act(() => jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS * 2));
    expect(lastAnnouncement(announce)).toBe('Capacity Overview dropped at position 5 of 5.');
  });
});

describe('Done', () => {
  it('leaves without a request when nothing changed', async () => {
    const { editor, enter, layout, announce } = setup();
    enter();
    await act(async () => {
      await editor().done();
    });
    expect(layout.save).not.toHaveBeenCalled();
    expect(editor().active).toBe(false);
    expect(announce).toHaveBeenLastCalledWith(copy.DONE_UNCHANGED_ANNOUNCEMENT);
  });

  it('saves exactly buildSavePayload(draft, saved, eligibility), locking the controls while it runs', async () => {
    const layout = createFakeLayout();
    const pendingSave = deferred<ReturnType<typeof layoutResponse>>();
    layout.save.mockImplementation(() => pendingSave.promise);
    const { editor, enter, showToast } = setup({ layout });
    enter();
    act(() => editor().setPanelSize('capacity_overview', 'full'));
    act(() => editor().setBannerVisible('morning_brief', false));
    const draft = editor().draft!;
    act(() => {
      void editor().done();
    });
    expect(layout.save).toHaveBeenCalledTimes(1);
    const body = layout.save.mock.calls[0][0];
    expect(body).toEqual(buildSavePayload(draft, null, MANAGER));
    expect(body.widgets).toContainEqual({ id: 'capacity_overview', size: 'full' });
    expect(body.widgets.map(widget => widget.id)).not.toContain('morning_brief');
    expect(editor().pending).toBe('save');
    expect(editor().controlsDisabled).toBe(true);
    // Locked: no edit lands while the write is in flight.
    act(() => editor().remove('kpi.overdue'));
    expect(kpis(editor())).toContain('kpi.overdue');

    await act(async () => pendingSave.resolve(layoutResponse(body, { version: 4 })));
    expect(editor().active).toBe(false);
    expect(showToast).toHaveBeenCalledWith('success', copy.SAVED_TOAST);
  });

  it('carries saved widgets the viewer cannot see through a save', async () => {
    const saved: DashboardLayoutValues = {
      format_version: 1,
      widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.open_cars' }, { id: 'kpi.due_today' }],
    };
    const { editor, enter, layout } = setup({ layout: createFakeLayout({ saved }), eligibility: SHIPPING });
    enter();
    expect(kpis(editor())).toEqual(['kpi.overdue', 'kpi.due_today']);
    expect(editor().carriedCount).toBe(1);
    act(() => editor().remove('kpi.due_today'));
    await act(async () => {
      await editor().done();
    });
    expect(layout.save.mock.calls[0][0].widgets.map(widget => widget.id)).toEqual(['kpi.overdue', 'kpi.open_cars']);
  });

  it.each([
    ['no saved layout', null],
    ['a saved layout that only carries the stock default', CLASSIC_LAYOUT_VALUES],
  ])('never counts a carried widget for a non-admin with %s', (_label, saved) => {
    const { editor, enter } = setup({ layout: createFakeLayout({ saved }) });
    enter();
    expect(editor().carriedCount).toBe(0);
  });

  it.each([
    ['a 409', httpError(409, 'Your dashboard layout changed in another tab. Reload it before saving.')],
    ['a save answered for another company', new DashboardLayoutCompanyChangedError()],
  ])('treats %s as stale: shows the detail and locks everything behind a reload', async (_label, failure) => {
    const layout = createFakeLayout();
    layout.save.mockRejectedValue(failure);
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    expect(editor().active).toBe(true);
    expect(editor().needsRefresh).toBe(true);
    expect(editor().controlsDisabled).toBe(true);
    expect(editor().error).toBe((failure as Error).message);
    expect(editor().pending).toBeNull();
    // A second Done is refused until the reload.
    await act(async () => {
      await editor().done();
    });
    expect(layout.save).toHaveBeenCalledTimes(1);
  });

  it('keeps the draft after a failure that is not a conflict, and a retry sends the same layout', async () => {
    const layout = createFakeLayout();
    layout.save.mockRejectedValueOnce(
      httpError(503, 'Your dashboard layout could not be saved because its audit record failed; nothing was changed.')
    );
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    expect(editor().error).toMatch(/audit record failed/);
    expect(editor().needsRefresh).toBe(false);
    expect(editor().controlsDisabled).toBe(false);
    expect(kpis(editor())).not.toContain('kpi.overdue');
    await act(async () => {
      await editor().done();
    });
    expect(layout.save).toHaveBeenCalledTimes(2);
    expect(layout.save.mock.calls[1][0]).toEqual(layout.save.mock.calls[0][0]);
    expect(editor().active).toBe(false);
  });
});

describe('reload after a conflict', () => {
  it('replaces the draft, clears history and sets the notice', async () => {
    const layout = createFakeLayout();
    layout.save.mockRejectedValue(httpError(409, 'Changed in another tab.'));
    const reloaded: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.due_today' }] };
    layout.reload.mockImplementation(async () => {
      const response = layoutResponse(reloaded, { version: 9 });
      layout.set({ response });
      return response;
    });
    const { editor, enter, onBeforeChange } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    const captures = onBeforeChange.mock.calls.length;
    await act(async () => {
      await editor().reload();
    });
    expect(kpis(editor())).toEqual(['kpi.due_today']);
    expect(editor().needsRefresh).toBe(false);
    expect(editor().error).toBe('');
    expect(editor().notice).toBe(copy.RELOADED_NOTICE);
    expect(editor().canUndo).toBe(false);
    expect(editor().isDirty).toBe(false);
    expect(onBeforeChange.mock.calls.length).toBe(captures + 1);
  });

  it('Cancel after a stale write re-reads the saved layout, so the next session starts current', async () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    const layout = createFakeLayout({ version: 3 });
    layout.save.mockRejectedValueOnce(httpError(409, 'Changed in another tab.'));
    layout.reload.mockImplementation(async () => {
      const response = layoutResponse(null, { version: 4 });
      layout.set({ response });
      return response;
    });
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    expect(editor().needsRefresh).toBe(true);
    await act(async () => {
      editor().requestCancel();
    });
    expect(editor().active).toBe(false);
    expect(layout.reload).toHaveBeenCalledTimes(1);
    expect(layout.getSnapshot().response?.version).toBe(4);

    // A plain Cancel (nothing stale) reads nothing.
    enter();
    act(() => editor().requestCancel());
    expect(layout.reload).toHaveBeenCalledTimes(1);
  });

  it('keeps the recovery state when the reload fails', async () => {
    const layout = createFakeLayout();
    layout.save.mockRejectedValue(httpError(409, 'Changed in another tab.'));
    layout.reload.mockResolvedValue(null);
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    await act(async () => {
      await editor().reload();
    });
    expect(editor().needsRefresh).toBe(true);
    expect(editor().error).toBe(copy.RELOAD_FAILED);
    expect(kpis(editor())).not.toContain('kpi.overdue');
  });
});

describe('Reset', () => {
  const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };

  it('resets through the confirmation and leaves', async () => {
    const { editor, enter, layout, showToast } = setup({ layout: createFakeLayout({ saved }) });
    enter();
    act(() => editor().requestReset());
    expect(editor().confirmResetOpen).toBe(true);
    await act(async () => {
      await editor().reset();
    });
    expect(layout.reset).toHaveBeenCalledTimes(1);
    expect(layout.save).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('success', copy.RESET_TOAST);
    expect(editor().active).toBe(false);
  });

  it('is unavailable on the stock default', () => {
    const { editor, enter } = setup();
    enter();
    expect(editor().hasSavedLayout).toBe(false);
    act(() => editor().requestReset());
    expect(editor().confirmResetOpen).toBe(false);
  });

  it('a 409 closes the confirmation and requires a reload', async () => {
    const layout = createFakeLayout({ saved });
    layout.reset.mockRejectedValue(httpError(409, 'Your dashboard layout changed in another tab.'));
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().requestReset());
    await act(async () => {
      await editor().reset();
    });
    expect(editor().confirmResetOpen).toBe(false);
    expect(editor().needsRefresh).toBe(true);
    expect(editor().active).toBe(true);
  });
});

describe('Cancel', () => {
  it('asks nothing when clean, and asks before discarding a dirty draft', () => {
    const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { editor, enter, announce } = setup();
    enter();
    act(() => editor().requestCancel());
    expect(confirm).not.toHaveBeenCalled();
    expect(editor().active).toBe(false);
    expect(announce).toHaveBeenLastCalledWith(copy.CANCELLED_ANNOUNCEMENT);

    enter();
    act(() => editor().remove('kpi.overdue'));
    act(() => editor().requestCancel());
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(editor().active).toBe(true);
    expect(kpis(editor())).not.toContain('kpi.overdue');

    confirm.mockReturnValue(true);
    act(() => editor().requestCancel());
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(editor().active).toBe(false);
  });

  it('cannot cancel while a save is in flight', () => {
    const layout = createFakeLayout();
    layout.save.mockImplementation(() => new Promise(() => undefined));
    const { editor, enter } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    act(() => {
      void editor().done();
    });
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    act(() => editor().requestCancel());
    expect(editor().active).toBe(true);
    expect(editor().pending).toBe('save');
  });
});

describe('the lock while Done saves', () => {
  const SAVED: DashboardLayoutValues = {
    format_version: 1,
    widgets: [
      { id: 'attention' },
      { id: 'kpi.overdue' },
      { id: 'kpi.due_today' },
      { id: 'presence', size: 'half' },
      { id: 'live_activity', size: 'half' },
    ],
  };

  /**
   * A session one change in (so Undo has a step) whose saves never answer. The change is a
   * panel removal so both KPIs stay: a KPI move then has somewhere to go, and a missing lock
   * shows up as a changed draft rather than only as an "already first" announcement.
   */
  const editingOneChangeIn = () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockImplementation(() => new Promise(() => undefined));
    const harness = setup({ layout });
    harness.enter();
    act(() => harness.editor().remove('live_activity'));
    expect(kpis(harness.editor())).toEqual(['kpi.overdue', 'kpi.due_today']);
    expect(panels(harness.editor())).toEqual(['presence']);
    return harness;
  };

  it.each<[string, (editor: LayoutEditor) => void]>([
    ['moveBy', editor => editor.moveBy('kpi.due_today', -1)],
    ['moveTo', editor => editor.moveTo('kpi.due_today', 'start')],
    ['remove', editor => editor.remove('kpi.due_today')],
    ['add', editor => void editor.add(WIDGETS_BY_ID.get('quality_pulse')!)],
    ['setPanelSize', editor => editor.setPanelSize('presence', 'third')],
    ['setBannerVisible', editor => editor.setBannerVisible('attention', false)],
    ['undo', editor => editor.undo()],
    ['openMenu', editor => editor.openMenu('presence')],
    ['openGallery', editor => editor.openGallery()],
    ['requestReset', editor => editor.requestReset()],
  ])('refuses %s: no draft change, no FLIP capture, nothing opened', (_verb, verb) => {
    // The control: with no save in flight the same call changes the draft or opens
    // something, so the refusal below is the lock's doing, not an edge no-op.
    const control = editingOneChangeIn();
    const controlDraft = control.editor().draft;
    act(() => verb(control.editor()));
    const acted =
      control.editor().draft !== controlDraft ||
      control.editor().menuFor !== null ||
      control.editor().galleryOpen ||
      control.editor().confirmResetOpen;
    expect(acted).toBe(true);
    control.unmount();

    const { editor, onBeforeChange, announce } = editingOneChangeIn();
    act(() => {
      void editor().done();
    });
    expect(editor().pending).toBe('save');
    const draft = editor().draft;
    const captures = onBeforeChange.mock.calls.length;
    const announcements = announce.mock.calls.length;

    act(() => verb(editor()));
    expect(editor().draft).toBe(draft);
    expect(editor().canUndo).toBe(true);
    expect(onBeforeChange).toHaveBeenCalledTimes(captures);
    expect(announce).toHaveBeenCalledTimes(announcements);
    expect(editor().menuFor).toBeNull();
    expect(editor().galleryOpen).toBe(false);
    expect(editor().confirmResetOpen).toBe(false);
    expect(editor().pending).toBe('save');
  });
});

describe('session changes', () => {
  it('reads as inactive in the very render the scope changes, warns about discarded edits and drops a late save', async () => {
    const layout = createFakeLayout();
    const pendingSave = deferred<ReturnType<typeof layoutResponse>>();
    layout.save.mockImplementation(() => pendingSave.promise);
    const { editor, enter, rerender, renders, showToast } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    act(() => {
      void editor().done();
    });
    expect(editor().pending).toBe('save');

    renders.length = 0;
    rerender({ scope: 'scope-b', eligibility: MANAGER });
    // The first render under the new scope already shows no session and no draft.
    expect(renders[0]).toEqual({ scope: 'scope-b', active: false, hasDraft: false });
    expect(editor().active).toBe(false);
    expect(showToast).toHaveBeenCalledWith('warning', copy.SESSION_CHANGED_TOAST);

    await act(async () => pendingSave.resolve(layoutResponse(null)));
    expect(showToast).not.toHaveBeenCalledWith('success', copy.SAVED_TOAST);
    expect(editor().active).toBe(false);
    // Returning to the first scope does not resurrect the old session.
    rerender({ scope: 'scope-a', eligibility: MANAGER });
    expect(editor().active).toBe(false);
  });

  it('does not warn when the discarded session was clean', () => {
    const { enter, rerender, showToast } = setup();
    enter();
    rerender({ scope: 'scope-b', eligibility: MANAGER });
    expect(showToast).not.toHaveBeenCalledWith('warning', expect.anything());
  });

  it('leaves edit mode with the read-only toast when canEdit turns false', () => {
    const { editor, enter, layout, showToast } = setup();
    enter();
    act(() => layout.set({ response: layoutResponse(null, { can_edit: false }) }));
    expect(editor().active).toBe(false);
    expect(showToast).toHaveBeenCalledWith('info', copy.READ_ONLY_LAYOUT_NOTICE);
  });

  it('refuses Done defensively when the layout it would write through cannot save', async () => {
    // Entered from an error-path re-read (`seed`) before this render's layout shows it: the
    // layout here still reads as not ready and not editable, so Done must not write.
    const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
    const layout = createFakeLayout({ status: 'error' });
    const { editor, showToast } = setup({ layout });
    act(() => void editor().enter(layoutResponse(saved)));
    act(() => editor().remove('kpi.overdue'));
    await act(async () => {
      await editor().done();
    });
    expect(layout.save).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('info', copy.READ_ONLY_LAYOUT_NOTICE);
    expect(editor().active).toBe(true);
  });

  it('refuses Reset defensively when the layout it would write through cannot save', async () => {
    // A saved layout is on hand (so Reset is meaningful), but this render's response can no
    // longer edit and is not settled yet (the read-only exit only runs on a ready layout).
    const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
    const layout = createFakeLayout({ saved });
    const { editor, enter, showToast } = setup({ layout });
    enter();
    act(() => layout.set({ status: 'loading', response: layoutResponse(saved, { can_edit: false }) }));
    expect(editor().active).toBe(true);
    await act(async () => {
      await editor().reset();
    });
    expect(layout.reset).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('info', copy.READ_ONLY_LAYOUT_NOTICE);
    expect(editor().active).toBe(true);
  });
});

describe('what the editor exposes for the bar and the gallery', () => {
  it('counts saved widgets this bundle does not know', () => {
    const saved: DashboardLayoutValues = {
      format_version: 1,
      widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.from_a_newer_bundle' }],
    };
    const { editor, enter } = setup({ layout: createFakeLayout({ saved }) });
    expect(editor().unknownCount).toBe(0);
    enter();
    expect(editor().unknownCount).toBe(1);
  });

  it('opens the gallery on a zone when asked (a placeholder), and at the top otherwise', () => {
    const { editor, enter } = setup();
    enter();
    act(() => editor().openGallery('panel'));
    expect(editor().galleryOpen).toBe(true);
    expect(editor().galleryFocusZone).toBe('panel');
    act(() => editor().closeGallery());
    expect(editor().galleryFocusZone).toBeNull();
    act(() => editor().openGallery());
    expect(editor().galleryFocusZone).toBeNull();
  });
});

describe('a save or reset that settles after the page unmounted', () => {
  it.each([
    ['a save that lands', 'save', true, ['success', copy.SAVED_TOAST]],
    ['a save that fails', 'save', false, ['error', copy.SAVE_FAILED_AFTER_LEAVING]],
    ['a reset that lands', 'reset', true, ['success', copy.RESET_TOAST]],
    ['a reset that fails', 'reset', false, ['error', copy.RESET_FAILED_AFTER_LEAVING]],
  ] as const)('still reports %s', async (_label, verb, succeeds, toast) => {
    const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
    const layout = createFakeLayout({ saved });
    const pending = deferred<ReturnType<typeof layoutResponse>>();
    layout.save.mockImplementation(() => pending.promise);
    layout.reset.mockImplementation(() => pending.promise);
    const { editor, enter, showToast, unmount } = setup({ layout });
    enter();
    act(() => editor().remove('kpi.overdue'));
    act(() => {
      if (verb === 'save') void editor().done();
      else {
        editor().requestReset();
      }
    });
    if (verb === 'reset') {
      act(() => {
        void editor().reset();
      });
    }
    unmount();
    await act(async () => {
      if (succeeds) pending.resolve(layoutResponse(null));
      else pending.reject(httpError(409, 'Changed in another tab.'));
    });
    expect(showToast).toHaveBeenLastCalledWith(...toast);
  });
});

describe('leaving the page while Done saves (DOM, data router)', () => {
  class RouterRequest {
    url: string;
    signal?: AbortSignal;
    method: string;
    constructor(url: string, init: RequestInit = {}) {
      this.url = url;
      this.signal = init.signal ?? undefined;
      this.method = init.method ?? 'GET';
    }
  }
  const realRequest = global.Request;
  beforeAll(() => {
    global.Request = RouterRequest as unknown as typeof Request;
  });
  afterAll(() => {
    global.Request = realRequest;
  });

  function renderRouted(layout: FakeLayoutController, showToast: jest.Mock) {
    const router = createMemoryRouter([
      {
        element: (
          <UnsavedChangesProvider>
            <Outlet />
          </UnsavedChangesProvider>
        ),
        children: [
          {
            path: '/',
            element: (
              <>
                <Link to="/other">Elsewhere</Link>
                <EditorHarness layout={layout} eligibility={MANAGER} showToast={showToast} />
              </>
            ),
          },
          { path: '/other', element: <p>Destination</p> },
        ],
      },
    ]);
    render(<RouterProvider router={router} />);
  }

  it.each([
    ['lands', true, ['success', copy.SAVED_TOAST]],
    ['fails', false, ['error', copy.SAVE_FAILED_AFTER_LEAVING]],
  ] as const)(
    'asks nothing (the edits are being saved, not discarded) and toasts a save that %s',
    async (_label, succeeds, toast) => {
      const layout = createFakeLayout();
      const pending = deferred<ReturnType<typeof layoutResponse>>();
      layout.save.mockImplementation(() => pending.promise);
      const showToast = jest.fn();
      renderRouted(layout, showToast);
      fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
      fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
      const bar = screen.getByRole('region', { name: 'Customizing dashboard' });
      await act(async () => {
        fireEvent.click(within(bar).getByRole('button', { name: /Done/ }));
      });
      expect(layout.save).toHaveBeenCalledTimes(1);

      await act(async () => {
        fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }));
      });
      expect(screen.queryByRole('dialog', { name: 'Leave with unsaved changes?' })).not.toBeInTheDocument();
      expect(await screen.findByText('Destination')).toBeInTheDocument();

      await act(async () => {
        if (succeeds) pending.resolve(layoutResponse(null));
        else pending.reject(httpError(409, 'Changed in another tab.'));
      });
      expect(showToast).toHaveBeenLastCalledWith(...toast);
    }
  );

  it('still guards a dirty draft that is not being saved', async () => {
    const layout = createFakeLayout();
    renderRouted(layout, jest.fn());
    fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }));
    });
    expect(await screen.findByRole('dialog', { name: 'Leave with unsaved changes?' })).toBeInTheDocument();
  });
});

describe('a keyboard or menu move reveals the moved widget (DOM)', () => {
  let scrollIntoView: jest.Mock;
  beforeEach(() => {
    scrollIntoView = jest.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
  });
  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
  });

  const scrolledCells = () =>
    scrollIntoView.mock.contexts.map(element => (element as HTMLElement).getAttribute('data-widget-cell'));

  it('scrolls the moved cell into view on End, Home (focus never moved) and a menu move', () => {
    render(<EditorHarness layout={createFakeLayout()} eligibility={MANAGER} />);
    fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
    const move = screen.getByRole('button', { name: 'Move or resize Capacity Overview' });
    move.focus();

    fireEvent.keyDown(move, { key: 'End' });
    expect(scrolledCells()).toEqual(['capacity_overview']);
    expect(scrollIntoView).toHaveBeenLastCalledWith({ block: 'nearest', behavior: 'smooth' });

    // Home leaves the focused node in place: focus() alone would never scroll.
    fireEvent.keyDown(move, { key: 'Home' });
    expect(move).toHaveFocus();
    expect(scrolledCells()).toEqual(['capacity_overview', 'capacity_overview']);

    fireEvent.click(move);
    fireEvent.click(
      within(screen.getByRole('group', { name: 'Move or resize Capacity Overview' })).getByRole('button', {
        name: 'Move to end',
      })
    );
    expect(scrolledCells()).toEqual(['capacity_overview', 'capacity_overview', 'capacity_overview']);
  });

  it('settles a glide still running on the cell before measuring where to scroll', () => {
    render(<EditorHarness layout={createFakeLayout()} eligibility={MANAGER} />);
    fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
    const cell = document.querySelector('[data-widget-cell="capacity_overview"]')!;
    const glide = { cancel: jest.fn() };
    Object.defineProperty(cell, 'getAnimations', { configurable: true, value: () => [glide] });
    const move = screen.getByRole('button', { name: 'Move or resize Capacity Overview' });
    move.focus();
    fireEvent.keyDown(move, { key: 'End' });
    expect(glide.cancel).toHaveBeenCalledTimes(1);
    expect(glide.cancel.mock.invocationCallOrder[0]).toBeLessThan(scrollIntoView.mock.invocationCallOrder[0]);
  });
});

it('the registry used by default is the dashboard registry', () => {
  const { editor, enter } = setup();
  enter();
  expect(editor().shown?.kpis.map(definition => definition.id)).toEqual(
    draftFromLayout(null, MANAGER, DASHBOARD_REGISTRY).kpis
  );
});

describe('focus when the session goes away under the user (DOM)', () => {
  const renderHarness = (layout: FakeLayoutController, showToast = jest.fn()) => {
    const utils = render(<EditorHarness layout={layout} eligibility={MANAGER} scope="scope-a" showToast={showToast} />);
    fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
    const rerenderScope = (scope: string) =>
      utils.rerender(<EditorHarness layout={layout} eligibility={MANAGER} scope={scope} showToast={showToast} />);
    return { ...utils, rerenderScope, showToast };
  };

  it('sends focus that was inside the editor to Customize when the scope changes', () => {
    const layout = createFakeLayout();
    const { rerenderScope, showToast } = renderHarness(layout);
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    expect(screen.getByRole('button', { name: 'Remove Signed In, Idle' })).toHaveFocus();
    rerenderScope('scope-b');
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Customize' })).toHaveFocus();
    expect(showToast).toHaveBeenCalledWith('warning', copy.SESSION_CHANGED_TOAST);
    // The discarded draft is not what renders: the saved layout is back.
    expect(document.querySelector('[data-widget-cell="kpi.overdue"]')).not.toBeNull();
  });

  it('falls back to #main-content while Customize is disabled because the new layout is loading', () => {
    const layout = createFakeLayout();
    const { rerenderScope } = renderHarness(layout);
    act(() => {
      layout.set({ status: 'loading', response: null });
      rerenderScope('scope-b');
    });
    expect(screen.getByRole('button', { name: 'Customize' })).toBeDisabled();
    expect(document.getElementById(copy.EDIT_IDS.mainContent)).toHaveFocus();
  });

  it('leaves focus where it is when it was outside the editor (it did not drop to <body>)', () => {
    const layout = createFakeLayout();
    const harness = (scope: string) => (
      <>
        <button type="button">Elsewhere on the page</button>
        <EditorHarness layout={layout} eligibility={MANAGER} scope={scope} />
      </>
    );
    const { rerender } = render(harness('scope-a'));
    fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere on the page' });
    elsewhere.focus();
    rerender(harness('scope-b'));
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(elsewhere).toHaveFocus();
  });

  it('focuses Customize when the session turns read-only mid-edit', () => {
    const layout = createFakeLayout();
    const { showToast } = renderHarness(layout);
    act(() => layout.set({ response: layoutResponse(null, { can_edit: false }) }));
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(showToast).toHaveBeenCalledWith('info', copy.READ_ONLY_LAYOUT_NOTICE);
    expect(screen.getByRole('button', { name: 'Customize' })).toHaveFocus();
  });
});

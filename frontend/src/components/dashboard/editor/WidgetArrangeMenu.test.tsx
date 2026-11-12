/**
 * The Move menu, driven through the real editor on a miniature dashboard: opening (click or
 * Enter) and its aria wiring, first-item focus with disabled edges, move items (move, close,
 * refocus), width radios (apply, stay open), the capture-phase Escape, outside presses and
 * focus leaving, one menu at a time, placement clear of the fixed chrome (the edit bar, the
 * sticky header, the viewport's / sidebar's left edge), and one Undo step per menu visit for
 * the width radios.
 * (Dialog case #3, "focus stays on the moved row", lives here and in the page suite.)
 */
import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Permission } from '../../../utils/permissions';
import { createFakeLayout, EditorHarness } from '../../../test-utils/layoutEditorHarness';
import { toDomRect, type RectInit } from '../../../test-utils/dndDom';
import { PANEL_SIZE_CLASSES, PANEL_SIZES, type WidgetEligibilityContext } from '../widgetTypes';
import { POSITION_ANNOUNCE_DEBOUNCE_MS } from './dragPlanning';
import { EDIT_IDS, moveButtonId } from './editorCopy';
import type { LayoutEditor } from './useLayoutEditor';
import { WIDTH_GLYPH_SPANS } from './WidgetArrangeMenu';

jest.mock('../../../services/api', () => ({ __esModule: true, default: {} }));

const ctxFor = (permissions: Permission[]): WidgetEligibilityContext => ({
  can: permission => permissions.includes(permission),
  hasRole: () => false,
  isAdmin: false,
});
const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'receiving:view', 'shipping:view']);

function renderEditing(layout = createFakeLayout()) {
  const editorRef: { current: LayoutEditor | null } = { current: null };
  const utils = render(<EditorHarness layout={layout} eligibility={MANAGER} editorRef={editorRef} />);
  fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
  return { ...utils, layout, editor: () => editorRef.current! };
}

/** Stub boxes by element: the open menu (`role="group"`), an id, or anything matching `pick`. */
function stubBoxes(boxes: { menu?: RectInit; byId?: Record<string, RectInit> }) {
  const original = HTMLElement.prototype.getBoundingClientRect;
  jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (boxes.menu && this.getAttribute('role') === 'group' && this.id.startsWith('dashboard-edit-menu-')) {
      return toDomRect(boxes.menu);
    }
    const byId = boxes.byId?.[this.id];
    if (byId) return toDomRect(byId);
    return original.call(this);
  });
}

function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width });
  Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height });
}

const moveButton = (name: string) => screen.getByRole('button', { name });
const menu = (name: string) => screen.getByRole('group', { name });
const kpiTitles = () =>
  Array.from(screen.getByTestId('kpi-zone').querySelectorAll('[data-widget-cell]')).map(cell =>
    cell.getAttribute('data-widget-cell')
  );
const announcer = () => screen.getByTestId('page-announcer');

const VIEWPORT = { width: window.innerWidth, height: window.innerHeight };
afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
  setViewport(VIEWPORT.width, VIEWPORT.height);
});

it('opens from Enter on the Move button, wires aria-controls only while open and focuses the first enabled item', async () => {
  renderEditing();
  const user = userEvent.setup();
  const move = moveButton('Move Overdue');
  expect(move).not.toHaveAttribute('aria-controls');
  move.focus();
  await user.keyboard('{Enter}');
  const group = menu('Move Overdue');
  expect(move).toHaveAttribute('aria-expanded', 'true');
  expect(move).toHaveAttribute('aria-controls', group.id);
  // First widget: the two "earlier" items are disabled, so focus lands on "Move later".
  expect(within(group).getByRole('button', { name: 'Move to start' })).toBeDisabled();
  expect(within(group).getByRole('button', { name: 'Move earlier' })).toBeDisabled();
  expect(within(group).getByRole('button', { name: 'Move later' })).toHaveFocus();
  expect(within(group).getByRole('button', { name: 'Move to end' })).toBeEnabled();
  // KPI tiles have one size: no Width fieldset.
  expect(within(group).queryByRole('group', { name: 'Width' })).not.toBeInTheDocument();
});

it('a move item moves the widget, closes the menu and refocuses its Move button', () => {
  renderEditing();
  fireEvent.click(moveButton('Move Overdue'));
  fireEvent.click(within(menu('Move Overdue')).getByRole('button', { name: 'Move later' }));
  expect(kpiTitles().slice(0, 2)).toEqual(['kpi.idle_operators', 'kpi.overdue']);
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
  expect(moveButton('Move Overdue')).toHaveFocus();
  expect(moveButton('Move Overdue')).not.toHaveAttribute('aria-controls');
  // Focus moved onto the button, so the screen reader reads it with its description, which
  // ends with the new position: the live region does not say it a second time.
  expect(moveButton('Move Overdue')).toHaveAccessibleDescription(/Position 2 of 10\.$/);
  expect(announcer()).not.toHaveTextContent('Overdue, position 2 of 10.');

  fireEvent.click(moveButton('Move Overdue'));
  fireEvent.click(within(menu('Move Overdue')).getByRole('button', { name: 'Move to end' }));
  expect(kpiTitles().at(-1)).toBe('kpi.overdue');
  expect(moveButton('Move Overdue')).toHaveFocus();
});

it('moves back one place and to the start; the last widget offers only those two', () => {
  renderEditing();
  const start = kpiTitles();
  const others = start.filter(id => id !== 'kpi.overdue');
  fireEvent.click(moveButton('Move Overdue'));
  fireEvent.click(within(menu('Move Overdue')).getByRole('button', { name: 'Move to end' }));
  expect(kpiTitles()).toEqual([...others, 'kpi.overdue']);

  // Last widget: the two "later" items are disabled, so focus lands on "Move to start".
  fireEvent.click(moveButton('Move Overdue'));
  const last = menu('Move Overdue');
  expect(within(last).getByRole('button', { name: 'Move later' })).toBeDisabled();
  expect(within(last).getByRole('button', { name: 'Move to end' })).toBeDisabled();
  expect(within(last).getByRole('button', { name: 'Move earlier' })).toBeEnabled();
  expect(within(last).getByRole('button', { name: 'Move to start' })).toHaveFocus();
  fireEvent.click(within(last).getByRole('button', { name: 'Move earlier' }));
  expect(kpiTitles()).toEqual([...others.slice(0, -1), 'kpi.overdue', others[others.length - 1]]);
  expect(moveButton('Move Overdue')).toHaveFocus();

  fireEvent.click(moveButton('Move Overdue'));
  fireEvent.click(within(menu('Move Overdue')).getByRole('button', { name: 'Move to start' }));
  expect(kpiTitles()).toEqual(['kpi.overdue', ...others]);
  expect(moveButton('Move Overdue')).toHaveFocus();
});

it('focuses the menu itself when every item is disabled (the only widget in its zone)', () => {
  renderEditing(
    createFakeLayout({
      saved: { format_version: 1, widgets: [{ id: 'kpi.overdue' }, { id: 'capacity_overview', size: 'wide' }] },
    })
  );
  fireEvent.click(moveButton('Move Overdue'));
  const group = menu('Move Overdue');
  const items = within(group).getAllByRole('button');
  expect(items).toHaveLength(4);
  items.forEach(item => expect(item).toBeDisabled());
  // Focus is inside the menu (not left on Move or dropped to <body>), so Escape and Tab work.
  expect(group).toHaveFocus();
  fireEvent.keyDown(group, { key: 'Escape' });
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
  expect(moveButton('Move Overdue')).toHaveFocus();
});

it('stays open when focus goes nowhere (the window lost focus), and closes on an outside touch', () => {
  renderEditing();
  fireEvent.click(moveButton('Move Overdue'));
  const item = within(menu('Move Overdue')).getByRole('button', { name: 'Move later' });
  fireEvent.focusOut(item, { relatedTarget: null });
  expect(menu('Move Overdue')).toBeInTheDocument();
  fireEvent.touchStart(item, { touches: [{ clientX: 1, clientY: 1 }] });
  expect(menu('Move Overdue')).toBeInTheDocument();
  fireEvent.touchStart(document.body, { touches: [{ clientX: 1, clientY: 1 }] });
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
});

it('arrow keys, Home and End reorder the grid with focus re-applied to the same Move button', () => {
  renderEditing();
  const move = moveButton('Move Overdue');
  move.focus();
  fireEvent.keyDown(move, { key: 'ArrowRight' });
  expect(kpiTitles().slice(0, 2)).toEqual(['kpi.idle_operators', 'kpi.overdue']);
  expect(document.activeElement?.id).toBe(moveButtonId('kpi.overdue'));
  expect(move).toHaveAccessibleDescription(/Position 2 of 10\.$/);
  fireEvent.keyDown(document.activeElement!, { key: 'End' });
  expect(kpiTitles().at(-1)).toBe('kpi.overdue');
  expect(document.activeElement?.id).toBe(moveButtonId('kpi.overdue'));
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  expect(announcer()).toHaveTextContent('Overdue is already last.');
  fireEvent.keyDown(document.activeElement!, { key: 'Home' });
  expect(kpiTitles()[0]).toBe('kpi.overdue');
  // A move earlier leaves the focused node in place: no focus event, so the live region says it.
  expect(announcer()).toHaveTextContent('Overdue, position 1 of 10.');
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
  expect(announcer()).toHaveTextContent('Overdue is already first.');
  // The cell's sr-only position follows the move.
  expect(move).toHaveAccessibleDescription(/Position 1 of 10\.$/);
});

it('a width radio applies at once and keeps the menu open with focus on the radio', () => {
  jest.useFakeTimers();
  renderEditing();
  fireEvent.click(moveButton('Move or resize Capacity Overview'));
  const group = menu('Move or resize Capacity Overview');
  const width = within(group).getByRole('group', { name: 'Width' });
  expect(
    within(width)
      .getAllByRole('radio')
      .map(radio => radio.getAttribute('value'))
  ).toEqual([...PANEL_SIZES]);
  expect(within(width).getByRole('radio', { name: 'Wide' })).toBeChecked();

  const third = within(width).getByRole('radio', { name: 'Third' });
  third.focus();
  fireEvent.click(third);
  const cell = document.querySelector('[data-widget-cell="capacity_overview"]')!;
  expect(cell).toHaveClass('xl:col-span-4');
  expect(cell).not.toHaveClass('xl:col-span-7');
  expect(menu('Move or resize Capacity Overview')).toBeInTheDocument();
  expect(within(menu('Move or resize Capacity Overview')).getByRole('radio', { name: 'Third' })).toBeChecked();
  expect(third).toHaveFocus();
  // Announced once the width has settled (arrowing checks every radio in turn).
  act(() => {
    jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS);
  });
  expect(announcer()).toHaveTextContent('Capacity Overview width: Third.');
  // The cell carries the menu above its siblings while it is open.
  expect(cell).toHaveClass('z-20');
});

it('Escape closes it through the capture-phase listener before any bubble listener, and refocuses', () => {
  renderEditing();
  const bubble = jest.fn();
  window.addEventListener('keydown', bubble);
  try {
    fireEvent.click(moveButton('Move Overdue'));
    const item = within(menu('Move Overdue')).getByRole('button', { name: 'Move later' });
    fireEvent.keyDown(item, { key: 'Escape' });
    expect(bubble).not.toHaveBeenCalled();
    expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
    expect(moveButton('Move Overdue')).toHaveFocus();
    // Closed, Escape is not the menu's any more (and never leaves edit mode).
    fireEvent.keyDown(moveButton('Move Overdue'), { key: 'Escape' });
    expect(bubble).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('region', { name: 'Customizing dashboard' })).toBeInTheDocument();
  } finally {
    window.removeEventListener('keydown', bubble);
  }
});

it('closes on an outside mousedown without refocusing Move, and stays open for presses inside', () => {
  renderEditing();
  fireEvent.click(moveButton('Move Overdue'));
  fireEvent.mouseDown(within(menu('Move Overdue')).getByRole('button', { name: 'Move later' }));
  fireEvent.mouseDown(moveButton('Move Overdue'));
  expect(menu('Move Overdue')).toBeInTheDocument();
  fireEvent.mouseDown(document.body);
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
  // The press itself decides where focus goes; the menu does not pull it back to Move.
  expect(moveButton('Move Overdue')).not.toHaveFocus();
});

it('closes when focus leaves both the Move button and the menu', () => {
  renderEditing();
  fireEvent.click(moveButton('Move Overdue'));
  const item = within(menu('Move Overdue')).getByRole('button', { name: 'Move later' });
  // Back to its own Move button: still open.
  fireEvent.focusOut(item, { relatedTarget: moveButton('Move Overdue') });
  expect(menu('Move Overdue')).toBeInTheDocument();
  fireEvent.focusOut(item, { relatedTarget: screen.getByRole('button', { name: 'Remove Overdue' }) });
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
});

it('keeps one menu open at a time', () => {
  renderEditing();
  fireEvent.click(moveButton('Move Overdue'));
  // A real press: mousedown (outside the first menu) then click.
  fireEvent.mouseDown(moveButton('Move Due Today'));
  fireEvent.click(moveButton('Move Due Today'));
  expect(screen.queryByRole('group', { name: 'Move Overdue' })).not.toBeInTheDocument();
  expect(menu('Move Due Today')).toBeInTheDocument();
  expect(screen.getAllByRole('group', { name: /^Move/ })).toHaveLength(1);
});

it('opens upward when it would overflow the viewport bottom', () => {
  renderEditing();
  const original = HTMLElement.prototype.getBoundingClientRect;
  jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.getAttribute('role') === 'group') {
      return { x: 0, y: 700, left: 0, top: 700, width: 224, height: 200, right: 224, bottom: 900, toJSON: () => ({}) };
    }
    if (this.id === moveButtonId('kpi.overdue')) {
      return { x: 0, y: 670, left: 0, top: 670, width: 24, height: 24, right: 24, bottom: 694, toJSON: () => ({}) };
    }
    return original.call(this);
  });
  act(() => {
    fireEvent.click(moveButton('Move Overdue'));
  });
  expect(menu('Move Overdue')).toHaveAttribute('data-placement', 'up');
});

it('a keyboard move is spoken once: by the re-focused button, or else by the live region', () => {
  renderEditing();
  const move = moveButton('Move Overdue');
  const focusEvents = jest.fn();
  move.addEventListener('focus', focusEvents);
  move.focus();
  focusEvents.mockClear();

  // Later: React re-inserts the cell, focus drops and is restored (a focus event).
  fireEvent.keyDown(move, { key: 'ArrowRight' });
  expect(focusEvents).toHaveBeenCalledTimes(1);
  expect(move).toHaveAccessibleDescription(/Position 2 of 10\.$/);
  expect(announcer()).not.toHaveTextContent('Overdue, position 2 of 10.');

  // Earlier: the focused node stays put (no focus event), so the live region speaks.
  focusEvents.mockClear();
  fireEvent.keyDown(move, { key: 'ArrowLeft' });
  expect(focusEvents).not.toHaveBeenCalled();
  expect(move).toHaveFocus();
  expect(announcer()).toHaveTextContent('Overdue, position 1 of 10.');
});

it('arrowing through the width radios is ONE Undo step for that menu visit, announced once settled', async () => {
  jest.useFakeTimers();
  const { editor } = renderEditing();
  const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
  fireEvent.click(moveButton('Move or resize Capacity Overview'));
  const width = within(menu('Move or resize Capacity Overview')).getByRole('group', { name: 'Width' });
  within(width).getByRole('radio', { name: 'Third' }).focus();
  // Narrow → Half → Wide → Two-thirds: four widths applied, one after another.
  await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}{ArrowDown}');
  const cell = document.querySelector('[data-widget-cell="capacity_overview"]')!;
  expect(within(width).getByRole('radio', { name: 'Two-thirds' })).toBeChecked();
  expect(cell).toHaveClass('xl:col-span-8');
  // Only the settled width is announced.
  expect(announcer()).not.toHaveTextContent(/width:/);
  act(() => {
    jest.advanceTimersByTime(POSITION_ANNOUNCE_DEBOUNCE_MS);
  });
  expect(announcer()).toHaveTextContent('Capacity Overview width: Two-thirds.');

  // All in one menu visit: one Undo puts back the width the menu opened with.
  expect(editor().canUndo).toBe(true);
  act(() => editor().undo());
  expect(editor().canUndo).toBe(false);
  expect(editor().draft?.panels.find(panel => panel.id === 'capacity_overview')?.size).toBe('wide');
});

describe('placement clear of the fixed chrome', () => {
  it('opens upward when it would run under the fixed edit bar, even inside the viewport', () => {
    renderEditing();
    setViewport(1280, 768);
    stubBoxes({
      menu: { left: 600, top: 434, width: 224, height: 334 }, // bottom 768: under the bar (top 719)
      byId: {
        [EDIT_IDS.bar]: { left: 288, top: 719, width: 992, height: 49 },
        [moveButtonId('capacity_overview')]: { left: 800, top: 404, width: 24, height: 24 },
      },
    });
    act(() => {
      fireEvent.click(moveButton('Move or resize Capacity Overview'));
    });
    expect(menu('Move or resize Capacity Overview')).toHaveAttribute('data-placement', 'up');
  });

  it('never flips under the sticky header; with no room either way it scrolls itself clear', () => {
    renderEditing();
    setViewport(1280, 700);
    const scrollIntoView = jest.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
    try {
      stubBoxes({
        menu: { left: 600, top: 410, width: 224, height: 350 }, // overflows the bottom
        byId: { [moveButtonId('capacity_overview')]: { left: 800, top: 380, width: 24, height: 24 } }, // 380 − 350 < 64
      });
      act(() => {
        fireEvent.click(moveButton('Move or resize Capacity Overview'));
      });
      const group = menu('Move or resize Capacity Overview');
      expect(group).toHaveAttribute('data-placement', 'down');
      expect(scrollIntoView.mock.contexts).toContain(group);
    } finally {
      Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });

  it('grows rightward on a narrow left-column tile where leftward would leave the viewport', () => {
    renderEditing();
    setViewport(390, 844);
    stubBoxes({ menu: { left: -37, top: 300, width: 224, height: 138 } });
    act(() => {
      fireEvent.click(moveButton('Move Overdue'));
    });
    const group = menu('Move Overdue');
    expect(group).toHaveAttribute('data-align', 'start');
    expect(group).toHaveClass('left-1');
    expect(group).not.toHaveClass('right-1');
  });

  it('grows rightward where leftward would run under the fixed sidebar (lg+)', () => {
    renderEditing();
    setViewport(1024, 768);
    stubBoxes({
      menu: { left: 220, top: 300, width: 224, height: 138 },
      byId: { [EDIT_IDS.mainContent]: { left: 288, top: 56, width: 736, height: 712 } },
    });
    act(() => {
      fireEvent.click(moveButton('Move Overdue'));
    });
    expect(menu('Move Overdue')).toHaveAttribute('data-align', 'start');
  });

  it('keeps growing leftward when it fits', () => {
    renderEditing();
    setViewport(1280, 768);
    stubBoxes({
      menu: { left: 500, top: 300, width: 224, height: 138 },
      byId: { [EDIT_IDS.mainContent]: { left: 288, top: 56, width: 992, height: 712 } },
    });
    act(() => {
      fireEvent.click(moveButton('Move Overdue'));
    });
    const group = menu('Move Overdue');
    expect(group).toHaveAttribute('data-align', 'end');
    expect(group).toHaveClass('right-1');
  });
});

it('draws each width glyph at the span the width really takes on the xl grid', () => {
  PANEL_SIZES.forEach(size => {
    expect(PANEL_SIZE_CLASSES[size]).toContain(`xl:col-span-${WIDTH_GLYPH_SPANS[size]}`);
  });
});

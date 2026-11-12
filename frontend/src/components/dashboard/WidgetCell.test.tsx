/**
 * WidgetCell: one stable element tree in view and edit mode (attributes and classes toggle,
 * the widget never remounts), inert content behind an aria-hidden drag surface, the named
 * Remove / Move chrome, keyboard moves, and pointer forwarding to the drag bridge.
 */
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { createDragBridge, type DragBridge } from './editor/dragBridge';
import { EDIT_IDS, moveButtonId, positionTextId } from './editor/editorCopy';
import { WidgetCell, type WidgetCellEditor, type WidgetCellProps } from './WidgetCell';
import { WIDGETS_BY_ID } from './widgetRegistry';
import type { DashboardWidgetComponent } from './widgetTypes';
import { MiniStat } from '../cockpit';
import { toDomRect } from '../../test-utils/dndDom';

jest.mock('../../services/api', () => ({ __esModule: true, default: {} }));
jest.mock('../../services/errorLogging', () => ({ logError: jest.fn() }));

const KPI = WIDGETS_BY_ID.get('kpi.overdue')!;
const PANEL = WIDGETS_BY_ID.get('capacity_overview')!;

let widgetMounts = 0;
const Tile: DashboardWidgetComponent = function Tile() {
  React.useEffect(() => {
    widgetMounts += 1;
  }, []);
  return (
    <div data-testid="tile">
      <a href="/work-orders">Overdue tile</a>
    </div>
  );
};

function makeEditor(overrides: Partial<WidgetCellEditor> = {}): WidgetCellEditor {
  return {
    controlsDisabled: false,
    menuFor: null,
    dragging: null,
    highlightId: null,
    moveBy: jest.fn(),
    moveTo: jest.fn(),
    remove: jest.fn(),
    openMenu: jest.fn(),
    closeMenu: jest.fn(),
    setPanelSize: jest.fn(),
    ...overrides,
  };
}

interface CellOptions {
  editing?: boolean;
  editor?: WidgetCellEditor;
  bridge?: DragBridge;
  props?: Partial<WidgetCellProps>;
}

function cellElement(props: WidgetCellProps, { editing = false, editor = makeEditor(), bridge }: CellOptions) {
  return (
    <MemoryRouter>
      <WidgetCell
        {...props}
        edit={editing ? { editor, bridge: bridge ?? createDragBridge(), index: 0, count: 3 } : null}
      />
    </MemoryRouter>
  );
}

function renderCell(options: CellOptions = {}) {
  const props: WidgetCellProps = { definition: KPI, component: Tile, zone: 'kpi', ...options.props };
  const utils = render(cellElement(props, options));
  const cell = () => utils.container.querySelector<HTMLElement>('[data-widget-cell]')!;
  const body = () => cell().querySelector<HTMLElement>('[data-widget-body]')!;
  const rerenderWith = (next: CellOptions) => utils.rerender(cellElement(props, { ...options, ...next }));
  return { ...utils, cell, body, rerenderWith };
}

beforeEach(() => {
  widgetMounts = 0;
});
afterEach(() => jest.restoreAllMocks());

describe('view mode', () => {
  it('renders no chrome, no inert, no role; min-w-0 on the cell and h-full on a KPI body', () => {
    const { cell, body } = renderCell();
    expect(cell()).toHaveAttribute('data-widget-cell', 'kpi.overdue');
    expect(cell()).toHaveClass('min-w-0');
    expect(cell()).not.toHaveAttribute('role');
    expect(cell()).not.toHaveClass('relative');
    expect(body()).not.toHaveAttribute('inert');
    expect(body()).toHaveClass('h-full');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(cell().querySelector('[data-drag-surface]')).toBeNull();
  });

  it('puts the panel size classes on a panel cell (and no h-full on its body)', () => {
    const { cell, body } = renderCell({ props: { definition: PANEL, zone: 'panel', size: 'two_thirds' } });
    expect(cell()).toHaveClass('min-w-0', 'lg:col-span-2', 'xl:col-span-8');
    expect(body()).not.toHaveClass('h-full');
  });
});

describe('edit mode', () => {
  it('makes only the body inert and names the chrome; the surface is aria-hidden with no role or tabindex', () => {
    const { cell, body } = renderCell({ editing: true });
    expect(cell()).toHaveAttribute('role', 'listitem');
    expect(cell()).toHaveClass('relative', 'outline-dashed', 'outline-fd-line-bright');
    expect(body()).toHaveAttribute('inert');
    expect(cell().querySelectorAll('[inert]')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Remove Overdue' })).toHaveAttribute('title', 'Remove');
    const move = screen.getByRole('button', { name: 'Move Overdue' });
    expect(move).toHaveAttribute('aria-expanded', 'false');
    expect(move).not.toHaveAttribute('aria-controls');
    expect(move).toHaveAttribute('aria-keyshortcuts', 'ArrowUp ArrowDown ArrowLeft ArrowRight Home End');
    expect(move).toHaveAttribute('aria-describedby', `${EDIT_IDS.moveHelp} ${positionTextId('kpi.overdue')}`);
    expect(screen.getByText('Position 1 of 3.')).toHaveClass('sr-only');

    const surface = cell().querySelector('[data-drag-surface]')!;
    expect(surface).toHaveAttribute('aria-hidden', 'true');
    expect(surface).not.toHaveAttribute('role');
    expect(surface).not.toHaveAttribute('tabindex');
    // Nothing borrowed from dnd-kit's attributes.
    expect(cell().querySelector('[aria-roledescription]')).toBeNull();
    expect(cell().querySelector('[role="button"]')).toBeNull();
    // Chrome first, then the body, then the surface.
    expect(Array.from(cell().children).indexOf(body())).toBeGreaterThan(0);
    expect(cell().lastElementChild).toBe(surface);
  });

  it('keeps a KPI label clear of the Move button while editing (and only then)', () => {
    const Stat: DashboardWidgetComponent = function Stat() {
      return <MiniStat icon="span" iconBg="" iconColor="" label="Active Work Orders" value={42} />;
    };
    const { body, rerenderWith } = renderCell({ editing: true, props: { component: Stat } });
    // The selector's hook: MiniStat's label carries `stat-label`.
    expect(body().querySelector('.stat-label')).toHaveTextContent('Active Work Orders');
    expect(body()).toHaveClass('[&_.stat-label]:pr-5', 'pointer-coarse:[&_.stat-label]:pr-7');
    rerenderWith({ editing: false });
    expect(body()).not.toHaveClass('[&_.stat-label]:pr-5');
  });

  it('reserves no label clearance in a panel (its Move button sits over the panel header row)', () => {
    const { body } = renderCell({ editing: true, props: { definition: PANEL, zone: 'panel', size: 'wide' } });
    expect(body()).not.toHaveClass('[&_.stat-label]:pr-5');
  });

  it('names a panel Move button "Move or resize" and compacts the body below lg', () => {
    const { body } = renderCell({ editing: true, props: { definition: PANEL, zone: 'panel', size: 'wide' } });
    expect(screen.getByRole('button', { name: 'Move or resize Capacity Overview' })).toBeInTheDocument();
    expect(body()).toHaveClass('max-lg:max-h-48', 'max-lg:overflow-hidden');
  });

  it('never remounts the widget when edit mode is entered and left', () => {
    const { body, rerenderWith } = renderCell();
    const before = body();
    const tile = screen.getByTestId('tile');
    rerenderWith({ editing: true });
    expect(body()).toBe(before);
    expect(screen.getByTestId('tile')).toBe(tile);
    rerenderWith({ editing: false });
    expect(body()).toBe(before);
    expect(screen.getByTestId('tile')).toBe(tile);
    expect(widgetMounts).toBe(1);
  });

  it('marks the dragged cell as the landing slot: body hidden but still mounted', () => {
    const editor = makeEditor({ dragging: { zone: 'kpi', id: 'kpi.overdue' } });
    const { cell, body } = renderCell({ editing: true, editor });
    expect(cell()).toHaveAttribute('data-dragging', 'true');
    expect(cell()).toHaveClass('outline-2', 'outline-fd-blue');
    expect(body()).toHaveClass('opacity-0');
    expect(screen.getByTestId('tile')).toBeInTheDocument();
  });

  it('rings the highlighted cell and lifts the cell whose menu is open', () => {
    const { cell } = renderCell({ editing: true, editor: makeEditor({ highlightId: 'kpi.overdue' }) });
    expect(cell()).toHaveClass('ring-2', 'ring-fd-blue');
    expect(cell()).not.toHaveClass('z-20');
  });

  it('keeps a crashed widget removable and movable', () => {
    jest.spyOn(console, 'error').mockImplementation(() => undefined);
    const Broken: DashboardWidgetComponent = () => {
      throw new Error('boom');
    };
    const editor = makeEditor();
    renderCell({ editing: true, editor, props: { component: Broken } });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    expect(editor.remove).toHaveBeenCalledWith('kpi.overdue');
    fireEvent.click(screen.getByRole('button', { name: 'Move Overdue' }));
    expect(editor.openMenu).toHaveBeenCalledWith('kpi.overdue');
  });
});

describe('Move button', () => {
  it('moves with the arrow keys, Home and End, ignores modified keys and prevents page scroll', () => {
    const editor = makeEditor();
    renderCell({ editing: true, editor });
    const move = screen.getByRole('button', { name: 'Move Overdue' });
    expect(fireEvent.keyDown(move, { key: 'ArrowRight' })).toBe(false);
    fireEvent.keyDown(move, { key: 'ArrowDown' });
    fireEvent.keyDown(move, { key: 'ArrowLeft' });
    fireEvent.keyDown(move, { key: 'ArrowUp' });
    expect((editor.moveBy as jest.Mock).mock.calls).toEqual([
      ['kpi.overdue', 1],
      ['kpi.overdue', 1],
      ['kpi.overdue', -1],
      ['kpi.overdue', -1],
    ]);
    fireEvent.keyDown(move, { key: 'Home' });
    fireEvent.keyDown(move, { key: 'End' });
    expect((editor.moveTo as jest.Mock).mock.calls).toEqual([
      ['kpi.overdue', 'start'],
      ['kpi.overdue', 'end'],
    ]);
    // Modifier combinations belong to the browser / assistive tech: untouched, not prevented.
    expect(fireEvent.keyDown(move, { key: 'ArrowRight', shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(move, { key: 'ArrowLeft', ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(move, { key: 'Tab' })).toBe(true);
    expect(editor.moveBy).toHaveBeenCalledTimes(4);
  });

  it('opens and closes the Move menu with a click', () => {
    const editor = makeEditor();
    const { rerenderWith } = renderCell({ editing: true, editor });
    fireEvent.click(screen.getByRole('button', { name: 'Move Overdue' }));
    expect(editor.openMenu).toHaveBeenCalledWith('kpi.overdue');
    const open = makeEditor({ menuFor: 'kpi.overdue' });
    rerenderWith({ editing: true, editor: open });
    const move = screen.getByRole('button', { name: 'Move Overdue' });
    expect(move).toHaveAttribute('aria-expanded', 'true');
    expect(move).toHaveAttribute('aria-controls', screen.getByRole('group', { name: 'Move Overdue' }).id);
    fireEvent.click(move);
    expect(open.closeMenu).toHaveBeenCalled();
  });

  it('is disabled with the rest of the chrome while controls are locked', () => {
    renderCell({ editing: true, editor: makeEditor({ controlsDisabled: true }) });
    expect(screen.getByRole('button', { name: 'Move Overdue' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove Overdue' })).toBeDisabled();
  });
});

describe('pointer forwarding to the drag bridge', () => {
  it('registers the cell only while editing', () => {
    const bridge = createDragBridge();
    const { cell, rerenderWith } = renderCell({ bridge });
    expect(bridge.getSnapshot().cells).toHaveLength(0);
    rerenderWith({ editing: true, bridge });
    expect(bridge.getSnapshot().cells).toEqual([{ id: 'kpi.overdue', zone: 'kpi', element: cell() }]);
    rerenderWith({ editing: false, bridge });
    expect(bridge.getSnapshot().cells).toHaveLength(0);
  });

  it('forwards mousedown and touchstart from the surface and the Move button, unprevented', () => {
    const bridge = createDragBridge();
    const onMouseDown = jest.fn((event: React.MouseEvent) => {
      expect(event.defaultPrevented).toBe(false);
    });
    const onTouchStart = jest.fn();
    bridge.setActivators('kpi.overdue', { onMouseDown, onTouchStart });
    const { cell } = renderCell({ editing: true, bridge });
    const surface = cell().querySelector('[data-drag-surface]')!;
    fireEvent.mouseDown(surface, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Move Overdue' }), { button: 0 });
    fireEvent.touchStart(surface, { touches: [{ clientX: 1, clientY: 1 }] });
    expect(onMouseDown).toHaveBeenCalledTimes(2);
    expect(onTouchStart).toHaveBeenCalledTimes(1);
  });

  it('forwards nothing while controls are locked', () => {
    const bridge = createDragBridge();
    const onMouseDown = jest.fn();
    bridge.setActivators('kpi.overdue', { onMouseDown });
    const { cell } = renderCell({ editing: true, bridge, editor: makeEditor({ controlsDisabled: true }) });
    fireEvent.mouseDown(cell().querySelector('[data-drag-surface]')!, { button: 0 });
    expect(onMouseDown).not.toHaveBeenCalled();
  });

  describe('a press while the zone is still gliding (FLIP)', () => {
    /** Mid-glide the cell is drawn 300 px right of its layout box; finishing the glide puts it home. */
    function renderGliding() {
      const bridge = createDragBridge();
      const onMouseDown = jest.fn();
      const onTouchStart = jest.fn();
      bridge.setActivators('kpi.overdue', { onMouseDown, onTouchStart });
      const utils = renderCell({ editing: true, bridge });
      let settled = false;
      const glide = {
        playState: 'running',
        finish: jest.fn(() => {
          settled = true;
        }),
        cancel: jest.fn(),
      };
      Object.defineProperty(utils.cell(), 'getAnimations', {
        configurable: true,
        value: () => (settled ? [] : [glide]),
      });
      jest
        .spyOn(utils.cell(), 'getBoundingClientRect')
        .mockImplementation(() =>
          toDomRect(
            settled ? { left: 0, top: 0, width: 200, height: 72 } : { left: 300, top: 0, width: 200, height: 72 }
          )
        );
      const surface = utils.cell().querySelector('[data-drag-surface]')!;
      return { surface, glide, onMouseDown, onTouchStart };
    }

    it('finishes the glide and starts no drag from a press off the settled box', () => {
      const { surface, glide, onMouseDown, onTouchStart } = renderGliding();
      fireEvent.mouseDown(surface, { button: 0, clientX: 350, clientY: 30 }); // on the in-flight picture
      expect(glide.finish).toHaveBeenCalledTimes(1);
      expect(onMouseDown).not.toHaveBeenCalled();
      // Settled now: the next press there is not on this cell at all, and one on it lifts.
      fireEvent.mouseDown(surface, { button: 0, clientX: 50, clientY: 30 });
      expect(onMouseDown).toHaveBeenCalledTimes(1);
      expect(onTouchStart).not.toHaveBeenCalled();
    });

    it('a touch on the in-flight picture is refused the same way', () => {
      const { surface, onTouchStart } = renderGliding();
      fireEvent.touchStart(surface, { touches: [{ clientX: 350, clientY: 30 }] });
      expect(onTouchStart).not.toHaveBeenCalled();
    });

    it('a press on the settled box lifts even while the glide was running', () => {
      const { surface, glide, onMouseDown } = renderGliding();
      fireEvent.mouseDown(surface, { button: 0, clientX: 50, clientY: 30 });
      expect(glide.finish).toHaveBeenCalledTimes(1);
      expect(onMouseDown).toHaveBeenCalledTimes(1);
    });

    it("settles a gliding NEIGHBOUR in the zone too, cancelling a glide that can't be finished", () => {
      const bridge = createDragBridge();
      const onMouseDown = jest.fn();
      bridge.setActivators('kpi.overdue', { onMouseDown });
      const editor = makeEditor();
      const DUE_TODAY = WIDGETS_BY_ID.get('kpi.due_today')!;
      const { container } = render(
        <MemoryRouter>
          <WidgetCell definition={KPI} component={Tile} zone="kpi" edit={{ editor, bridge, index: 0, count: 2 }} />
          <WidgetCell
            definition={DUE_TODAY}
            component={Tile}
            zone="kpi"
            edit={{ editor, bridge, index: 1, count: 2 }}
          />
        </MemoryRouter>
      );
      const [pressed, neighbour] = Array.from(container.querySelectorAll<HTMLElement>('[data-widget-cell]'));
      expect(neighbour.parentElement).toBe(pressed.parentElement);
      // The neighbour's glide throws on finish() (an infinite or zero-rate animation).
      const neighbourGlide = {
        playState: 'running',
        finish: jest.fn(() => {
          throw new DOMException('Cannot finish', 'InvalidStateError');
        }),
        cancel: jest.fn(),
      };
      // The pressed cell's own animation has already finished: it is not a glide in flight.
      const ownFinished = { playState: 'finished', finish: jest.fn(), cancel: jest.fn() };
      Object.defineProperty(neighbour, 'getAnimations', { configurable: true, value: () => [neighbourGlide] });
      Object.defineProperty(pressed, 'getAnimations', { configurable: true, value: () => [ownFinished] });
      jest
        .spyOn(pressed, 'getBoundingClientRect')
        .mockReturnValue(toDomRect({ left: 0, top: 0, width: 200, height: 72 }));

      fireEvent.mouseDown(pressed.querySelector('[data-drag-surface]')!, { button: 0, clientX: 50, clientY: 30 });
      expect(neighbourGlide.finish).toHaveBeenCalledTimes(1);
      expect(neighbourGlide.cancel).toHaveBeenCalledTimes(1);
      expect(ownFinished.finish).not.toHaveBeenCalled();
      expect(ownFinished.cancel).not.toHaveBeenCalled();
      // The press was on the pressed cell's settled box: it lifts.
      expect(onMouseDown).toHaveBeenCalledTimes(1);
    });

    it('lets every press through when no animation on the zone is running', () => {
      const bridge = createDragBridge();
      const onMouseDown = jest.fn();
      bridge.setActivators('kpi.overdue', { onMouseDown });
      const { cell } = renderCell({ editing: true, bridge });
      const paused = { playState: 'paused', finish: jest.fn(), cancel: jest.fn() };
      Object.defineProperty(cell(), 'getAnimations', { configurable: true, value: () => [paused] });
      // Even a press the cell's box does not contain: with nothing gliding, nothing is vetted.
      jest
        .spyOn(cell(), 'getBoundingClientRect')
        .mockReturnValue(toDomRect({ left: 300, top: 0, width: 200, height: 72 }));
      fireEvent.mouseDown(cell().querySelector('[data-drag-surface]')!, { button: 0, clientX: 50, clientY: 30 });
      expect(paused.finish).not.toHaveBeenCalled();
      expect(onMouseDown).toHaveBeenCalledTimes(1);
    });
  });

  it('gives the Move button the id focus requests target', () => {
    renderCell({ editing: true });
    expect(screen.getByRole('button', { name: 'Move Overdue' })).toHaveAttribute('id', moveButtonId('kpi.overdue'));
  });
});

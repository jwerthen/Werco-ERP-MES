/**
 * One KPI tile or panel on the dashboard grid, in BOTH view mode and edit mode.
 *
 * It renders the same element types in both modes — children `[chrome | false, body,
 * surface | false]`, the body always child 1 — and edit mode only toggles attributes and
 * classes. So entering or leaving edit mode never remounts a widget: panel scroll survives,
 * the Notifications list cache is not invalidated, and self-fetching widgets do not refetch.
 *
 * Contract:
 * - `component` is the widget's zero-prop renderer. It renders inside `WidgetBody`, a
 *   `React.memo` holding the widget `ErrorBoundary`, so a crashed widget can still be removed
 *   and moved, and a cell re-render (chrome, classes) never re-renders the widget.
 * - `edit` (omit / null = view mode) carries the editor, the drag bridge and the widget's
 *   0-based `index` of `count` in its zone AS RENDERED. Pass `editor` = `useLayoutEditor()`.
 * - View mode: `min-w-0` (+ the panel's `PANEL_SIZE_CLASSES`), no role, no `inert`, no chrome;
 *   a KPI body is `h-full` so the tile still fills its row.
 * - Edit mode: the cell is a `listitem` (the page gives the zone grid `role="list"`) with a
 *   dashed hairline outline; the body is `inert` (live but unreachable); an `aria-hidden` drag
 *   surface covers it and forwards `onMouseDown` / `onTouchStart` to `bridge.start` (never
 *   `preventDefault` first — dnd-kit ignores a prevented event). The chrome is a "−" Remove
 *   badge and a Move button: arrows move one place, Home / End to the ends (each one Undo
 *   step), Enter / Space opens the Move menu, and a press-and-drag on it lifts the widget
 *   like the surface does. Below lg a panel body is compacted to 12rem with a fade.
 * - While dragged: `data-dragging`, a blue dashed slot, the body `opacity-0` (still mounted,
 *   so the slot keeps the widget's height).
 * - A press while the zone's cells are still gliding (FLIP) first finishes those glides, then
 *   starts a drag only when the press is on the cell's SETTLED box: the drag layer and dnd-kit
 *   measure layout boxes, so a press on a cell's in-flight picture would hit-test a neighbour.
 * - In a KPI tile the label reserves the top-right corner while editing, so the Move button
 *   never covers the end of the label that identifies the tile.
 * - `data-widget-cell={id}` is on the cell in both modes (FLIP and the tests key on it); the
 *   cell registers with the bridge only while editing.
 */
import React, { memo } from 'react';
import { MinusIcon } from '@heroicons/react/24/outline';
import { ErrorBoundary } from '../ErrorBoundary';
import type { EditableZone } from './dashboardLayout';
import type { DragBridge, DragStartEvent } from './editor/dragBridge';
import {
  EDIT_IDS,
  arrangeMenuId,
  moveButtonId,
  moveLabel,
  positionTextId,
  removeButtonId,
  removeLabel,
} from './editor/editorCopy';
import type { LayoutEditor } from './editor/useLayoutEditor';
import { WidgetArrangeMenu, type ArrangeTarget } from './editor/WidgetArrangeMenu';
import {
  PANEL_SIZE_CLASSES,
  type DashboardWidgetComponent,
  type DashboardWidgetDefinition,
  type PanelSize,
} from './widgetTypes';

/** What a cell reads from `useLayoutEditor()`. */
export type WidgetCellEditor = Pick<
  LayoutEditor,
  | 'controlsDisabled'
  | 'menuFor'
  | 'dragging'
  | 'highlightId'
  | 'moveBy'
  | 'moveTo'
  | 'remove'
  | 'openMenu'
  | 'closeMenu'
  | 'setPanelSize'
>;

export interface WidgetCellEditProps {
  editor: WidgetCellEditor;
  bridge: DragBridge;
  /** 0-based position in the zone, as rendered. */
  index: number;
  /** How many widgets the zone renders. */
  count: number;
}

export interface WidgetCellProps {
  definition: DashboardWidgetDefinition;
  /** The widget's renderer. */
  component: DashboardWidgetComponent;
  zone: EditableZone;
  /** Panels only: the width this cell renders at. */
  size?: PanelSize;
  /** Edit mode; omit (or null) for view mode. */
  edit?: WidgetCellEditProps | null;
}

/** The widget itself; memoized so cell-level re-renders (chrome, classes) never reach it. */
const WidgetBody = memo(function WidgetBody({
  definition,
  component: Widget,
}: {
  definition: DashboardWidgetDefinition;
  component: DashboardWidgetComponent;
}) {
  return (
    <ErrorBoundary level="widget" name={definition.title}>
      <Widget />
    </ErrorBoundary>
  );
});

const CHROME_BUTTON =
  'absolute z-10 flex size-6 items-center justify-center border border-fd-line-bright bg-fd-panel text-slate-300 ' +
  'transition-colors hover:text-fd-ink focus-visible:outline-2 focus-visible:outline-offset-1 ' +
  'focus-visible:outline-fd-blue disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:size-8';
const GRAB = 'touch-manipulation select-none [-webkit-touch-callout:none]';
/** In edit mode a KPI label wraps short of the Move button (24 px, 32 px on a coarse pointer). */
const KPI_LABEL_CLEARANCE = '[&_.stat-label]:pr-5 pointer-coarse:[&_.stat-label]:pr-7';
/** Below lg a panel previews at 12rem with a fade: placement depends only on order and span. */
const COMPACT_PANEL_BODY =
  'max-lg:max-h-48 max-lg:overflow-hidden max-lg:[mask-image:linear-gradient(to_bottom,#000_70%,transparent)]';

function classes(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

function pressPoint(event: DragStartEvent): { x: number; y: number } | null {
  if ('touches' in event) {
    const touch = event.touches[0] ?? event.changedTouches[0];
    return touch ? { x: touch.clientX, y: touch.clientY } : null;
  }
  return { x: event.clientX, y: event.clientY };
}

/**
 * True when the press must not start a drag: a FLIP glide was running on this zone's cells
 * (each is finished here, so every cell is at its layout box) and the press is not on the
 * pressed cell's settled box. With nothing gliding, every press goes through unchanged.
 */
function pressMissesSettledCell(event: DragStartEvent): boolean {
  const cell = event.currentTarget.closest('[data-widget-cell]');
  const zone = cell?.parentElement;
  if (!cell || !zone) return false;
  let glided = false;
  Array.from(zone.children).forEach(sibling => {
    if (!sibling.hasAttribute('data-widget-cell') || typeof sibling.getAnimations !== 'function') return;
    sibling.getAnimations().forEach(animation => {
      if (animation.playState !== 'running') return;
      glided = true;
      try {
        animation.finish();
      } catch {
        animation.cancel();
      }
    });
  });
  if (!glided) return false;
  const point = pressPoint(event);
  if (!point) return false;
  const box = cell.getBoundingClientRect();
  return point.x < box.left || point.x > box.right || point.y < box.top || point.y > box.bottom;
}

/** Six-dot grip: the Move button's (and the drag ghost's) affordance. */
function GripIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 12 12" className="size-3.5" fill="currentColor">
      <circle cx="4" cy="2.5" r="1" />
      <circle cx="8" cy="2.5" r="1" />
      <circle cx="4" cy="6" r="1" />
      <circle cx="8" cy="6" r="1" />
      <circle cx="4" cy="9.5" r="1" />
      <circle cx="8" cy="9.5" r="1" />
    </svg>
  );
}

interface CellChromeProps {
  definition: DashboardWidgetDefinition;
  zone: EditableZone;
  size?: PanelSize;
  edit: WidgetCellEditProps;
  menuOpen: boolean;
  onPointerStart: (event: DragStartEvent) => void;
}

function CellChrome({ definition, zone, size, edit, menuOpen, onPointerStart }: CellChromeProps) {
  const { editor, index, count } = edit;
  const id = definition.id;
  const title = definition.title;
  const disabled = editor.controlsDisabled;

  const onMoveKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    switch (event.key) {
      case 'ArrowLeft':
      case 'ArrowUp':
        editor.moveBy(id, -1);
        break;
      case 'ArrowRight':
      case 'ArrowDown':
        editor.moveBy(id, 1);
        break;
      case 'Home':
        editor.moveTo(id, 'start');
        break;
      case 'End':
        editor.moveTo(id, 'end');
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const onArrange = (target: ArrangeTarget) => {
    if (target === 'earlier') editor.moveBy(id, -1);
    else if (target === 'later') editor.moveBy(id, 1);
    else editor.moveTo(id, target);
  };

  return (
    <>
      <span id={positionTextId(id)} className="sr-only">
        {`Position ${index + 1} of ${count}.`}
      </span>
      <button
        type="button"
        id={removeButtonId(id)}
        aria-label={removeLabel(title)}
        title="Remove"
        disabled={disabled}
        onClick={() => editor.remove(id)}
        className={`${CHROME_BUTTON} -left-2 -top-2 hover:border-fd-red hover:text-fd-red`}
      >
        <MinusIcon aria-hidden="true" className="size-3.5" />
      </button>
      <button
        type="button"
        id={moveButtonId(id)}
        aria-label={moveLabel(title, zone)}
        aria-describedby={`${EDIT_IDS.moveHelp} ${positionTextId(id)}`}
        aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Home End"
        aria-expanded={menuOpen}
        aria-controls={menuOpen ? arrangeMenuId(id) : undefined}
        title={moveLabel(title, zone)}
        disabled={disabled}
        onKeyDown={onMoveKeyDown}
        onClick={() => (menuOpen ? editor.closeMenu() : editor.openMenu(id))}
        onMouseDown={onPointerStart}
        onTouchStart={onPointerStart}
        className={`${CHROME_BUTTON} right-1 top-1 cursor-grab hover:border-fd-blue ${GRAB}`}
      >
        <GripIcon />
      </button>
      {menuOpen && (
        <WidgetArrangeMenu
          widgetId={id}
          title={title}
          zone={zone}
          index={index}
          count={count}
          size={size}
          disabled={disabled}
          onMove={onArrange}
          onSetSize={next => editor.setPanelSize(id, next)}
          onClose={({ refocus }) => editor.closeMenu({ refocus })}
        />
      )}
    </>
  );
}

export function WidgetCell({ definition, component, zone, size, edit }: WidgetCellProps) {
  const id = definition.id;
  const editing = Boolean(edit);
  const dragging = Boolean(edit && edit.editor.dragging?.id === id);
  const menuOpen = Boolean(edit && edit.editor.menuFor === id);
  const highlighted = Boolean(edit && edit.editor.highlightId === id);

  const onPointerStart = (event: DragStartEvent) => {
    // Never preventDefault here: dnd-kit's activator ignores an already-prevented event.
    if (!edit || edit.editor.controlsDisabled || pressMissesSettledCell(event)) return;
    edit.bridge.start(id, event);
  };

  const cellClass = classes(
    'min-w-0',
    zone === 'panel' && size ? PANEL_SIZE_CLASSES[size] : '',
    editing && 'relative outline outline-dashed outline-offset-2',
    editing && (dragging ? 'outline-2 outline-fd-blue bg-fd-blue/5' : 'outline-1 outline-fd-line-bright'),
    menuOpen && 'z-20',
    highlighted && 'ring-2 ring-fd-blue'
  );
  const bodyClass = classes(
    zone === 'kpi' && 'h-full',
    editing && 'transition-opacity duration-150',
    editing && zone === 'kpi' && KPI_LABEL_CLEARANCE,
    editing && zone === 'panel' && COMPACT_PANEL_BODY,
    dragging && 'opacity-0'
  );

  return (
    <div
      ref={edit ? edit.bridge.cellRef(id, zone) : undefined}
      data-widget-cell={id}
      data-dragging={dragging ? 'true' : undefined}
      role={editing ? 'listitem' : undefined}
      className={cellClass}
    >
      {edit && (
        <CellChrome
          definition={definition}
          zone={zone}
          size={size}
          edit={edit}
          menuOpen={menuOpen}
          onPointerStart={onPointerStart}
        />
      )}
      <div data-widget-body="" inert={editing} className={bodyClass || undefined}>
        <WidgetBody definition={definition} component={component} />
      </div>
      {edit && (
        <div
          aria-hidden="true"
          data-drag-surface=""
          className={classes('absolute inset-0 z-[1]', dragging ? 'cursor-grabbing' : 'cursor-grab', GRAB)}
          onMouseDown={onPointerStart}
          onTouchStart={onPointerStart}
        />
      )}
    </div>
  );
}

export default WidgetCell;

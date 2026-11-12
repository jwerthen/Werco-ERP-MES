/**
 * The Move menu: a disclosure popover inside a widget cell, opened by the cell's Move button
 * (a tap without dragging, or Enter / Space). It is the precise, single-pointer alternative to
 * dragging (WCAG 2.5.7) and the browse-mode screen-reader path.
 *
 * Contract (the cell renders it ONLY while open; `useLayoutEditor.menuFor` is the open state,
 * so only one menu is ever open):
 * - Move to start · Move earlier · Move later · Move to end, edge items disabled. Panels also
 *   get a "Width" fieldset of six native radios (Third … Full width), each with a visible label
 *   and an `aria-hidden` 12-cell glyph of its xl span.
 * - Opening focuses the first enabled item. A move item calls `onMove` (the editor moves the
 *   widget, closes the menu and refocuses the Move button). A width radio calls `onSetSize`
 *   and the menu STAYS open with focus on the radio.
 * - Escape is a capture-phase `window` keydown listener (the ComboBox precedent): it stops the
 *   event before any bubble-phase listener (`<Modal>`, dnd-kit) and calls
 *   `onClose({ refocus: true })`. An outside mousedown / touchstart, or a focusout that leaves
 *   both the Move button and the menu, calls `onClose({ refocus: false })`.
 * - It opens downward from the Move button and flips upward when it would run under the fixed
 *   edit bar (or past the viewport bottom) and the flipped menu clears the sticky header;
 *   when neither fits it scrolls itself clear. It grows leftward from the cell's right edge,
 *   and rightward from its left edge when leftward would cross the content's left edge (the
 *   viewport below lg, the fixed sidebar at lg+). Measured once, on open.
 *
 * No handlers sit on non-interactive elements: every listener is a native one on `window`,
 * `document` or the elements themselves.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { EditableZone } from '../dashboardLayout';
import { PANEL_SIZE_LABELS, PANEL_SIZES, type PanelSize } from '../widgetTypes';
import { EDIT_IDS, arrangeMenuId, moveButtonId, moveLabel } from './editorCopy';

export type ArrangeTarget = 'start' | 'earlier' | 'later' | 'end';

export interface WidgetArrangeMenuProps {
  widgetId: string;
  title: string;
  zone: EditableZone;
  /** 0-based position in the zone. */
  index: number;
  count: number;
  /** The panel's current width (panels only; KPI tiles have one size). */
  size?: PanelSize;
  /** Every item is disabled (a request is in flight or a reload is required). */
  disabled?: boolean;
  onMove: (target: ArrangeTarget) => void;
  onSetSize: (size: PanelSize) => void;
  onClose: (options: { refocus: boolean }) => void;
}

/** The xl column span each width occupies, for the 12-cell glyph (presentation only). */
export const WIDTH_GLYPH_SPANS: Record<PanelSize, number> = {
  third: 4,
  narrow: 5,
  half: 6,
  wide: 7,
  two_thirds: 8,
  full: 12,
};

const GLYPH_CELLS = Array.from({ length: 12 }, (_, index) => index);

/** The sticky app header's clearance (the page's edit-mode scroll-padding-top). */
export const MENU_TOP_CLEARANCE_PX = 64;

function WidthGlyph({ size }: { size: PanelSize }) {
  const span = WIDTH_GLYPH_SPANS[size];
  return (
    <span aria-hidden="true" data-width-glyph={span} className="grid w-14 flex-shrink-0 grid-cols-12 gap-px">
      {GLYPH_CELLS.map(cell => (
        <span key={cell} className={`h-2 ${cell < span ? 'bg-fd-blue' : 'bg-fd-line-bright'}`} />
      ))}
    </span>
  );
}

const ITEM_CLASS =
  'w-full px-2 py-1.5 text-left text-sm text-slate-200 hover:bg-fd-blue/10 focus-visible:outline-2 ' +
  'focus-visible:-outline-offset-2 focus-visible:outline-fd-blue disabled:cursor-not-allowed disabled:text-slate-400 ' +
  'disabled:hover:bg-transparent';

export function WidgetArrangeMenu({
  widgetId,
  title,
  zone,
  index,
  count,
  size,
  disabled = false,
  onMove,
  onSetSize,
  onClose,
}: WidgetArrangeMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState<'down' | 'up'>('down');
  const [align, setAlign] = useState<'end' | 'start'>('end');
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const menuId = arrangeMenuId(widgetId);
  const anchorId = moveButtonId(widgetId);

  // On open: keep the menu clear of the fixed chrome, then focus the first enabled item (the
  // menu itself when every item is disabled).
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const rect = menu.getBoundingClientRect();
    if (rect.width > 0) {
      // Leftward from the cell's right edge would cross the content's left edge (the
      // viewport, or the sidebar at lg+): grow rightward from the cell's left edge instead.
      const leftBound = document.getElementById(EDIT_IDS.mainContent)?.getBoundingClientRect().left ?? 0;
      if (rect.left < Math.max(0, leftBound)) setAlign('start');
    }
    if (rect.height > 0) {
      // The fixed edit bar (above BottomNav below lg) covers the viewport's bottom band.
      const barTop = document.getElementById(EDIT_IDS.bar)?.getBoundingClientRect().top;
      const bottomBound =
        barTop !== undefined && barTop > 0 ? Math.min(barTop, window.innerHeight) : window.innerHeight;
      if (rect.bottom > bottomBound) {
        const anchorTop = document.getElementById(anchorId)?.getBoundingClientRect().top ?? rect.top;
        if (anchorTop - rect.height >= MENU_TOP_CLEARANCE_PX) setPlacement('up');
        // Neither side fits: scroll it clear (honouring the page's edit-mode scroll padding).
        else if (typeof menu.scrollIntoView === 'function') menu.scrollIntoView({ block: 'nearest' });
      }
    }
    const first = menu.querySelector<HTMLElement>('button:not(:disabled), input:not(:disabled)');
    (first ?? menu).focus();
  }, [anchorId]);

  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      onCloseRef.current({ refocus: true });
    };
    window.addEventListener('keydown', onEscape, true);
    return () => window.removeEventListener('keydown', onEscape, true);
  }, []);

  useEffect(() => {
    const menu = menuRef.current;
    const anchor = document.getElementById(anchorId);
    const inside = (target: EventTarget | null) =>
      target instanceof Node && Boolean(menu?.contains(target) || anchor?.contains(target));
    const onPointerDown = (event: Event) => {
      if (!inside(event.target)) onCloseRef.current({ refocus: false });
    };
    // A focusout with no related target (the window lost focus, a node was removed) is
    // left to the outside-press listener; only a move to a known element outside closes.
    const onFocusOut = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && !inside(event.relatedTarget)) onCloseRef.current({ refocus: false });
    };
    document.addEventListener('mousedown', onPointerDown, true);
    document.addEventListener('touchstart', onPointerDown, true);
    menu?.addEventListener('focusout', onFocusOut);
    anchor?.addEventListener('focusout', onFocusOut);
    return () => {
      document.removeEventListener('mousedown', onPointerDown, true);
      document.removeEventListener('touchstart', onPointerDown, true);
      menu?.removeEventListener('focusout', onFocusOut);
      anchor?.removeEventListener('focusout', onFocusOut);
    };
  }, [anchorId]);

  const atStart = index <= 0;
  const atEnd = index >= count - 1;
  const items: { target: ArrangeTarget; label: string; edge: boolean }[] = [
    { target: 'start', label: 'Move to start', edge: atStart },
    { target: 'earlier', label: 'Move earlier', edge: atStart },
    { target: 'later', label: 'Move later', edge: atEnd },
    { target: 'end', label: 'Move to end', edge: atEnd },
  ];

  return (
    <div
      ref={menuRef}
      id={menuId}
      role="group"
      aria-label={moveLabel(title, zone)}
      tabIndex={-1}
      data-placement={placement}
      data-align={align}
      className={`absolute ${align === 'start' ? 'left-1' : 'right-1'} z-20 w-56 max-w-[calc(100vw-2rem)] border border-fd-line-bright bg-fd-panel p-1 shadow-lg ${
        placement === 'up' ? 'bottom-[calc(100%-0.25rem)]' : 'top-8 pointer-coarse:top-10'
      }`}
    >
      <div className="flex flex-col">
        {items.map(item => (
          <button
            key={item.target}
            type="button"
            className={ITEM_CLASS}
            disabled={disabled || item.edge}
            onClick={() => onMove(item.target)}
          >
            {item.label}
          </button>
        ))}
      </div>
      {zone === 'panel' && size && (
        <fieldset className="mt-1 border-t border-fd-line pt-1">
          <legend className="px-2 pt-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Width</legend>
          {PANEL_SIZES.map(option => {
            const inputId = `${menuId}-width-${option}`;
            return (
              <div key={option} className="flex items-center gap-2 px-2 py-1">
                <input
                  id={inputId}
                  type="radio"
                  className="radio !h-4 !w-4"
                  name={`${menuId}-width`}
                  value={option}
                  checked={option === size}
                  disabled={disabled}
                  onChange={() => onSetSize(option)}
                />
                <label
                  htmlFor={inputId}
                  className="flex min-w-0 flex-1 cursor-pointer items-center justify-between gap-2 text-sm text-slate-200"
                >
                  {PANEL_SIZE_LABELS[option]}
                  <WidthGlyph size={option} />
                </label>
              </div>
            );
          })}
        </fieldset>
      )}
    </div>
  );
}

export default WidgetArrangeMenu;

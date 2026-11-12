/**
 * Dashboard edit mode: one rect-snapshot FLIP for every layout change.
 *
 * Call `capture()` BEFORE a change (live reorder, keyboard or menu move, remove, add, width,
 * banner toggle, undo, drag cancel, reload, Cancel / exit). The next commit of the host
 * component then animates every `[data-widget-cell]` under `rootRef` from where it WAS (its
 * visual rect, mid-animation included) to where it IS:
 * - a cell that moved — including one whose index did not change but whose row did — glides
 *   (translate → none, 200 ms);
 * - a cell that was not there fades and scales in (180 ms).
 *
 * Nothing animates without a fresh snapshot, so data refreshes (which never capture) never
 * animate height changes. Disabled under prefers-reduced-motion (the CSS `!important`
 * reduced-motion rule does not reach the Web Animations API) and where
 * `Element.prototype.animate` is missing (jsdom).
 */
import { useCallback, useLayoutEffect, useMemo, useRef, type RefObject } from 'react';

export const FLIP_MOVE_MS = 200;
export const FLIP_ENTER_MS = 180;
export const FLIP_EASING = 'cubic-bezier(0.2,0,0,1)';
/** A snapshot older than this is discarded unplayed (its change never rendered, e.g. a no-op). */
export const FLIP_SNAPSHOT_MAX_AGE_MS = 1000;
/** Moves smaller than this (sub-pixel layout noise) are not animated. */
const MIN_DELTA_PX = 0.5;

const CELL_SELECTOR = '[data-widget-cell]';

interface Snapshot {
  rects: Map<Element, DOMRect>;
  at: number;
}

export interface FlipLayout {
  /** Record every cell's current visual rect; the next commit animates from it. */
  capture: () => void;
}

function canAnimate(element: Element): boolean {
  return typeof element.animate === 'function';
}

function cellsUnder(root: HTMLElement | null): Element[] {
  return root ? Array.from(root.querySelectorAll(CELL_SELECTOR)) : [];
}

export function useFlipLayout(
  rootRef: RefObject<HTMLElement | null>,
  { disabled = false }: { disabled?: boolean } = {}
): FlipLayout {
  const snapshotRef = useRef<Snapshot | null>(null);
  const animationsRef = useRef(new WeakMap<Element, Animation>());
  const disabledRef = useRef(disabled);
  // Declared before the play effect, so a commit that turns motion off is already respected by it.
  useLayoutEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  const capture = useCallback(() => {
    if (disabledRef.current) {
      snapshotRef.current = null;
      return;
    }
    if (typeof Element === 'undefined' || typeof Element.prototype.animate !== 'function') {
      snapshotRef.current = null;
      return;
    }
    // An empty snapshot is still a snapshot: the first widget added to an empty zone fades in.
    const cells = cellsUnder(rootRef.current);
    const rects = new Map<Element, DOMRect>();
    cells.forEach(cell => rects.set(cell, cell.getBoundingClientRect()));
    snapshotRef.current = { rects, at: Date.now() };
  }, [rootRef]);

  // No dependency array: play after whichever commit follows a capture.
  useLayoutEffect(() => {
    const snapshot = snapshotRef.current;
    if (!snapshot) return;
    snapshotRef.current = null;
    if (disabledRef.current || Date.now() - snapshot.at > FLIP_SNAPSHOT_MAX_AGE_MS) return;
    const cells = cellsUnder(rootRef.current).filter(canAnimate);
    const animations = animationsRef.current;
    // Cancel first: a running glide's transform would skew the "where it is now" measurement.
    cells.forEach(cell => {
      animations.get(cell)?.cancel();
      animations.delete(cell);
    });
    cells.forEach(cell => {
      const after = cell.getBoundingClientRect();
      const before = snapshot.rects.get(cell);
      if (!before) {
        animations.set(
          cell,
          cell.animate(
            [
              { opacity: 0, transform: 'scale(0.98)' },
              { opacity: 1, transform: 'none' },
            ],
            { duration: FLIP_ENTER_MS, easing: FLIP_EASING }
          )
        );
        return;
      }
      const dx = before.left - after.left;
      const dy = before.top - after.top;
      if (Math.abs(dx) < MIN_DELTA_PX && Math.abs(dy) < MIN_DELTA_PX) return;
      animations.set(
        cell,
        cell.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], {
          duration: FLIP_MOVE_MS,
          easing: FLIP_EASING,
        })
      );
    });
  });

  return useMemo(() => ({ capture }), [capture]);
}

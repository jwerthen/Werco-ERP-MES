/**
 * Dashboard edit mode: the pointer and touch drag layer. This is the ONLY module that
 * imports `@dnd-kit/core`. It is reached only through `loadDragLayer.ts`'s dynamic import,
 * so dnd-kit lands in its own async chunk and the landing route never carries it.
 *
 * It renders no widget. The page keeps one stable widget tree in view and edit mode; this
 * layer is a SIBLING of that tree, so mounting or unmounting it never remounts a widget.
 *
 * - **Binding.** The page's cells and zone grids register their DOM elements on the
 *   `dragBridge`. For every registered cell, `CellBinding` calls `useDraggable` and
 *   `useDroppable` with the widget id and binds both to the cell's element with
 *   `setNodeRef`. It then publishes the draggable's `listeners` back to the bridge, and the
 *   cell's drag surface and Move button forward `onMouseDown` / `onTouchStart` to them.
 *   `ZoneBinding` makes each zone grid a droppable (`zone:kpi`, `zone:panel`).
 *   dnd-kit's `attributes` are never read or spread: the cell's Move button is its
 *   accessible control, and the drag surface is `aria-hidden`.
 * - **Collision probe.** `collisionDetection` runs during render, so its only side effect
 *   is writing `probe`. It considers only droppables in the active widget's zone, prefers
 *   the cell under the pointer (`hitTestZone`, the same hit test the planner uses on
 *   predicted layouts), and otherwise reports `ZONE_HIT` inside the zone grid.
 * - **Live reorder.** `onDragMove` / `onDragOver` evaluate the probe through three gates:
 *   1. the droppable rects were re-measured after the last layout commit (rect-map
 *      identity);
 *   2. the pointer moved `REORDER_HYSTERESIS_PX` since the last reorder, measured in
 *      content space so auto-scroll counts as movement;
 *   3. `liveReorderTarget` + `planOrder` produce a new order.
 *   The page renders that order. `RemeasureOnLayoutChange` re-measures the droppables once
 *   that render commits, which reopens gate 1. A stationary pointer can never reorder twice.
 *   The page must render every order it is given: until it does, gate 1 stays shut.
 * - **Drift check.** dnd-kit re-measures only the droppables a ResizeObserver names, so a
 *   shift the layer did not cause (a banner or a taller panel above the zone) would leave
 *   every rect stale. Before each evaluation the active zone grid's live box is compared
 *   with its measured rect; on drift the layer re-measures everything and skips that move.
 * - **Orphaned start.** A release that lands before dnd-kit commits its own DragStart is
 *   dropped by dnd-kit (no end or cancel event). `OrphanedDragGuard` sees the committed
 *   state with no active drag after a start and reports the drag as cancelled, so the page
 *   never stays stuck mid-drag.
 * - **Announcements.** dnd-kit's own live region is silenced; the page's `onReorder`
 *   (`useLayoutEditor.dragReorder`) debounces the live position announcement.
 * - **Ghost.** A compact card in a portaled `DragOverlay` at z-index 55: above the sidebar
 *   (50), the edit bar (45) and BottomNav (40), and below Modal (60). It is the cell's size
 *   capped by `GHOST_MAX`, anchored so the pointer keeps its relative grab point
 *   (`ghostBox`). On drop it glides to the landing slot and fades out, with no animation
 *   under reduced motion.
 * - **Resize guard.** dnd-kit cancels a drag on every window `resize`, and its
 *   `handleCancel` is `private`, so no typed subclass can change that. The iOS toolbar
 *   collapsing mid-drag fires `resize`, so the layer registers a capture-phase window
 *   listener on mount, before any sensor can attach. While a drag is active and the
 *   Tailwind breakpoint is unchanged since pickup, it swallows the event and re-measures.
 *   A resize that crosses a breakpoint passes through and cancels the drag.
 */
import React, { useCallback, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import {
  DndContext,
  DragOverlay,
  MouseSensor,
  TouchSensor,
  getClientRect,
  useDndContext,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type AutoScrollOptions,
  type ClientRect,
  type CollisionDetection,
  type DragMoveEvent,
  type DragStartEvent,
  type DropAnimation,
  type MouseSensorOptions,
  type TouchSensorOptions,
  type UniqueIdentifier,
} from '@dnd-kit/core';
import { Bars2Icon } from '@heroicons/react/24/outline';
import type { EditableZone } from '../dashboardLayout';
import type { DragBridge } from './dragBridge';
import type { DashboardDragLayerProps } from './dragLayerTypes';
import {
  ZONE_HIT,
  breakpointOf,
  ghostBox,
  hitTestZone,
  liveReorderTarget,
  movedEnough,
  planOrder,
  type Breakpoint,
  type GhostBox,
  type PlanPoint,
  type PlanRect,
} from './dragPlanning';

/** Above the sidebar (50), the edit bar (45) and BottomNav (40); below Modal (60). */
export const DRAG_OVERLAY_Z_INDEX = 55;
/** Drop animation: the ghost glides to the landing slot while fading out. */
export const DROP_ANIMATION_MS = 180;
export const DROP_ANIMATION_EASING = 'cubic-bezier(0.2,0,0,1)';

const MOUSE_SENSOR_OPTIONS: MouseSensorOptions = { activationConstraint: { distance: 6 } };
const TOUCH_SENSOR_OPTIONS: TouchSensorOptions = { activationConstraint: { delay: 250, tolerance: 8 } };

/** Auto-scroll vertically only, when the pointer is within 20% of the viewport's top or bottom. */
const AUTO_SCROLL: AutoScrollOptions = { threshold: { x: 0, y: 0.2 }, layoutShiftCompensation: false };

/** dnd-kit's assertive live region stays empty; the page announces through its own polite region. */
const SILENT: Announcements = {
  onDragStart: () => undefined,
  onDragMove: () => undefined,
  onDragOver: () => undefined,
  onDragEnd: () => undefined,
  onDragCancel: () => undefined,
};
const ACCESSIBILITY = {
  announcements: SILENT,
  screenReaderInstructions: { draggable: '' },
  restoreFocus: false,
};

const ZONES: readonly EditableZone[] = ['kpi', 'panel'];
const OVERLAY_STYLE: React.CSSProperties = { pointerEvents: 'none' };

interface OverlayTransform {
  x: number;
  y: number;
  scaleX: number;
  scaleY: number;
}

/** CSS for a dnd-kit transform. A local copy keeps `@dnd-kit/utilities` from being imported directly. */
function transformToString({ x, y, scaleX, scaleY }: OverlayTransform): string {
  const tx = x ? Math.round(x) : 0;
  const ty = y ? Math.round(y) : 0;
  return `translate3d(${tx}px, ${ty}px, 0) scaleX(${scaleX}) scaleY(${scaleY})`;
}

const DROP_ANIMATION: DropAnimation = {
  duration: DROP_ANIMATION_MS,
  easing: DROP_ANIMATION_EASING,
  sideEffects: null,
  keyframes: ({ transform }) => [
    { opacity: 1, transform: transformToString(transform.initial) },
    { opacity: 0, transform: transformToString(transform.final) },
  ],
};

/** The droppable id of a zone's grid container. `:` never appears in a widget id. */
export function zoneDroppableId(zone: EditableZone): string {
  return `zone:${zone}`;
}

type RectMap = Map<UniqueIdentifier, ClientRect>;

interface Probe {
  /** Viewport pointer, as dnd-kit computed it for the collision. */
  pointer: PlanPoint | null;
  /** The droppable rect map the collision saw (each re-measure builds a new Map). */
  rects: RectMap | null;
  /** A cell id, `ZONE_HIT`, or null. */
  hitId: string | null;
}

interface Gate {
  /** A rect map in force before the last layout commit. Evaluation waits for a newer one. */
  staleRects: RectMap | null;
  /** Content-space pointer at the last reorder; null before the first, so the first reorder is immediate. */
  lastPointer: PlanPoint | null;
}

interface ActiveDrag {
  zone: EditableZone;
  id: string;
  /** The breakpoint at pickup: a resize within it keeps the drag, a resize across it cancels. */
  breakpoint: Breakpoint;
}

interface GhostProps {
  title: string;
  sizeLabel: string;
  box: GhostBox;
}

const EMPTY_PROBE: Probe = { pointer: null, rects: null, hitId: null };
/** A zone box that moved or resized by more than this since it was measured has drifted. */
const DRIFT_PX = 1;

function zoneOf(data: unknown): EditableZone | null {
  if (!data || typeof data !== 'object' || !('zone' in data)) return null;
  const { zone } = data;
  return zone === 'kpi' || zone === 'panel' ? zone : null;
}

function isZoneData(data: unknown): boolean {
  return Boolean(data && typeof data === 'object' && 'kind' in data && data.kind === 'zone');
}

function isTouchEvent(event: Event): event is TouchEvent {
  return 'touches' in event;
}

function hasClientPoint(event: Event): event is MouseEvent {
  return 'clientX' in event && 'clientY' in event;
}

/** The viewport point of a mouse or touch activator event. */
function pointOf(event: Event | null): PlanPoint | null {
  if (!event) return null;
  if (isTouchEvent(event)) {
    const touch =
      event.touches && event.touches.length > 0
        ? event.touches[0]
        : event.changedTouches && event.changedTouches.length > 0
          ? event.changedTouches[0]
          : null;
    return touch ? { x: touch.clientX, y: touch.clientY } : null;
  }
  return hasClientPoint(event) ? { x: event.clientX, y: event.clientY } : null;
}

/**
 * Where the pointer is relative to the content at pickup: the activation point plus
 * dnd-kit's scroll-adjusted translate. It moves when the pointer moves OR the page scrolls,
 * so hysteresis treats auto-scroll as movement.
 */
function contentPoint(event: DragMoveEvent): PlanPoint | null {
  const origin = pointOf(event.activatorEvent);
  return origin ? { x: origin.x + event.delta.x, y: origin.y + event.delta.y } : null;
}

/** The cells' bounding box: the zone rect fallback, used only when the zone grid is not registered. */
function boundsOf(ids: readonly string[], rects: RectMap): PlanRect | null {
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const id of ids) {
    const rect = rects.get(id);
    if (!rect) continue;
    left = Math.min(left, rect.left);
    top = Math.min(top, rect.top);
    right = Math.max(right, rect.left + rect.width);
    bottom = Math.max(bottom, rect.top + rect.height);
  }
  return left === Infinity ? null : { left, top, width: right - left, height: bottom - top };
}

// ── Bindings ─────────────────────────────────────────────────────────────────

interface CellBindingProps {
  bridge: DragBridge;
  id: string;
  zone: EditableZone;
  element: HTMLElement;
  enabled: boolean;
}

/** A page cell as both a draggable and a droppable (same id), its listeners published to the bridge. */
function CellBinding({ bridge, id, zone, element, enabled }: CellBindingProps) {
  const data = useMemo(() => ({ zone, kind: 'cell' }), [zone]);
  const { setNodeRef: setDraggableNode, listeners } = useDraggable({ id, data, disabled: !enabled });
  const { setNodeRef: setDroppableNode } = useDroppable({ id, data });

  useLayoutEffect(() => {
    setDraggableNode(element);
    setDroppableNode(element);
    return () => {
      setDraggableNode(null);
      setDroppableNode(null);
    };
  }, [element, setDraggableNode, setDroppableNode]);

  useLayoutEffect(() => {
    bridge.setActivators(id, listeners ?? null);
    return () => bridge.setActivators(id, null);
  }, [bridge, id, listeners]);

  return null;
}

/** A zone's grid container as a droppable: the row-band fallback for gutters and row-end holes. */
function ZoneBinding({ zone, element }: { zone: EditableZone; element: HTMLElement }) {
  const data = useMemo(() => ({ zone, kind: 'zone' }), [zone]);
  const { setNodeRef } = useDroppable({ id: zoneDroppableId(zone), data });
  useLayoutEffect(() => {
    setNodeRef(element);
    return () => setNodeRef(null);
  }, [element, setNodeRef]);
  return null;
}

interface RemeasureProps {
  layoutKey: string;
  /** Receives dnd-kit's `measureDroppableContainers` (the resize guard calls it). */
  measureRef: React.MutableRefObject<(() => void) | null>;
  /** Runs first on every layout commit: marks the rect map in force as stale. */
  onLayoutCommit: () => void;
}

/**
 * After a layout change COMMITS (live reorder, width, membership), re-measure every
 * droppable. dnd-kit then builds a new rect `Map` from the committed DOM, which reopens
 * gate 1. No measurement can come from the pre-commit DOM: the map in force at commit time
 * is marked stale first, including one a ResizeObserver produced before the commit.
 */
function RemeasureOnLayoutChange({ layoutKey, measureRef, onLayoutCommit }: RemeasureProps) {
  const { measureDroppableContainers } = useDndContext();

  useLayoutEffect(() => {
    // An empty id list re-measures every droppable (a fresh array each call).
    measureRef.current = () => measureDroppableContainers([]);
    return () => {
      measureRef.current = null;
    };
  }, [measureDroppableContainers, measureRef]);

  useLayoutEffect(() => {
    onLayoutCommit();
    measureDroppableContainers([]);
  }, [layoutKey, measureDroppableContainers, onLayoutCommit]);

  return null;
}

/**
 * dnd-kit calls `onDragStart` synchronously, but reads its own `active` from a layout effect
 * of the DragStart commit. A release that lands before that commit (a quick flick on a busy
 * main thread, or a finger lifted as the long-press fires) builds no event, so neither
 * `onDragEnd` nor `onDragCancel` runs. `startSeq` is bumped in `onDragStart`, in the same
 * batch as dnd-kit's DragStart dispatch: a commit that carries the new `startSeq` with no
 * `active` is exactly that orphaned start. A normal end clears the layer's drag before any
 * commit, so `onOrphaned` is then a no-op.
 */
function OrphanedDragGuard({ startSeq, onOrphaned }: { startSeq: number; onOrphaned: () => void }) {
  const { active } = useDndContext();
  useLayoutEffect(() => {
    if (active === null) onOrphaned();
  }, [startSeq, active, onOrphaned]);
  return null;
}

// ── Ghost ────────────────────────────────────────────────────────────────────

/**
 * The compact card under the pointer: grip, title, width label and three hairline "content"
 * bars. It is decorative (`aria-hidden`), and its props are frozen at pickup, because the
 * overlay keeps rendering it through the drop animation after the drag has ended.
 */
function DragGhost({ title, sizeLabel, box }: GhostProps) {
  return (
    <div
      aria-hidden="true"
      data-drag-ghost=""
      className="absolute flex flex-col gap-2 overflow-hidden border border-fd-blue bg-fd-panel/95 p-3 shadow-lg"
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
    >
      <div className="flex min-w-0 items-center gap-2">
        <Bars2Icon className="h-4 w-4 flex-shrink-0 text-fd-blue" aria-hidden="true" />
        <span className="truncate text-sm font-semibold text-fd-ink">{title}</span>
        <span className="ml-auto flex-shrink-0 text-[10px] font-medium uppercase tracking-wide text-slate-400">
          {sizeLabel}
        </span>
      </div>
      <div className="h-px w-full flex-shrink-0 bg-fd-line-bright" />
      <div className="h-px w-3/4 flex-shrink-0 bg-fd-line-bright" />
      <div className="h-px w-1/2 flex-shrink-0 bg-fd-line-bright" />
    </div>
  );
}

// ── Layer ────────────────────────────────────────────────────────────────────

export default function DashboardDragLayer(props: DashboardDragLayerProps) {
  const { bridge, enabled, layoutKey, reducedMotion } = props;
  const snapshot = useSyncExternalStore(bridge.subscribe, bridge.getSnapshot, bridge.getSnapshot);

  // Handlers run outside render (sensor events, effects); they read the latest props here.
  const latest = useRef(props);
  useLayoutEffect(() => {
    latest.current = props;
  });

  const mounted = useRef(false);
  const drag = useRef<ActiveDrag | null>(null);
  const probe = useRef<Probe>(EMPTY_PROBE);
  const gate = useRef<Gate>({ staleRects: null, lastPointer: null });
  const measure = useRef<(() => void) | null>(null);
  const [ghost, setGhost] = useState<GhostProps | null>(null);
  const [startSeq, setStartSeq] = useState(0);

  const sensors = useSensors(
    useSensor(MouseSensor, MOUSE_SENSOR_OPTIONS),
    useSensor(TouchSensor, TOUCH_SENSOR_OPTIONS)
  );

  // The resize guard, registered on mount: before any sensor attaches its own listener.
  useLayoutEffect(() => {
    mounted.current = true;
    const guard = (event: Event) => {
      const current = drag.current;
      if (!current || breakpointOf(window.innerWidth) !== current.breakpoint) return;
      event.stopImmediatePropagation();
      measure.current?.();
    };
    window.addEventListener('resize', guard, { capture: true });
    return () => {
      mounted.current = false;
      drag.current = null;
      window.removeEventListener('resize', guard, { capture: true });
    };
  }, []);

  const collisionDetection = useCallback<CollisionDetection>(
    ({ active, droppableContainers, droppableRects, pointerCoordinates }) => {
      const zone = zoneOf(active.data.current);
      let hitId: string | null = null;
      let collisionId: string | null = null;
      if (zone && pointerCoordinates) {
        const cellIds: string[] = [];
        let zoneId: string | null = null;
        for (const container of droppableContainers) {
          const data = container.data.current;
          if (zoneOf(data) !== zone) continue;
          if (isZoneData(data)) zoneId = String(container.id);
          else cellIds.push(String(container.id));
        }
        const zoneRect = (zoneId !== null && droppableRects.get(zoneId)) || boundsOf(cellIds, droppableRects);
        hitId = zoneRect ? hitTestZone(cellIds, droppableRects, zoneRect, pointerCoordinates) : null;
        collisionId = hitId === ZONE_HIT ? zoneId : hitId;
      }
      probe.current = { pointer: pointerCoordinates, rects: droppableRects, hitId };
      return collisionId === null ? [] : [{ id: collisionId }];
    },
    []
  );

  const evaluate = useCallback((event: DragMoveEvent) => {
    const current = drag.current;
    const { pointer, rects, hitId } = probe.current;
    if (!current || !mounted.current || !pointer || !rects) return;
    // Drift: content above or inside the zone moved it since the rects were measured (a
    // banner appeared, a panel grew). Re-measure everything; the next move evaluates fresh
    // rects. dnd-kit's rects are scroll-compensated, and the zone grid is never transformed.
    const zoneElement = latest.current.bridge.getSnapshot().zones[current.zone];
    const measuredZone = rects.get(zoneDroppableId(current.zone));
    if (zoneElement && measuredZone) {
      const live = zoneElement.getBoundingClientRect();
      if (
        Math.abs(live.top - measuredZone.top) > DRIFT_PX ||
        Math.abs(live.left - measuredZone.left) > DRIFT_PX ||
        Math.abs(live.height - measuredZone.height) > DRIFT_PX
      ) {
        measure.current?.();
        return;
      }
    }
    // Gate 1: only droppables measured after the last layout commit.
    if (rects === gate.current.staleRects) return;
    // Gate 2: hysteresis since the last reorder.
    const travelled = contentPoint(event) ?? pointer;
    if (!movedEnough(gate.current.lastPointer, travelled)) return;
    // Gate 3: a stable target that changes the order.
    const { orders, onReorder } = latest.current;
    const order = orders[current.zone];
    const zoneRect = rects.get(zoneDroppableId(current.zone)) ?? boundsOf(order, rects);
    if (!zoneRect) return;
    const target = liveReorderTarget({ order, activeId: current.id, hitId, pointer, rects, zoneRect });
    const next = planOrder(order, current.id, target);
    if (next === order) return;
    gate.current = { staleRects: rects, lastPointer: travelled };
    onReorder(current.zone, next);
  }, []);

  const markRectsStale = useCallback(() => {
    if (drag.current) gate.current = { ...gate.current, staleRects: probe.current.rects };
  }, []);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const zone = zoneOf(event.active.data.current);
    if (!zone || !mounted.current) return;
    const id = String(event.active.id);
    const current: ActiveDrag = { zone, id, breakpoint: breakpointOf(window.innerWidth) };
    drag.current = current;
    probe.current = EMPTY_PROBE;
    gate.current = { staleRects: null, lastPointer: null };
    // Same batch as dnd-kit's DragStart dispatch: see OrphanedDragGuard.
    setStartSeq(seq => seq + 1);

    const { bridge: currentBridge, describe, onDragStart } = latest.current;
    const cell = currentBridge.getSnapshot().cells.find(entry => entry.id === id && entry.zone === zone);
    if (cell) {
      // Transform-agnostic, like dnd-kit's own measure of the overlay's initial rect, so a
      // cell picked up mid-FLIP still anchors the ghost where the overlay wrapper sits.
      const rect = getClientRect(cell.element, { ignoreTransform: true });
      const point = pointOf(event.activatorEvent);
      const grab = point ? { x: point.x - rect.left, y: point.y - rect.top } : { x: 0, y: 0 };
      const { title, sizeLabel } = describe(id);
      setGhost({ title, sizeLabel, box: ghostBox(rect, grab) });
    } else {
      setGhost(null);
    }
    if (isTouchEvent(event.activatorEvent)) navigator.vibrate?.(10);
    onDragStart(zone, id);
  }, []);

  const finish = useCallback((cancelled: boolean) => {
    const current = drag.current;
    drag.current = null;
    probe.current = EMPTY_PROBE;
    // The sensor outlives an unmounted layer until the pointer is released: never call back then.
    if (!mounted.current) return;
    setGhost(null);
    if (!current) return;
    if (cancelled) latest.current.onDragCancel(current.zone, current.id);
    else latest.current.onDragEnd(current.zone, current.id);
  }, []);

  const handleDragEnd = useCallback(() => finish(false), [finish]);
  const handleDragCancel = useCallback(() => finish(true), [finish]);
  /** A start dnd-kit never committed (see OrphanedDragGuard): the drag is over, report it cancelled. */
  const handleOrphaned = useCallback(() => {
    if (drag.current) finish(true);
  }, [finish]);

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      autoScroll={AUTO_SCROLL}
      accessibility={ACCESSIBILITY}
      onDragStart={handleDragStart}
      onDragMove={evaluate}
      onDragOver={evaluate}
      onDragEnd={handleDragEnd}
      onDragCancel={handleDragCancel}
    >
      {snapshot.cells.map(cell => (
        <CellBinding
          key={`${cell.zone}:${cell.id}`}
          bridge={bridge}
          id={cell.id}
          zone={cell.zone}
          element={cell.element}
          enabled={enabled}
        />
      ))}
      {ZONES.map(zone => {
        const element = snapshot.zones[zone];
        return element ? <ZoneBinding key={zone} zone={zone} element={element} /> : null;
      })}
      <RemeasureOnLayoutChange layoutKey={layoutKey} measureRef={measure} onLayoutCommit={markRectsStale} />
      <OrphanedDragGuard startSeq={startSeq} onOrphaned={handleOrphaned} />
      {createPortal(
        <DragOverlay
          zIndex={DRAG_OVERLAY_Z_INDEX}
          style={OVERLAY_STYLE}
          dropAnimation={reducedMotion ? null : DROP_ANIMATION}
        >
          {ghost ? <DragGhost title={ghost.title} sizeLabel={ghost.sizeLabel} box={ghost.box} /> : null}
        </DragOverlay>,
        document.body
      )}
    </DndContext>
  );
}

/**
 * Dashboard edit mode: the seam between the page's widget cells (main bundle) and the lazily
 * loaded drag layer (the only module that imports `@dnd-kit/core`). No dnd-kit types here.
 *
 * - The page's cells register their DOM element per widget id and zone (`setCell`, or the
 *   stable ref callback `cellRef(id, zone)`); each zone's grid registers too (`setZone` /
 *   `zoneRef(zone)`). The layer reads them through `subscribe` + `getSnapshot` (the
 *   `useSyncExternalStore` contract) and binds each element with dnd-kit's `setNodeRef`.
 * - The layer publishes each cell's dnd-kit `listeners` back with `setActivators(id, map)`.
 *   They are read at event time, so publishing them does not notify subscribers.
 * - A cell's drag surface or Move button forwards its `onMouseDown` / `onTouchStart` to
 *   `start(id, event)`, which calls the matching listener (`onMouseDown` / `onTouchStart`).
 *   dnd-kit's activator reads only `event.nativeEvent`, and it ignores an event whose
 *   default was already prevented — so the forwarding handler must not call
 *   `preventDefault()` first.
 */
import type React from 'react';
import type { EditableZone } from '../dashboardLayout';

export interface DragBridgeCell {
  id: string;
  zone: EditableZone;
  element: HTMLElement;
}

/** Immutable; a new object only when a registration actually changed. */
export interface DragBridgeSnapshot {
  /** Registered cells, in registration order. */
  cells: readonly DragBridgeCell[];
  zones: Readonly<Partial<Record<EditableZone, HTMLElement>>>;
}

/**
 * A cell's activator listeners, keyed by React event prop name (`onMouseDown`,
 * `onTouchStart`). Values are `unknown` so dnd-kit's `SyntheticListenerMap`
 * (`Record<string, Function>`) is accepted as is; `start` calls only functions.
 */
export type DragActivatorMap = Readonly<Record<string, unknown>>;

export type DragStartEvent = React.MouseEvent<Element> | React.TouchEvent<Element>;

export interface DragBridge {
  /** Register (element) or unregister (null) a widget cell. */
  setCell(id: string, zone: EditableZone, element: HTMLElement | null): void;
  /** A stable ref callback for `setCell(id, zone, …)` — the same function for the same id and zone. */
  cellRef(id: string, zone: EditableZone): (element: HTMLElement | null) => void;
  /** Register (element) or unregister (null) a zone's grid container. */
  setZone(zone: EditableZone, element: HTMLElement | null): void;
  /** A stable ref callback for `setZone(zone, …)`. */
  zoneRef(zone: EditableZone): (element: HTMLElement | null) => void;
  /** The layer publishes a cell's dnd-kit listeners (null when unbound or disabled). Does not notify. */
  setActivators(id: string, activators: DragActivatorMap | null): void;
  /**
   * Forward a pointer-down from the cell's surface or Move button to the layer. Returns
   * true when a listener received it; false (a no-op) without activators for the id or for
   * that event type.
   */
  start(id: string, event: DragStartEvent): boolean;
  subscribe(listener: () => void): () => void;
  getSnapshot(): DragBridgeSnapshot;
}

const ACTIVATOR_FOR_EVENT: Readonly<Record<string, string>> = {
  mousedown: 'onMouseDown',
  touchstart: 'onTouchStart',
};

const EMPTY_SNAPSHOT: DragBridgeSnapshot = { cells: [], zones: {} };

function sameSnapshot(a: DragBridgeSnapshot, b: DragBridgeSnapshot): boolean {
  if (a.cells.length !== b.cells.length) return false;
  for (let index = 0; index < a.cells.length; index += 1) {
    const left = a.cells[index];
    const right = b.cells[index];
    if (left.id !== right.id || left.zone !== right.zone || left.element !== right.element) return false;
  }
  return a.zones.kpi === b.zones.kpi && a.zones.panel === b.zones.panel;
}

export function createDragBridge(): DragBridge {
  const cells = new Map<string, DragBridgeCell>();
  const zones: Partial<Record<EditableZone, HTMLElement>> = {};
  const activators = new Map<string, DragActivatorMap>();
  const listeners = new Set<() => void>();
  const cellRefs = new Map<string, (element: HTMLElement | null) => void>();
  const zoneRefs = new Map<EditableZone, (element: HTMLElement | null) => void>();
  let snapshot = EMPTY_SNAPSHOT;
  let stale = false;

  const notify = () => {
    stale = true;
    Array.from(listeners).forEach(listener => listener());
  };

  const bridge: DragBridge = {
    setCell(id, zone, element) {
      const current = cells.get(id);
      if (element) {
        if (current && current.zone === zone && current.element === element) return;
        cells.set(id, { id, zone, element });
      } else {
        if (!current) return;
        cells.delete(id);
      }
      notify();
    },

    cellRef(id, zone) {
      const key = `${zone}\n${id}`;
      let ref = cellRefs.get(key);
      if (!ref) {
        ref = element => bridge.setCell(id, zone, element);
        cellRefs.set(key, ref);
      }
      return ref;
    },

    setZone(zone, element) {
      if ((zones[zone] ?? null) === element) return;
      if (element) zones[zone] = element;
      else delete zones[zone];
      notify();
    },

    zoneRef(zone) {
      let ref = zoneRefs.get(zone);
      if (!ref) {
        ref = element => bridge.setZone(zone, element);
        zoneRefs.set(zone, ref);
      }
      return ref;
    },

    setActivators(id, map) {
      if (map) activators.set(id, map);
      else activators.delete(id);
    },

    start(id, event) {
      const name = ACTIVATOR_FOR_EVENT[event.type];
      const handler = name ? activators.get(id)?.[name] : undefined;
      if (typeof handler !== 'function') return false;
      handler(event);
      return true;
    },

    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },

    getSnapshot() {
      if (!stale) return snapshot;
      stale = false;
      const next: DragBridgeSnapshot = { cells: Array.from(cells.values()), zones: { ...zones } };
      if (!sameSnapshot(snapshot, next)) snapshot = next;
      return snapshot;
    },
  };
  return bridge;
}

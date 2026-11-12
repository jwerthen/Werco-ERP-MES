/**
 * The props contract between the dashboard page (main bundle) and the lazily loaded drag
 * layer (`DashboardDragLayer.tsx`, the only `@dnd-kit/core` importer). Shared by the real
 * layer and the typed test fake (`test-utils/fakeDragLayer.tsx`), so a fake can never drift
 * from what the page passes. No dnd-kit types here.
 */
import type { EditableZone } from '../dashboardLayout';
import type { DragBridge } from './dragBridge';

export interface DragWidgetDescription {
  /** The widget's title (the ghost card and announcements). */
  title: string;
  /** The ghost card's width label, e.g. "Two-thirds" (panels) or "Key metric" (KPI tiles). */
  sizeLabel: string;
}

export interface DashboardDragLayerProps {
  /** Where the page's cells and zone grids register; the layer binds them and publishes listeners back. */
  bridge: DragBridge;
  /** False while a save / reset / reload is in flight or after a 409: no drag may start. */
  enabled: boolean;
  /** Each zone's order AS RENDERED (the live session order for the dragged zone). */
  orders: { kpi: readonly string[]; panel: readonly string[] };
  /** Changes whenever the rendered layout changes (order, widths, membership); re-measures droppables after commit. */
  layoutKey: string;
  /** prefers-reduced-motion: no drop animation. */
  reducedMotion: boolean;
  describe: (id: string) => DragWidgetDescription;
  /** A pointer / touch drag lifted widget `id` (pause polling, start the session, announce). */
  onDragStart: (zone: EditableZone, id: string) => void;
  /** A live reorder of `zone` (capture FLIP, then dispatch the session reorder). */
  onReorder: (zone: EditableZone, next: readonly string[]) => void;
  /** Dropped: commit the session (one Undo step), focus the widget's Move button. */
  onDragEnd: (zone: EditableZone, id: string) => void;
  /**
   * Escape, tab switch, a breakpoint-crossing resize, or a release dnd-kit dropped before its
   * start committed: glide back to the starting order. The live position announcement is the
   * page's job (`useLayoutEditor.dragReorder` debounces it); the layer announces nothing.
   */
  onDragCancel: (zone: EditableZone, id: string) => void;
}

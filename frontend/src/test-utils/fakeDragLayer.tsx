/**
 * A typed fake of the dashboard drag layer, so page and component suites never load
 * `@dnd-kit/core`. It renders nothing and captures the latest props into `dragHarness`, which
 * drives the page's drag callbacks directly (each inside `act`).
 *
 * Wire it with:
 *
 *   jest.mock('../components/dashboard/editor/loadDragLayer', () =>
 *     jest.requireActual('../test-utils/fakeDragLayer').createFakeLoadDragLayerModule()
 *   );
 *
 * (paths relative to the test file). The module's `loadDashboardDragLayer` is a `jest.fn`
 * resolving to `FakeDragLayer`, so a test can `mockRejectedValueOnce` a chunk failure;
 * `dragHarness.reset()` in `beforeEach`.
 */
import { useEffect, type ComponentType } from 'react';
import { act } from '@testing-library/react';
import type { EditableZone } from '../components/dashboard/dashboardLayout';
import type { DashboardDragLayerProps } from '../components/dashboard/editor/dragLayerTypes';
import type * as LoadDragLayerModule from '../components/dashboard/editor/loadDragLayer';

interface DragHarness {
  /** The props of the last render, or null when the layer is not mounted. */
  props: DashboardDragLayerProps | null;
  /** True while mounted. */
  mounted: boolean;
  /** How many times the layer mounted (it mounts on entering edit mode, unmounts on exit). */
  mounts: number;
  renders: number;
  /** The latest props; throws when the layer is not mounted (edit mode is off). */
  latest(): DashboardDragLayerProps;
  /** A pickup: a no-op while `enabled` is false, as dnd-kit's `disabled` draggable starts nothing. */
  start(zone: EditableZone, id: string): void;
  reorder(zone: EditableZone, next: readonly string[]): void;
  end(zone: EditableZone, id: string): void;
  cancel(zone: EditableZone, id: string): void;
  reset(): void;
}

export const dragHarness: DragHarness = {
  props: null,
  mounted: false,
  mounts: 0,
  renders: 0,
  latest() {
    if (!dragHarness.mounted || !dragHarness.props) {
      throw new Error('The drag layer is not mounted — enter edit mode first.');
    }
    return dragHarness.props;
  },
  start(zone, id) {
    const props = dragHarness.latest();
    if (!props.enabled) return;
    act(() => props.onDragStart(zone, id));
  },
  reorder(zone, next) {
    act(() => dragHarness.latest().onReorder(zone, next));
  },
  end(zone, id) {
    act(() => dragHarness.latest().onDragEnd(zone, id));
  },
  cancel(zone, id) {
    act(() => dragHarness.latest().onDragCancel(zone, id));
  },
  reset() {
    dragHarness.props = null;
    dragHarness.mounted = false;
    dragHarness.mounts = 0;
    dragHarness.renders = 0;
  },
};

export const FakeDragLayer: ComponentType<DashboardDragLayerProps> = function FakeDragLayer(props) {
  dragHarness.props = props;
  dragHarness.renders += 1;
  useEffect(() => {
    dragHarness.mounted = true;
    dragHarness.mounts += 1;
    return () => {
      dragHarness.mounted = false;
      dragHarness.props = null;
    };
  }, []);
  // The real layer renders nothing in place either (its overlay and live region portal to <body>).
  return null;
};

type LoadDragLayer = typeof LoadDragLayerModule;

/** Every export of the real loader, as a `jest.Mock` of the same signature (a new export fails to compile here). */
export type FakeLoadDragLayerModule = {
  [K in keyof LoadDragLayer]: jest.Mock<ReturnType<LoadDragLayer[K]>, Parameters<LoadDragLayer[K]>>;
};

/** A stand-in for `editor/loadDragLayer` that resolves to `FakeDragLayer`. */
export function createFakeLoadDragLayerModule(): FakeLoadDragLayerModule {
  return {
    loadDashboardDragLayer: jest.fn(() => Promise.resolve(FakeDragLayer)),
    prefetchDashboardDragLayer: jest.fn<void, []>(),
  };
}

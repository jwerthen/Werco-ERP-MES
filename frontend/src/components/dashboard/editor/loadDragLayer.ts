/**
 * Lazy loader for the dashboard drag layer, so `@dnd-kit/core` lands in its own async chunk
 * and never in the landing route's bundle.
 *
 * - `loadDashboardDragLayer()` is memoized: every caller shares one import. A rejected
 *   import (a chunk 404 after a deploy, a network blip) clears the memo, so the next call
 *   imports again rather than replaying the failure.
 * - `prefetchDashboardDragLayer()` warms it (Customize hover / focus) and swallows errors:
 *   a failed prefetch is retried by the real load and reported there.
 */
import type { ComponentType } from 'react';
import type { DashboardDragLayerProps } from './dragLayerTypes';

export type DashboardDragLayerComponent = ComponentType<DashboardDragLayerProps>;

let pending: Promise<DashboardDragLayerComponent> | null = null;

export function loadDashboardDragLayer(): Promise<DashboardDragLayerComponent> {
  if (pending) return pending;
  const attempt: Promise<DashboardDragLayerComponent> = import('./DashboardDragLayer').then(module => module.default);
  pending = attempt;
  attempt.catch(() => {
    if (pending === attempt) pending = null;
  });
  return attempt;
}

export function prefetchDashboardDragLayer(): void {
  loadDashboardDragLayer().catch(() => undefined);
}

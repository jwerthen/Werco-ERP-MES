/**
 * What every dashboard widget reads. The page provides it; widgets never call the api
 * module directly for their data, so each widget is testable with a provider and no api
 * mocks (see `renderWithDashboardContext` in `src/test-utils/dashboardWidgets.tsx`).
 */
import React, { createContext, useContext } from 'react';
import { EMPTY_SOURCE_STATE, type DashboardSourceKey, type SourceState } from './widgetTypes';

export interface DashboardWidgetEnv {
  /** Data per source key. A needed key with no entry yet reads as loading. */
  sources: Partial<Record<DashboardSourceKey, SourceState>>;
  /** True when the viewer may open `href` — the exact predicate PrivateRoute enforces. */
  canOpen: (href: string) => boolean;
  /** Whether another widget is on the dashboard (cross-links degrade when their target is hidden). */
  isVisible: (widgetId: string) => boolean;
  /** Ticks every 30 s; use for elapsed-time displays. */
  nowMs: number;
  /** Ask the page to refresh. `forced` = a user action (bypasses refresh floors, not hard minimums). */
  refresh: (forced: boolean) => void;
}

const defaultEnv: DashboardWidgetEnv = {
  sources: {},
  canOpen: () => false,
  isVisible: () => false,
  nowMs: 0,
  refresh: () => undefined,
};

const DashboardWidgetContext = createContext<DashboardWidgetEnv>(defaultEnv);

export function DashboardWidgetProvider({ value, children }: { value: DashboardWidgetEnv; children: React.ReactNode }) {
  return <DashboardWidgetContext.Provider value={value}>{children}</DashboardWidgetContext.Provider>;
}

export function useDashboardWidgetEnv(): DashboardWidgetEnv {
  return useContext(DashboardWidgetContext);
}

/** The state of one source. Reads as loading until the loader has a result for it. */
export function useDashboardSource<T>(key: DashboardSourceKey): SourceState<T> {
  const { sources } = useContext(DashboardWidgetContext);
  return (sources[key] as SourceState<T> | undefined) ?? (EMPTY_SOURCE_STATE as SourceState<T>);
}

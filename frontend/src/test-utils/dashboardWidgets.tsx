/**
 * Test helper for dashboard widgets: render inside a router and a DashboardWidgetProvider
 * whose sources you control. Widgets read all data from context, so no api mocks needed.
 */
import React from 'react';
import { render, type RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DashboardWidgetProvider, type DashboardWidgetEnv } from '../components/dashboard/DashboardWidgetContext';
import type { DashboardSourceKey, SourceState } from '../components/dashboard/widgetTypes';

/** A settled source with `data`. */
export function readySource<T>(data: T, overrides: Partial<SourceState<T>> = {}): SourceState<T> {
  return { data, error: null, loading: false, forbidden: false, updatedAt: new Date('2026-11-11T15:00:00Z'), ...overrides };
}

export function renderWithDashboardContext(
  ui: React.ReactElement,
  {
    sources = {},
    env = {},
  }: {
    sources?: Partial<Record<DashboardSourceKey, SourceState>>;
    env?: Partial<Omit<DashboardWidgetEnv, 'sources'>>;
  } = {}
): RenderResult & { refresh: jest.Mock } {
  const refresh = jest.fn();
  const value: DashboardWidgetEnv = {
    sources,
    canOpen: () => true,
    isVisible: () => true,
    nowMs: new Date('2026-11-11T15:00:00Z').getTime(),
    refresh,
    ...env,
  };
  const result = render(
    <MemoryRouter>
      <DashboardWidgetProvider value={value}>{ui}</DashboardWidgetProvider>
    </MemoryRouter>
  );
  return Object.assign(result, { refresh: (env.refresh as jest.Mock | undefined) ?? refresh });
}

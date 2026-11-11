/**
 * Page state the CORE widgets (the sections the dashboard showed before customization)
 * read besides the shared DashboardWidgetContext: the shop-floor heartbeat
 * (`GET /shop-floor/dashboard`), its refresh error, and the operation-details opener.
 * Opt-in widgets never read this — they get everything from DashboardWidgetContext.
 */
import React, { createContext, useContext } from 'react';
import type { ActiveAssignment, DashboardData } from '../../../../types';

export interface CoreDashboardEnv {
  /** The last verified shop-floor heartbeat. */
  data: DashboardData | null;
  /** '' while the heartbeat is fresh; otherwise the page's shop-data error text. */
  shopError: string;
  /** Open the operation-details dialog for a live assignment. */
  openOperation: (assignment: ActiveAssignment) => void;
}

const CoreDashboardContext = createContext<CoreDashboardEnv>({
  data: null,
  shopError: '',
  openOperation: () => undefined,
});

export function CoreDashboardProvider({ value, children }: { value: CoreDashboardEnv; children: React.ReactNode }) {
  return <CoreDashboardContext.Provider value={value}>{children}</CoreDashboardContext.Provider>;
}

export function useCoreDashboard(): CoreDashboardEnv {
  return useContext(CoreDashboardContext);
}

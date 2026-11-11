/**
 * A person's saved dashboard layout (`/api/v1/dashboard/layout`), mirroring
 * backend/app/schemas/dashboard_layout.py.
 *
 * The server stores ordered `{ id, size }` placements and validates only their SHAPE;
 * the widget catalog is the SPA's (components/dashboard/widgetRegistry.ts). `layout:
 * null` means the stock default — only `POST /dashboard/layout/reset` returns a user to
 * it. An empty `widgets` list is a deliberately empty dashboard, never "the default".
 */

/** Matches the server's MAX_DASHBOARD_WIDGETS. */
export const MAX_DASHBOARD_WIDGETS = 64;

export interface DashboardWidgetPlacement {
  /** ^[a-z][a-z0-9_.]{0,63}$ — a widget id from the SPA catalog, e.g. `kpi.overdue`. */
  id: string;
  /** Panel width (third | narrow | half | wide | two_thirds | full); null = the widget's default. */
  size?: string | null;
}

export interface DashboardLayoutValues {
  format_version: 1;
  widgets: DashboardWidgetPlacement[];
}

export interface DashboardLayoutResponse {
  company_id: number;
  /** 0 = no row yet (the stock default). */
  version: number;
  /** null = the stock default. */
  layout: DashboardLayoutValues | null;
  updated_at: string | null;
  /** False for read-only company views and API/kiosk credentials: render, never save. */
  can_edit: boolean;
}

/** Compare-and-swap fields, taken from the LAST server response (never a client company context). */
export interface DashboardLayoutCommand {
  expected_company_id: number;
  expected_version: number;
}

export interface DashboardLayoutSave extends DashboardLayoutCommand {
  layout: DashboardLayoutValues;
}

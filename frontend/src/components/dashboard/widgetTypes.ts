/**
 * Contracts for the customizable dashboard (`/`).
 *
 * The widget catalog is a FRONTEND concern: the server stores a user's layout as an
 * ordered list of `{ id, size }` placements and validates only their shape, because the
 * SPA and the API deploy independently. A stored id grants nothing — it names a component
 * this bundle may or may not render, and every data endpoint a widget reads enforces its
 * own authorization. See docs/DASHBOARD.md → "Customizing the dashboard".
 */
import type { ComponentType } from 'react';
import type { UserRole } from '../../types';
import type { Permission } from '../../utils/permissions';

/** Where a widget renders. Banners toggle but keep registry order; KPIs and panels reorder. */
export type DashboardZone = 'banner' | 'kpi' | 'panel';

/**
 * Panel widths on the xl 12-column grid. `narrow` (5) + `wide` (7) reproduce the classic
 * cockpit exactly; the class map lives in one place (`PANEL_SIZE_CLASSES`).
 */
export type PanelSize = 'third' | 'narrow' | 'half' | 'wide' | 'two_thirds' | 'full';

export const PANEL_SIZES: readonly PanelSize[] = ['third', 'narrow', 'half', 'wide', 'two_thirds', 'full'];

export const PANEL_SIZE_LABELS: Record<PanelSize, string> = {
  third: 'Third',
  narrow: 'Narrow',
  half: 'Half',
  wide: 'Wide',
  two_thirds: 'Two-thirds',
  full: 'Full width',
};

/** Static strings so Tailwind generates every class (never build these dynamically). */
export const PANEL_SIZE_CLASSES: Record<PanelSize, string> = {
  third: 'xl:col-span-4',
  narrow: 'xl:col-span-5',
  half: 'xl:col-span-6',
  wide: 'xl:col-span-7',
  two_thirds: 'lg:col-span-2 xl:col-span-8',
  full: 'lg:col-span-2 xl:col-span-12',
};

export function isPanelSize(value: unknown): value is PanelSize {
  return typeof value === 'string' && (PANEL_SIZES as readonly string[]).includes(value);
}

/**
 * Data sources the page's keyed loader can fetch. `GET /shop-floor/dashboard` is NOT a
 * source: it is the page heartbeat and is always fetched.
 *
 * Core keys (`sources/coreSources.ts`) back sections the dashboard showed before
 * customization; extra keys (`sources/extraSources.ts`) back the opt-in widgets.
 */
export type CoreSourceKey = 'quality' | 'equipment' | 'stock' | 'capacity';
export type ExtraSourceKey =
  | 'wallboard'
  | 'workOrdersOverdue'
  | 'workOrdersDueToday'
  | 'actionInbox'
  | 'downtime'
  | 'maintenance'
  | 'inspectionStats'
  | 'inspectionQueue'
  | 'deliveryKpis'
  | 'visitors'
  | 'notifications';
export type DashboardSourceKey = CoreSourceKey | ExtraSourceKey;

export interface SourceSpec<T = unknown> {
  /** Human label for the page's "widget errors" banner, e.g. 'Quality'. */
  label: string;
  /** Fetch the data. Api methods take no AbortSignal; the loader's per-key sequence guard discards stale results. */
  fetch: () => Promise<T>;
  /** Non-forced refreshes (30 s poll, WebSocket pushes) skip this key until it is this old. */
  minIntervalMs: number;
  /** A manual (forced) refresh still waits this long between fetches (expensive sources). */
  hardMinIntervalMs?: number;
  /** Keep the last result in a module-level cache (keyed by session scope) so the floor survives remounts of `/`. */
  cacheTtlMs?: number;
}

export interface SourceState<T = unknown> {
  data: T | null;
  /** 'Could not refresh' style message; data (if any) is the last verified value. */
  error: string | null;
  /** True until the first result (or failure) for a needed key. */
  loading: boolean;
  /** HTTP 403: terminal until the session scope changes; render "Not available for your role". */
  forbidden: boolean;
  /** When `data` was last verified. */
  updatedAt: Date | null;
}

export const EMPTY_SOURCE_STATE: SourceState<never> = {
  data: null,
  error: null,
  loading: true,
  forbidden: false,
  updatedAt: null,
};

/**
 * What a widget's eligibility predicate may consult. `can` is `usePermissions().can`
 * (platform_admin passes every permission, mirroring the backend); `hasRole` mirrors a
 * backend `require_role([...])` list, superuser and platform_admin included.
 */
export interface WidgetEligibilityContext {
  can: (permission: Permission) => boolean;
  hasRole: (roles: UserRole[]) => boolean;
  isAdmin: boolean;
}

export type WidgetCategory =
  | 'Alerts'
  | 'Key metrics'
  | 'Shop floor'
  | 'Work orders'
  | 'Quality'
  | 'Supply chain'
  | 'Planning'
  | 'Insights'
  | 'People';

export interface DashboardWidgetDefinition {
  /** Matches the server's WIDGET_ID_PATTERN: ^[a-z][a-z0-9_.]{0,63}$ */
  id: string;
  zone: DashboardZone;
  /** Shown in the Customize dialog. */
  title: string;
  /** One line, shown in the Customize dialog. */
  description: string;
  category: WidgetCategory;
  /** Sources this widget reads; the union over visible widgets is what the page fetches. */
  sources: readonly DashboardSourceKey[];
  /** Panels only. Used for the classic layout, for new placements and for invalid stored sizes. */
  defaultSize?: PanelSize;
  /** Omitted = eligible for everyone who can open `/`. Eligibility is a UX proxy; endpoints still enforce. */
  isEligible?: (ctx: WidgetEligibilityContext) => boolean;
}

/** One placement as stored on the server (`size` is a free string there; coerce on read). */
export interface LayoutEntry {
  id: string;
  size?: string | null;
}

export interface ResolvedPanel {
  definition: DashboardWidgetDefinition;
  size: PanelSize;
}

export interface ResolvedLayout {
  banners: DashboardWidgetDefinition[];
  kpis: DashboardWidgetDefinition[];
  panels: ResolvedPanel[];
  visibleIds: ReadonlySet<string>;
  /** Sorted, de-duplicated source keys the visible widgets need. */
  neededSources: readonly DashboardSourceKey[];
}

/** An extra (opt-in) panel's renderer: zero props, reads everything from DashboardWidgetContext. */
export type DashboardWidgetComponent = ComponentType;

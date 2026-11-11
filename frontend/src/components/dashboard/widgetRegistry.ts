/**
 * The dashboard widget catalog: every section `/` can show, in registry order.
 *
 * CORE_WIDGETS are the sections the dashboard showed before customization (plus two
 * quality KPI tiles that read the already-fetched quality summary); EXTRA_WIDGETS are the
 * opt-in panels (`widgets/extra`). Lookups go through `WIDGETS_BY_ID`, a Map, never a
 * plain-object index: a stored id is user data, and an id like `constructor` must resolve
 * to "unknown", not to `Object.prototype.constructor`.
 *
 * Renderers are not here (this module stays free of React components of its own): core
 * renderers live in `widgets/core`, extra ones in `EXTRA_WIDGET_RENDERERS`.
 */
import type { DashboardLayoutValues } from '../../types/dashboardLayout';
import { EXTRA_WIDGETS } from './widgets/extra';
import type { DashboardWidgetDefinition, LayoutEntry, WidgetCategory } from './widgetTypes';

const canViewQuality = ({ can }: Parameters<NonNullable<DashboardWidgetDefinition['isEligible']>>[0]) =>
  can('quality:view');

export const CORE_WIDGETS: readonly DashboardWidgetDefinition[] = [
  // ── Banners: toggled, always rendered in this order ─────────────────────────
  {
    id: 'attention',
    zone: 'banner',
    title: 'Needs attention',
    description:
      'The most urgent exception (overdue work, open NCRs, calibration, low stock) with a link to review it.',
    category: 'Alerts',
    sources: ['quality', 'equipment', 'stock'],
  },
  {
    id: 'setup_nudge',
    zone: 'banner',
    title: 'Setup progress',
    description: 'Reminds administrators to finish company setup until it is complete.',
    category: 'Alerts',
    sources: [],
    isEligible: ({ isAdmin }) => isAdmin,
  },
  {
    id: 'morning_brief',
    zone: 'banner',
    title: 'Morning brief',
    description: "Today's AI morning brief, when one is waiting in the Action Inbox.",
    category: 'Alerts',
    sources: [],
  },
  {
    id: 'alert_chips',
    zone: 'banner',
    title: 'Alert chips',
    description: 'One compact chip per active exception.',
    category: 'Alerts',
    sources: ['quality', 'equipment', 'stock'],
  },

  // ── Key metrics ─────────────────────────────────────────────────────────────
  {
    id: 'kpi.overdue',
    zone: 'kpi',
    title: 'Overdue',
    description: 'Open work orders past their due date.',
    category: 'Work orders',
    sources: [],
  },
  {
    id: 'kpi.idle_operators',
    zone: 'kpi',
    title: 'Signed In, Idle',
    description: 'People signed in but not clocked into work.',
    category: 'People',
    sources: [],
  },
  {
    id: 'kpi.active_work_orders',
    zone: 'kpi',
    title: 'Active Work Orders',
    description: 'Work orders in progress.',
    category: 'Work orders',
    sources: [],
  },
  {
    id: 'kpi.signed_in',
    zone: 'kpi',
    title: 'Signed In Now',
    description: 'Live ERP sessions.',
    category: 'People',
    sources: [],
  },
  {
    id: 'kpi.checked_in',
    zone: 'kpi',
    title: 'Checked In Now',
    description: 'Active time entries on the floor.',
    category: 'People',
    sources: [],
  },
  {
    id: 'kpi.due_today',
    zone: 'kpi',
    title: 'Due Today',
    description: 'Open work orders due today (Central time).',
    category: 'Work orders',
    sources: [],
  },
  {
    id: 'kpi.calibration_due',
    zone: 'kpi',
    title: 'Calibration Due',
    description: 'Equipment due for calibration within 30 days, overdue included.',
    category: 'Quality',
    sources: ['equipment'],
  },
  {
    id: 'kpi.low_stock',
    zone: 'kpi',
    title: 'Low Stock Items',
    description: 'Parts below their reorder point.',
    category: 'Supply chain',
    sources: ['stock'],
  },
  {
    id: 'kpi.completed_today',
    zone: 'kpi',
    title: 'Operations Completed Today',
    description: 'Operations completed since midnight.',
    category: 'Shop floor',
    sources: [],
  },
  {
    id: 'kpi.open_ncrs',
    zone: 'kpi',
    title: 'Open NCRs',
    description: 'Nonconformance reports still open.',
    category: 'Quality',
    sources: ['quality'],
  },
  {
    id: 'kpi.open_cars',
    zone: 'kpi',
    title: 'Open CARs',
    description: 'Corrective action requests not yet closed.',
    category: 'Quality',
    sources: ['quality'],
    isEligible: canViewQuality,
  },
  {
    id: 'kpi.pending_fai',
    zone: 'kpi',
    title: 'Pending FAIs',
    description: 'First article inspections pending or in progress.',
    category: 'Quality',
    sources: ['quality'],
    isEligible: canViewQuality,
  },

  // ── Panels ──────────────────────────────────────────────────────────────────
  {
    id: 'capacity_overview',
    zone: 'panel',
    title: 'Capacity Overview',
    description: 'Seven-day scheduled load per machine.',
    category: 'Planning',
    sources: ['capacity'],
    defaultSize: 'wide',
  },
  {
    id: 'live_activity',
    zone: 'panel',
    title: 'Live Shop Activity',
    description: 'Who is clocked into which job, grouped by work center.',
    category: 'Shop floor',
    sources: [],
    defaultSize: 'narrow',
  },
  {
    id: 'work_center_status',
    zone: 'panel',
    title: 'Work Center Status',
    description: 'Station status with active, queued and people counts.',
    category: 'Shop floor',
    sources: [],
    defaultSize: 'wide',
  },
  {
    id: 'presence',
    zone: 'panel',
    title: 'Signed In Right Now',
    description: 'Signed-in people, on a job or idle.',
    category: 'People',
    sources: [],
    defaultSize: 'narrow',
  },
  {
    id: 'recent_completions',
    zone: 'panel',
    title: 'Recent Completions',
    description: 'The latest completed operations.',
    category: 'Shop floor',
    sources: [],
    defaultSize: 'full',
  },
];

/** Today's cockpit, for every role: the layout `null` (no saved layout) resolves to. */
export const CLASSIC_LAYOUT: readonly LayoutEntry[] = [
  { id: 'attention' },
  { id: 'setup_nudge' },
  { id: 'morning_brief' },
  { id: 'alert_chips' },
  { id: 'kpi.overdue' },
  { id: 'kpi.idle_operators' },
  { id: 'kpi.active_work_orders' },
  { id: 'kpi.signed_in' },
  { id: 'kpi.checked_in' },
  { id: 'kpi.due_today' },
  { id: 'kpi.calibration_due' },
  { id: 'kpi.low_stock' },
  { id: 'kpi.completed_today' },
  { id: 'kpi.open_ncrs' },
  { id: 'capacity_overview', size: 'wide' },
  { id: 'live_activity', size: 'narrow' },
  { id: 'work_center_status', size: 'wide' },
  { id: 'presence', size: 'narrow' },
  { id: 'recent_completions', size: 'full' },
];

/** Category order in the Customize dialog's "Available" lists. */
export const WIDGET_CATEGORY_ORDER: readonly WidgetCategory[] = [
  'Alerts',
  'Key metrics',
  'Shop floor',
  'Work orders',
  'Quality',
  'Supply chain',
  'Planning',
  'Insights',
  'People',
];

export interface WidgetRegistry {
  /** Every widget, in registry order (banners render in this order). */
  widgets: readonly DashboardWidgetDefinition[];
  byId: ReadonlyMap<string, DashboardWidgetDefinition>;
  /** The stock default. */
  classic: readonly LayoutEntry[];
}

/** Build a registry; a duplicate id keeps its FIRST definition (core wins over extra). */
export function createWidgetRegistry(
  widgets: readonly DashboardWidgetDefinition[],
  classic: readonly LayoutEntry[]
): WidgetRegistry {
  const byId = new Map<string, DashboardWidgetDefinition>();
  const ordered: DashboardWidgetDefinition[] = [];
  for (const widget of widgets) {
    if (byId.has(widget.id)) continue;
    byId.set(widget.id, widget);
    ordered.push(widget);
  }
  return { widgets: ordered, byId, classic };
}

export const DASHBOARD_REGISTRY: WidgetRegistry = createWidgetRegistry(
  [...CORE_WIDGETS, ...EXTRA_WIDGETS],
  CLASSIC_LAYOUT
);

export const ALL_WIDGETS: readonly DashboardWidgetDefinition[] = DASHBOARD_REGISTRY.widgets;
export const WIDGETS_BY_ID: ReadonlyMap<string, DashboardWidgetDefinition> = DASHBOARD_REGISTRY.byId;

/** The classic layout as the server would store it. */
export const CLASSIC_LAYOUT_VALUES: DashboardLayoutValues = {
  format_version: 1,
  widgets: CLASSIC_LAYOUT.map(entry => ({ id: entry.id, size: entry.size ?? null })),
};

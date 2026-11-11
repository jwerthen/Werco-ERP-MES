/**
 * The opt-in dashboard panels. None is on the classic (default) layout; a user adds them
 * from Customize dashboard. Every one reads an endpoint that already exists, through the
 * page's keyed loader (`sources/extraSources.ts`, plus the core `quality` source for Quality
 * Pulse), and renders a `CockpitPanel` with no column-span classes — the page's wrapper
 * carries the span for the placement's size.
 *
 * Eligibility is a UX proxy that mirrors each endpoint's server gate as closely as the
 * client can (`hasRole` where the server uses `require_role`, the route permission
 * otherwise); every endpoint still enforces its own authorization, and a 403 renders
 * in-panel as "Not available for your role".
 */
import type { UserRole } from '../../../../types';
import type { DashboardWidgetComponent, DashboardWidgetDefinition } from '../../widgetTypes';
import { ActionInboxWidget } from './ActionInboxWidget';
import { ActiveDowntimeWidget } from './ActiveDowntimeWidget';
import { DeliveryKpisWidget } from './DeliveryKpisWidget';
import { InspectionQueueWidget } from './InspectionQueueWidget';
import { MaintenanceDueWidget } from './MaintenanceDueWidget';
import { NotificationsWidget } from './NotificationsWidget';
import { QualityPulseWidget } from './QualityPulseWidget';
import { VisitorsOnSiteWidget } from './VisitorsOnSiteWidget';
import { BlockedJobsWidget, LateJobsWidget, ShippingOutlookWidget, TodaysOutputWidget } from './WallboardWidgets';
import { WorkOrdersDueTodayWidget, WorkOrdersOverdueWidget } from './WorkOrderListWidgets';

/** The server's `require_role([ADMIN, MANAGER, SUPERVISOR])` list (superuser / platform_admin pass via hasRole). */
const PLANNER_ROLES: UserRole[] = ['admin', 'manager', 'supervisor'];

/** Definitions for the opt-in panels, in the order the Customize dialog lists them. */
export const EXTRA_WIDGETS: readonly DashboardWidgetDefinition[] = [
  {
    id: 'action_inbox',
    zone: 'panel',
    title: 'Action Inbox',
    description: 'Open issues by severity, with the top high-severity items to act on.',
    category: 'Alerts',
    sources: ['actionInbox'],
    defaultSize: 'half',
  },
  {
    id: 'notifications',
    zone: 'panel',
    title: 'Notifications',
    description: 'Your unread count and the five newest unread notifications.',
    category: 'Alerts',
    sources: ['notifications'],
    defaultSize: 'third',
  },
  {
    id: 'work_orders_overdue',
    zone: 'panel',
    title: 'Overdue Work Orders',
    description: 'Work orders past their due date (Central), oldest due first.',
    category: 'Work orders',
    sources: ['workOrdersOverdue'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'work_orders_due_today',
    zone: 'panel',
    title: 'Work Orders Due Today',
    description: 'Open work orders due today (Central).',
    category: 'Work orders',
    sources: ['workOrdersDueToday'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'late_jobs',
    zone: 'panel',
    title: 'Late Jobs',
    description: 'Work orders past their promise date (must-ship-by, else due date), worst first.',
    category: 'Work orders',
    sources: ['wallboard'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'blocked_jobs',
    zone: 'panel',
    title: 'Blocked Jobs',
    description: 'Open or acknowledged blockers on live work orders, oldest first.',
    category: 'Work orders',
    sources: ['wallboard'],
    defaultSize: 'third',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'todays_output',
    zone: 'panel',
    title: "Today's Output",
    description: 'Operations, pieces and work orders completed today, plus labor and receipts.',
    category: 'Shop floor',
    sources: ['wallboard'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'active_downtime',
    zone: 'panel',
    title: 'Active Downtime',
    description: 'Machines with an open downtime event and how long they have been down.',
    category: 'Shop floor',
    sources: ['downtime'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'maintenance_due',
    zone: 'panel',
    title: 'Maintenance Due',
    description: 'Maintenance work orders overdue, due in the next 7 days, or in progress.',
    category: 'Shop floor',
    sources: ['maintenance'],
    defaultSize: 'third',
    isEligible: ({ can }) => can('work_orders:view'),
  },
  {
    id: 'quality_pulse',
    zone: 'panel',
    title: 'Quality Pulse',
    description: 'Open NCRs, CARs and pending FAIs, plus new NCRs this month by current disposition.',
    category: 'Quality',
    sources: ['quality'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('quality:view'),
  },
  {
    id: 'inspection_queue',
    zone: 'panel',
    title: 'Inspection Queue',
    description: 'Receipts waiting for incoming inspection, with 30-day rejections and acceptance.',
    category: 'Supply chain',
    sources: ['inspectionStats', 'inspectionQueue'],
    defaultSize: 'half',
    isEligible: ({ can }) => can('receiving:view'),
  },
  {
    id: 'shipping_outlook',
    zone: 'panel',
    title: 'Shipping Outlook',
    description: 'Work orders promised to ship today and over the next 7 days.',
    category: 'Supply chain',
    sources: ['wallboard'],
    defaultSize: 'third',
    isEligible: ({ can }) => can('shipping:view'),
  },
  {
    id: 'delivery_kpis',
    zone: 'panel',
    title: 'Delivery & Yield KPIs',
    description: '30-day on-time delivery, OTIF, first-pass yield, scrap rate and backlog hours.',
    category: 'Insights',
    sources: ['deliveryKpis'],
    defaultSize: 'half',
    isEligible: ({ hasRole }) => hasRole(PLANNER_ROLES),
  },
  {
    id: 'visitors_on_site',
    zone: 'panel',
    title: 'Visitors On Site',
    description: 'Visitors currently signed in at the lobby and who is hosting them.',
    category: 'People',
    sources: ['visitors'],
    defaultSize: 'third',
    isEligible: ({ hasRole }) => hasRole(PLANNER_ROLES),
  },
];

/** id → renderer for every entry in EXTRA_WIDGETS. A Map, never a plain object. */
export const EXTRA_WIDGET_RENDERERS: ReadonlyMap<string, DashboardWidgetComponent> = new Map<
  string,
  DashboardWidgetComponent
>([
  ['action_inbox', ActionInboxWidget],
  ['notifications', NotificationsWidget],
  ['work_orders_overdue', WorkOrdersOverdueWidget],
  ['work_orders_due_today', WorkOrdersDueTodayWidget],
  ['late_jobs', LateJobsWidget],
  ['blocked_jobs', BlockedJobsWidget],
  ['todays_output', TodaysOutputWidget],
  ['active_downtime', ActiveDowntimeWidget],
  ['maintenance_due', MaintenanceDueWidget],
  ['quality_pulse', QualityPulseWidget],
  ['inspection_queue', InspectionQueueWidget],
  ['shipping_outlook', ShippingOutlookWidget],
  ['delivery_kpis', DeliveryKpisWidget],
  ['visitors_on_site', VisitorsOnSiteWidget],
]);

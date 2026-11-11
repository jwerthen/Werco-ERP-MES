/**
 * Renderers for the core widgets (the sections the dashboard showed before
 * customization), keyed by widget id. A Map, never a plain object: ids are user data.
 */
import type { DashboardWidgetComponent } from '../../widgetTypes';
import { AlertChipsBanner, AttentionBanner, MorningBriefWidget, SetupNudgeBanner } from './banners';
import { CapacityOverviewPanel } from './CapacityOverviewPanel';
import {
  ActiveWorkOrdersTile,
  CalibrationDueTile,
  CheckedInTile,
  CompletedTodayTile,
  DueTodayTile,
  IdleOperatorsTile,
  LowStockTile,
  OpenCarsTile,
  OpenNcrsTile,
  OverdueTile,
  PendingFaiTile,
  SignedInTile,
} from './kpis';
import { LiveActivityPanel } from './LiveActivityPanel';
import { PresencePanel } from './PresencePanel';
import { RecentCompletionsPanel } from './RecentCompletionsPanel';
import { WorkCenterStatusPanel } from './WorkCenterStatusPanel';

export const CORE_WIDGET_RENDERERS: ReadonlyMap<string, DashboardWidgetComponent> = new Map<
  string,
  DashboardWidgetComponent
>([
  ['attention', AttentionBanner],
  ['setup_nudge', SetupNudgeBanner],
  ['morning_brief', MorningBriefWidget],
  ['alert_chips', AlertChipsBanner],
  ['kpi.overdue', OverdueTile],
  ['kpi.idle_operators', IdleOperatorsTile],
  ['kpi.active_work_orders', ActiveWorkOrdersTile],
  ['kpi.signed_in', SignedInTile],
  ['kpi.checked_in', CheckedInTile],
  ['kpi.due_today', DueTodayTile],
  ['kpi.calibration_due', CalibrationDueTile],
  ['kpi.low_stock', LowStockTile],
  ['kpi.completed_today', CompletedTodayTile],
  ['kpi.open_ncrs', OpenNcrsTile],
  ['kpi.open_cars', OpenCarsTile],
  ['kpi.pending_fai', PendingFaiTile],
  ['capacity_overview', CapacityOverviewPanel],
  ['live_activity', LiveActivityPanel],
  ['work_center_status', WorkCenterStatusPanel],
  ['presence', PresencePanel],
  ['recent_completions', RecentCompletionsPanel],
]);

export { CoreDashboardProvider, useCoreDashboard } from './CoreDashboardContext';
export type { CoreDashboardEnv } from './CoreDashboardContext';

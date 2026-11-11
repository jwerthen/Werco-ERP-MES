/**
 * Response shapes read by the dashboard's opt-in widgets
 * (`components/dashboard/widgets/extra`), for endpoints whose `api.ts` method is
 * untyped. Each mirrors the backend contract named beside it; fields a widget does
 * not render are still listed when the endpoint always sends them, so a fixture
 * annotated with one of these types matches the real payload.
 *
 * Endpoints that already have frontend types reuse them: the wallboard
 * (`types/wallboard.ts`), work-order browse (`types/workOrderBrowse.ts`), the
 * operations inbox (`types/operationsInbox.ts`), notifications
 * (`types/notification.ts`) and visitor logs (`types/visitor.ts`). The quality summary
 * read by Quality Pulse is the core `quality` source (`QualitySummaryData` in
 * `components/dashboard/sources/coreSources.ts`).
 */
import type { OperationalInboxResponse } from './operationsInbox';
import type { NotificationListResponse } from './notification';
import type { VisitorLogListResponse } from './visitor';
import type { WallboardResponse } from './wallboard';
import type { WorkOrderBrowseResponse } from './workOrderBrowse';

/**
 * One row of `GET /downtime/active` (`DowntimeEventResponse`, backend/app/api/endpoints/downtime.py).
 * A plain `BaseModel`, not `UTCModel`: datetimes may arrive WITHOUT a `Z` — parse them with
 * `utils/centralTime.toDate`, which treats a zone-less string as UTC.
 */
export interface ActiveDowntimeEvent {
  id: number;
  work_center_id: number;
  work_order_id: number | null;
  start_time: string;
  end_time: string | null;
  duration_minutes: number | null;
  /** DowntimeCategory value, e.g. `mechanical`, `tooling`, `planned_maintenance`. */
  category: string;
  /** `planned` | `unplanned`. */
  planned_type: string;
  reason_code: string | null;
  description: string | null;
  resolution: string | null;
  reported_by: number;
  resolved_by: number | null;
  created_at: string | null;
  updated_at: string | null;
  work_center: { id: number; code: string; name: string } | null;
  reporter: { id: number; username: string | null; full_name: string | null } | null;
  resolver: { id: number; username: string | null; full_name: string | null } | null;
  work_order: { id: number; wo_number: string | null } | null;
}

/**
 * `GET /maintenance/dashboard` (backend/app/api/endpoints/maintenance.py `get_dashboard`).
 * The dashboard widget deliberately renders none of the cost fields.
 */
export interface MaintenanceDashboardResponse {
  overdue_count: number;
  due_this_week: number;
  completed_this_month: number;
  completion_rate: number;
  total_cost_month: number;
  in_progress: number;
  work_center_metrics: {
    work_center_id: number;
    work_center_name: string;
    work_center_code: string | null;
    wo_count: number;
    mtbf_hours: number;
    mttr_hours: number;
    total_downtime_minutes: number;
    total_cost: number;
  }[];
}

/** `GET /receiving/stats?days=N` (backend/app/api/endpoints/receiving.py `get_receiving_stats`). */
export interface ReceivingStatsResponse {
  pending_inspection: number;
  receipts_in_period: number;
  inspected_in_period: number;
  rejections_in_period: number;
  total_qty_received: number | string;
  total_qty_accepted: number | string;
  total_qty_rejected: number | string;
  /** Percent of received quantity accepted in the period; 100 when nothing was received. */
  acceptance_rate: number;
}

/**
 * One row of `GET /receiving/inspection-queue` (`InspectionQueueItem`,
 * backend/app/schemas/purchasing.py). Oldest first. `quantity_received` is a Decimal on
 * the wire (it may serialize as a string).
 */
export interface InspectionQueueRow {
  receipt_id: number;
  receipt_number: string;
  po_number: string | null;
  po_id: number | null;
  vendor_name: string | null;
  part_id: number | null;
  part_number: string | null;
  part_name: string | null;
  quantity_received: number | string;
  lot_number: string;
  cert_number: string | null;
  coc_attached: boolean;
  received_at: string | null;
  received_by_name: string | null;
  location_code: string | null;
  days_pending: number;
}

/** `KPIValue` (backend/app/schemas/analytics.py). `value` is null when uncomputable ("n/a"). */
export interface KpiValue {
  value: number | null;
  target: number | null;
  prior_value: number | null;
  change_pct: number | null;
  trend: 'up' | 'down' | 'flat';
  sparkline: number[];
}

/** `GET /analytics/kpis` (`KPIDashboard`, backend/app/schemas/analytics.py). ADMIN/MANAGER/SUPERVISOR only. */
export interface KpiDashboardResponse {
  oee: KpiValue;
  on_time_delivery: KpiValue;
  first_pass_yield: KpiValue;
  scrap_rate: KpiValue;
  open_ncrs: KpiValue;
  quote_win_rate: KpiValue;
  backlog_hours: KpiValue;
  inventory_turnover: KpiValue;
  /** Optional on the schema; the live endpoint always populates it. */
  on_time_delivery_ship?: KpiValue | null;
  /** Optional on the schema; the live endpoint always populates it. */
  otif?: KpiValue | null;
  period_start: string;
  period_end: string;
  generated_at: string;
}

/** Data type behind each opt-in widget source key (`sources/extraSources.ts`). */
export interface ExtraSourceDataMap {
  wallboard: WallboardResponse;
  workOrdersOverdue: WorkOrderBrowseResponse;
  workOrdersDueToday: WorkOrderBrowseResponse;
  actionInbox: OperationalInboxResponse;
  downtime: ActiveDowntimeEvent[];
  maintenance: MaintenanceDashboardResponse;
  inspectionStats: ReceivingStatsResponse;
  inspectionQueue: InspectionQueueRow[];
  deliveryKpis: KpiDashboardResponse;
  visitors: VisitorLogListResponse;
  notifications: NotificationListResponse;
}

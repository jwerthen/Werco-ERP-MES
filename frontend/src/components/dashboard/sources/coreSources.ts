/**
 * Source specs for the sections the dashboard showed before customization. They keep
 * today's cadence: no refresh floor, so every 30 s poll, WebSocket push and manual
 * refresh refetches them (the keyed loader still skips non-forced ticks in a hidden tab).
 *
 * Each spec stores the RAW response; widgets derive what they show (counts etc.), so
 * two widgets reading one source never disagree.
 */
import { addDays } from 'date-fns';
import api from '../../../services/api';
import { getCentralDateStamp, getCentralTodayDate } from '../../../utils/centralTime';
import type { CoreSourceKey, SourceSpec } from '../widgetTypes';

/** `GET /quality/summary`. `ncr_dispositions` counts NCRs CREATED this (server UTC) month by CURRENT disposition. */
export interface QualitySummaryData {
  open_ncrs: number;
  open_cars: number;
  pending_fais: number;
  ncr_dispositions: Record<string, number>;
}

/** One row of `GET /calibration/equipment/due-soon?days=30` (overdue included); the dashboard counts rows. */
export interface EquipmentDueSoonRow {
  equipment_id?: number;
  name?: string;
  next_calibration_date?: string | null;
  days_until_due?: number | null;
  status?: string;
}

/** One row of `GET /inventory/low-stock`; the dashboard counts rows. */
export interface LowStockRow {
  id?: number;
  part_number?: string;
  is_critical?: boolean;
}

export interface CapacityHeatmapDay {
  date: string;
  scheduled_hours: number;
  capacity_hours: number;
  utilization_pct: number;
  job_count: number;
  overloaded: boolean;
}

export interface CapacityHeatmapRow {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  capacity_hours_per_day: number;
  days: CapacityHeatmapDay[];
}

/** `GET /scheduling/capacity-heatmap` for Central today through today + 6. */
export interface CapacityHeatmapResponse {
  start_date: string;
  end_date: string;
  overload_cells: number;
  overloaded_work_centers: number[];
  work_centers: CapacityHeatmapRow[];
}

const qualitySpec: SourceSpec<QualitySummaryData> = {
  label: 'Quality',
  fetch: () => api.getQualitySummary(),
  minIntervalMs: 0,
};

const equipmentSpec: SourceSpec<EquipmentDueSoonRow[]> = {
  label: 'Calibration',
  fetch: () => api.getEquipmentDueSoon(30),
  minIntervalMs: 0,
};

const stockSpec: SourceSpec<LowStockRow[]> = {
  label: 'Inventory',
  fetch: () => api.getLowStockAlerts(),
  minIntervalMs: 0,
};

const capacitySpec: SourceSpec<CapacityHeatmapResponse> = {
  label: 'Capacity',
  fetch: () => {
    // The window is recomputed per fetch so a dashboard left open overnight rolls forward.
    const today = getCentralTodayDate();
    return api.getCapacityHeatmap(getCentralDateStamp(today), getCentralDateStamp(addDays(today, 6)));
  },
  minIntervalMs: 0,
};

export const CORE_SOURCE_SPECS: Record<CoreSourceKey, SourceSpec> = {
  quality: qualitySpec,
  equipment: equipmentSpec,
  stock: stockSpec,
  capacity: capacitySpec,
};

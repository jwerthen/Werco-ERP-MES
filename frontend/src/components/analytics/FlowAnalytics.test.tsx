/**
 * FlowAnalytics — happy-path render of the Analytics "Flow" view (Lean Phase 1).
 *
 * With all five endpoints resolving, the view must show: the flow KPI strip,
 * the WIP-aging rows with the aging badge, the queue-by-work-center panel and
 * its "measured from ready events" provenance caption, the FPY panels with the
 * overall FPY/RTY subtitle, the scrap-Pareto panel with its quantity/cost
 * headline + cost-coverage hint, and the adoption tiles with the MTBF/MTTR
 * table. Percent/day metrics the backend returns as null must render "—".
 *
 * jsdom has no ResizeObserver; recharts' ResponsiveContainer needs one.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import FlowAnalytics from './FlowAnalytics';

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getFlowMetrics: jest.fn(),
    getWipAging: jest.fn(),
    getAdoptionMetrics: jest.fn(),
    getFpyAnalytics: jest.fn(),
    getScrapPareto: jest.fn(),
  },
}));

jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const flow = {
  period_start: '2026-06-09',
  period_end: '2026-07-09',
  summary: {
    work_orders_completed: 12,
    avg_lead_time_days: 4.2,
    median_lead_time_days: 3.5,
    avg_release_to_last_ship_days: 5.1,
    avg_queue_hours: 6.4,
    avg_wip: 9.3,
    daily_completion_rate: 0.4,
    littles_law_throughput_days: 23.2,
    avg_pce_pct: 11.8,
    excluded_backfill_import_hours: 3.5,
  },
  work_orders: [],
  queue_by_work_center: [
    {
      work_center_id: 1,
      work_center_code: 'LASER1',
      work_center_name: 'Laser cell 1',
      avg_queue_hours: 6.4,
      max_queue_hours: 20.1,
      samples: 3,
      from_ready_events: 2,
    },
  ],
  generated_at: '2026-07-09T12:00:00Z',
};

const wip = {
  items: [
    {
      work_order_id: 42,
      work_order_number: 'WO-1001',
      part_number: 'PN-7',
      customer_name: 'Acme',
      status: 'in_progress',
      priority: 3,
      quantity_ordered: 50,
      quantity_complete: 20,
      released_at: '2026-06-20T12:00:00Z',
      days_since_release: 19.0,
      current_operation_id: 7,
      current_operation_number: '20',
      current_operation_name: 'Deburr',
      current_work_center_name: 'Deburr bench',
      days_in_current_operation: 2.5,
      due_date: '2026-07-05',
      days_to_due: -4,
    },
  ],
  total_open: 1,
  generated_at: '2026-07-09T12:00:00Z',
};

const fpy = {
  period_start: '2026-06-09',
  period_end: '2026-07-09',
  overall_fpy_pct: 96.0,
  overall_rty_pct: 91.5,
  by_part: [
    {
      key: 'PN-7',
      name: 'Bracket',
      operations: 4,
      units_attempted: 100,
      first_pass_units: 96,
      fpy_pct: 96.0,
      rty_pct: 91.5,
      work_orders: 2,
    },
  ],
  by_work_center: [
    {
      key: 'LASER1',
      name: 'Laser cell 1',
      operations: 4,
      units_attempted: 100,
      first_pass_units: 98,
      fpy_pct: 98.0,
      rty_pct: null,
      work_orders: 2,
    },
  ],
  generated_at: '2026-07-09T12:00:00Z',
};

const pareto = {
  period_start: '2026-06-09',
  period_end: '2026-07-09',
  total_quantity: 14,
  total_cost: 322,
  buckets: [
    {
      scrap_reason_code_id: 7,
      code: 'OT',
      name: 'Out of tolerance',
      category: 'operator',
      quantity: 9,
      cost: 322,
      percentage: 64.3,
      cumulative_pct: 64.3,
    },
    {
      scrap_reason_code_id: null,
      code: 'unspecified',
      name: null,
      category: null,
      quantity: 5,
      cost: 0,
      percentage: 35.7,
      cumulative_pct: 100.0,
    },
  ],
  excluded_backfill_import_quantity: 2,
  generated_at: '2026-07-09T12:00:00Z',
};

const adoption = {
  period_start: '2026-06-09',
  period_end: '2026-07-09',
  digital_completion_pct: 82.0,
  clock_in_coverage_pct: 74.5,
  backfill_rate_pct: 12.0,
  live_completions: 41,
  backfill_completions: 6,
  unknown_completions: 3,
  weekly: [
    {
      week_start: '2026-06-29',
      operation_completions: 20,
      live_completions: 17,
      backfill_completions: 2,
      unknown_completions: 1,
      digital_completion_pct: 85.0,
      clock_in_coverage_pct: 75.0,
      time_entries: 40,
      backfill_entries: 4,
      backfill_rate_pct: 10.0,
    },
  ],
  hidden_factory: {
    rework_hours: 14.5,
    total_labor_hours: 200,
    rework_hours_pct: 7.3,
    rework_quantity: 12,
    total_quantity: 300,
    rework_quantity_pct: 4.0,
    maintenance: { planned_count: 6, reactive_count: 2, planned_pct: 75.0 },
    reliability_by_work_center: [
      {
        work_center_id: 1,
        work_center_code: 'LASER1',
        work_center_name: 'Laser cell 1',
        unplanned_downtime_events: 2,
        unplanned_downtime_hours: 3.0,
        staffed_run_hours: 120,
        mtbf_hours: 60.0,
        mttr_hours: 1.5,
      },
    ],
    excluded_backfill_import_hours: 1.2,
  },
  generated_at: '2026-07-09T12:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getFlowMetrics.mockResolvedValue(flow as any);
  mockedApi.getWipAging.mockResolvedValue(wip as any);
  mockedApi.getFpyAnalytics.mockResolvedValue(fpy as any);
  mockedApi.getScrapPareto.mockResolvedValue(pareto as any);
  mockedApi.getAdoptionMetrics.mockResolvedValue(adoption as any);
});

function renderFlow() {
  return render(
    <MemoryRouter>
      <FlowAnalytics period="30d" />
    </MemoryRouter>
  );
}

test('renders the flow KPI strip, WIP aging, queue provenance, FPY, Pareto, and adoption', async () => {
  renderFlow();

  // 1 — Flow KPI strip.
  expect(await screen.findByText('Avg Lead Time')).toBeInTheDocument();
  expect(screen.getByText('4.2d')).toBeInTheDocument();
  expect(screen.getByText("Little's Law")).toBeInTheDocument();
  expect(screen.getByText('23.2d')).toBeInTheDocument();
  expect(screen.getByText('11.8%')).toBeInTheDocument();
  // Provenance caption for excluded backfill labor.
  expect(screen.getByText(/3\.5h of backfill\/import labor excluded/i)).toBeInTheDocument();

  // 2 — WIP aging row (both desktop table and mobile card render in jsdom)
  //     with the aging badge and the queue provenance caption.
  expect(screen.getAllByText('WO-1001').length).toBeGreaterThan(0);
  expect(screen.getAllByText('19.0d').length).toBeGreaterThan(0);
  expect(screen.getByText(/measured from ready events: 2\/3 samples/i)).toBeInTheDocument();

  // 3 — FPY panels: overall subtitle + per-part row.
  expect(screen.getByText(/Overall FPY 96\.0% · RTY 91\.5%/)).toBeInTheDocument();
  expect(screen.getAllByText(/PN-7/).length).toBeGreaterThan(0);

  // 4 — Scrap Pareto headline (quantity · cost) + coverage/provenance hints.
  expect(screen.getByText(/14 pcs · \$322/)).toBeInTheDocument();
  expect(screen.getByText(/1 bucket carry quantity but no cost/i)).toBeInTheDocument();
  expect(screen.getByText(/2 pcs of backfill\/import scrap excluded/i)).toBeInTheDocument();

  // 5 — Adoption tiles + reliability table.
  expect(screen.getByText('Digital Completion')).toBeInTheDocument();
  expect(screen.getByText('82.0%')).toBeInTheDocument();
  expect(screen.getByText('Clock-in Coverage')).toBeInTheDocument();
  expect(screen.getByText(/41 live \/ 6 backfill \/ 3 unknown/)).toBeInTheDocument();
  expect(screen.getAllByText('60.0h').length).toBeGreaterThan(0); // MTBF
});

test('renders "—" for metrics the backend cannot compute (null)', async () => {
  mockedApi.getFlowMetrics.mockResolvedValue({
    ...flow,
    summary: {
      ...flow.summary,
      avg_lead_time_days: null,
      littles_law_throughput_days: null,
      avg_pce_pct: null,
      excluded_backfill_import_hours: 0,
    },
  } as any);

  renderFlow();

  expect(await screen.findByText('Avg Lead Time')).toBeInTheDocument();
  // The null-valued tiles all fall back to the em dash.
  expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  expect(screen.queryByText(/backfill\/import labor excluded/i)).not.toBeInTheDocument();
});

/**
 * SPC page — API-contract regression suite.
 *
 * WHY THIS FILE WAS REWRITTEN
 * ---------------------------
 * The previous version of this suite mocked every SPC client method as
 * `mockResolvedValue({ data: ... })`. That is NOT what `services/api.ts` returns —
 * every SPC getter/creator/adder on the client already returns `response.data`
 * (api.ts §"SPC (Statistical Process Control)"). By handing the page a wrapper the
 * real client never produces, the suite reproduced the page's *own* double-unwrap bug
 * inside the fixtures, so `charRes.data?.results || charRes.data || []` resolved to a
 * populated list in tests and to `[]` in production. A completely non-functional page
 * stayed green for months, and it hid live kiosk-captured quality data.
 *
 * Every mock below therefore resolves the UNWRAPPED payload in the exact shape the
 * backend emits (verified against `backend/app/api/endpoints/spc.py`), and the fixtures
 * are annotated with the contract interfaces from `types/spc.ts`.
 *
 * ⚠️ THE FIXTURE ANNOTATIONS ARE NOT A COMPILE-TIME GATE. `tsconfig.json` excludes
 * every `.test.tsx` from `npm run type-check`, and jest.config.js inherits
 * `isolatedModules: true`, which puts ts-jest in transpile-only mode — a wrong-shaped
 * fixture is NOT a compile error here (measured: an object literal with a bogus
 * property passes). Flipping `isolatedModules: false` repo-wide fails 30 unrelated
 * suites and takes the run from 30s to 430s, so it is a separate cleanup. What actually
 * protects the contract is the ASSERTIONS below — each was verified to fail individually
 * against a reintroduction of the defect it covers. Keep them specific.
 *
 * WHY recharts IS MOCKED HERE
 * ---------------------------
 * The chart is where three of the renamed fields actually land — `chart_points[].mean`
 * on the series, `subgroup_number` on the X axis, and `control_limits.center_line` on
 * the centre `ReferenceLine`. Real recharts renders those into computed SVG geometry
 * that a test cannot read back, so a `cl`-vs-`center_line` regression would be
 * invisible. The stub below re-emits each recharts prop as a data attribute, which
 * turns "the centre line is drawn from the wrong field" into a hard assertion. The
 * rest of the page (panels, tables, modals, empty states) still renders for real.
 * The ResizeObserver shim is kept so this file still works if the stub is ever removed.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import SPC from './SPC';
import type { Part } from '../types';
import type {
  SPCCapability,
  SPCChartData,
  SPCChartPoint,
  SPCCharacteristic,
  SPCControlLimit,
  SPCDashboard,
  SPCMeasurement,
  SPCOutOfControlAlert,
  SPCViolationsResponse,
} from '../types/spc';

// recharts ResponsiveContainer needs ResizeObserver; jsdom has none.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

interface RechartsStubProps {
  children?: React.ReactNode;
  data?: unknown;
  dataKey?: string;
  name?: string;
  y?: number;
  label?: string;
}

jest.mock('recharts', () => {
  const R: typeof import('react') = require('react');
  const el = R.createElement;
  return {
    __esModule: true,
    ResponsiveContainer: ({ children }: RechartsStubProps) => el('div', null, children),
    LineChart: ({ data, children }: RechartsStubProps) =>
      el(
        'div',
        { 'data-testid': 'line-chart', 'data-chart-data': JSON.stringify(data ?? []) },
        children
      ),
    Line: ({ dataKey, name }: RechartsStubProps) =>
      el('div', { 'data-testid': 'chart-series', 'data-key': dataKey, 'data-name': name }),
    XAxis: ({ dataKey }: RechartsStubProps) =>
      el('div', { 'data-testid': 'x-axis', 'data-key': dataKey }),
    YAxis: () => el('div', { 'data-testid': 'y-axis' }),
    CartesianGrid: () => null,
    Tooltip: () => null,
    Legend: () => null,
    ReferenceLine: ({ y, label }: RechartsStubProps) =>
      el('div', {
        'data-testid': 'reference-line',
        'data-label': String(label),
        'data-y': String(y),
      }),
  };
});

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    // mount
    getSPCDashboard: jest.fn(),
    getSPCCharacteristics: jest.fn(),
    getSPCOutOfControl: jest.fn(),
    // characteristic detail
    getSPCControlLimits: jest.fn(),
    getSPCCapability: jest.fn(),
    getSPCMeasurements: jest.fn(),
    getSPCChartData: jest.fn(),
    getSPCViolations: jest.fn(),
    // writes
    addSPCMeasurements: jest.fn(),
    createSPCCharacteristic: jest.fn(),
    calculateSPCControlLimits: jest.fn(),
    runSPCCapabilityStudy: jest.fn(),
    // part labels + the create-characteristic part picker
    getParts: jest.fn(),
  },
}));

// SPC calls `useAuth()` to gate the Recalculate control (the endpoint is
// ADMIN/MANAGER/QUALITY-only because it rewrites out-of-control flags on historical
// measurements). `useAuth` THROWS outside an AuthProvider rather than returning a null
// user, so a page test either provides the context or mocks the hook — mocked here, the
// same way PartDetail's suites do. `mockRole` is reassigned per-test to drive the gate.
let mockRole = 'admin';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: mockRole, is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// ---------------------------------------------------------------------------
// Fixtures — the REAL response shapes. `specification_*` not `nominal/usl/lsl`,
// `center_line` not `cl`, `measurement_value` not `value`, an int `measured_by`,
// an OBJECT from chart-data and violations, a bare array from characteristics.
// ---------------------------------------------------------------------------

const characteristic: SPCCharacteristic = {
  id: 7,
  name: 'Bore Diameter',
  part_id: 10,
  characteristic_type: 'dimensional',
  unit_of_measure: 'in',
  specification_nominal: 1.5,
  specification_usl: 1.55,
  specification_lsl: 1.45,
  chart_type: 'xbar_r',
  subgroup_size: 5,
  work_center_id: null,
  operation_number: null,
  is_active: true,
  is_critical: false,
  notes: null,
  // Deliberately no trailing `Z` — the SPC schema routes inherit bare BaseModel.
  created_at: '2026-06-01T12:00:00',
  updated_at: null,
};

const dashboard: SPCDashboard = {
  total_characteristics: 3,
  out_of_control_count: 1,
  average_cpk: 1.42,
  characteristics_below_cpk_threshold: 2,
  attention_needed: [],
};

const controlLimits: SPCControlLimit = {
  id: 4,
  characteristic_id: 7,
  calculation_date: '2026-06-28T15:00:00',
  ucl: 1.552,
  lcl: 1.448,
  center_line: 1.5,
  ucl_range: 0.09,
  lcl_range: 0,
  center_line_range: 0.04,
  sample_count: 24,
  is_current: true,
  notes: null,
};

const capability: SPCCapability = {
  id: 2,
  characteristic_id: 7,
  study_date: '2026-06-28T15:05:00',
  sample_count: 120,
  mean: 1.5,
  std_dev: 0.01,
  cp: 1.6,
  cpk: 1.42,
  pp: 1.55,
  ppk: 1.38,
  within_spec_pct: 99.7,
  is_capable: true,
  notes: null,
};

const measurements: SPCMeasurement[] = [
  {
    id: 91,
    characteristic_id: 7,
    subgroup_number: 11,
    measurement_value: 1.498,
    sample_number: 1,
    measured_at: '2026-06-28T14:55:00',
    measured_by: 4,
    work_order_id: null,
    lot_number: null,
    serial_number: null,
    is_out_of_control: false,
    violation_rules: null,
    notes: 'in tolerance',
  },
  {
    id: 92,
    characteristic_id: 7,
    subgroup_number: 12,
    measurement_value: 1.562,
    sample_number: 2,
    measured_at: '2026-06-28T15:00:00',
    measured_by: 4,
    work_order_id: null,
    lot_number: null,
    serial_number: null,
    is_out_of_control: true,
    violation_rules: 'Rule1',
    notes: null,
  },
];

const chartData: SPCChartData = {
  characteristic: {
    id: 7,
    name: 'Bore Diameter',
    chart_type: 'xbar_r',
    subgroup_size: 5,
    specification_nominal: 1.5,
    specification_usl: 1.55,
    specification_lsl: 1.45,
    unit_of_measure: 'in',
  },
  chart_points: [
    {
      subgroup_number: 11,
      mean: 1.499,
      range: 0.02,
      sample_count: 5,
      is_out_of_control: false,
      violations: [],
      measured_at: '2026-06-28T14:55:00Z',
    },
    {
      subgroup_number: 12,
      mean: 1.556,
      range: 0.06,
      sample_count: 5,
      is_out_of_control: true,
      violations: ['Rule1'],
      measured_at: '2026-06-28T15:00:00Z',
    },
  ],
  control_limits: {
    ucl: 1.552,
    lcl: 1.448,
    center_line: 1.5,
    ucl_range: 0.09,
    lcl_range: 0,
    center_line_range: 0.04,
  },
};

const violations: SPCViolationsResponse = {
  characteristic_id: 7,
  characteristic_name: 'Bore Diameter',
  control_limits: { ucl: 1.552, lcl: 1.448, center_line: 1.5 },
  violations: [{ subgroup_number: 12, subgroup_mean: 1.556, rules_violated: ['Rule1', 'Rule2'] }],
  total_subgroups: 2,
  total_violations: 1,
};

const oocAlert: SPCOutOfControlAlert = {
  characteristic_id: 7,
  characteristic_name: 'Bore Diameter',
  part_id: 10,
  is_critical: true,
  ooc_count: 3,
  last_ooc: '2026-06-28T15:00:00Z',
};

const part: Part = {
  id: 10,
  version: 1,
  part_number: 'P-1000',
  revision: 'A',
  name: 'Housing',
  part_type: 'manufactured',
  unit_of_measure: 'ea',
  standard_cost: 12,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderSPC() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <SPC />
      </ToastProvider>
    </MemoryRouter>
  );
}

/** A CockpitPanel, scoped by its <h2> title. */
const panel = (title: string): HTMLElement => {
  const heading = screen.getByRole('heading', { name: title });
  const card = heading.closest('.card');
  if (!card) throw new Error(`No panel card found for "${title}"`);
  return card as HTMLElement;
};

/** A MiniStat KPI tile, scoped by its label so the value can't be matched page-wide. */
const statTile = (label: string): HTMLElement => {
  const tile = screen.getByText(label).parentElement?.parentElement;
  if (!tile) throw new Error(`No stat tile found for "${label}"`);
  return tile;
};

/** A capability tile (Cp / Cpk / Pp / Ppk), scoped by its heading. */
const capabilityTile = (label: string): HTMLElement => {
  const tile = within(panel('Process Capability')).getByText(label).parentElement;
  if (!tile) throw new Error(`No capability tile found for "${label}"`);
  return tile;
};

/** label -> y for every stubbed recharts ReferenceLine inside `root`. */
const referenceLines = (root: HTMLElement): Record<string, string> => {
  const out: Record<string, string> = {};
  root.querySelectorAll('[data-testid="reference-line"]').forEach((node) => {
    out[node.getAttribute('data-label') ?? ''] = node.getAttribute('data-y') ?? '';
  });
  return out;
};

async function selectCharacteristic(name = /Bore Diameter/) {
  renderSPC();
  fireEvent.click(await screen.findByRole('button', { name }));
  await waitFor(() => {
    expect(mockedApi.getSPCControlLimits).toHaveBeenCalled();
  });
  await screen.findByRole('heading', { name: /^Control Chart: / });
}

describe('SPC page — API contract', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRole = 'admin';

    mockedApi.getSPCDashboard.mockResolvedValue(dashboard);
    mockedApi.getSPCCharacteristics.mockResolvedValue([characteristic]);
    mockedApi.getSPCOutOfControl.mockResolvedValue([]);

    mockedApi.getSPCControlLimits.mockResolvedValue(controlLimits);
    mockedApi.getSPCCapability.mockResolvedValue(capability);
    mockedApi.getSPCMeasurements.mockResolvedValue(measurements);
    mockedApi.getSPCChartData.mockResolvedValue(chartData);
    mockedApi.getSPCViolations.mockResolvedValue(violations);

    mockedApi.addSPCMeasurements.mockResolvedValue([]);
    mockedApi.createSPCCharacteristic.mockResolvedValue({ ...characteristic, id: 7 });
    mockedApi.calculateSPCControlLimits.mockResolvedValue(controlLimits);
    mockedApi.runSPCCapabilityStudy.mockResolvedValue(capability);
    mockedApi.getParts.mockResolvedValue([part]);
  });

  // =========================================================================
  // 1. READS — the regression that made the whole page dead
  // =========================================================================

  it('renders the characteristic list from the bare array the client returns', async () => {
    renderSPC();

    // THE regression guard. GET /spc/characteristics is a bare array with no envelope,
    // and the client already unwrapped it. Re-unwrapping (`res.data?.results || res.data
    // || []`) evaluates to [] for an array, which is why production showed an empty list
    // and every downstream flow was unreachable.
    expect(await screen.findByRole('button', { name: /Bore Diameter/ })).toBeInTheDocument();

    const list = panel('Characteristics');
    // Sub-label proves `chart_type`, `subgroup_size` and `part_id` are read off the row.
    // The part number matters: the route orders by NAME alone, so two "Bore Diameter"
    // characteristics on different parts sort adjacent and are otherwise identical.
    expect(await within(list).findByText('P-1000 · X-bar & R · n=5')).toBeInTheDocument();
    expect(within(list).queryByText('No characteristics')).not.toBeInTheDocument();
    expect(screen.getByText('1 characteristic total')).toBeInTheDocument();

    // No explicit `limit`: that is what makes the client page the route to completion
    // instead of taking its silent default of 100 and reporting it as the total.
    expect(mockedApi.getSPCCharacteristics).toHaveBeenCalledWith({ is_active: true });
  });

  it('renders the dashboard tiles from the real /spc/dashboard field names', async () => {
    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });

    // total_characteristics — NOT `characteristics_monitored`.
    expect(within(statTile('Characteristics Monitored')).getByText('3')).toBeInTheDocument();
    expect(within(statTile('Out-of-Control Alerts')).getByText('1')).toBeInTheDocument();
    // average_cpk — NOT `avg_cpk`.
    expect(within(statTile('Average Cpk')).getByText('1.42')).toBeInTheDocument();
    // characteristics_below_cpk_threshold — the endpoint has no `measurements_today`,
    // so a tile bound to that name could only ever render a placeholder.
    expect(within(statTile('Below Cpk 1.33')).getByText('2')).toBeInTheDocument();
    expect(screen.queryByText('Measurements Today')).not.toBeInTheDocument();

    expect(screen.getByRole('heading', { name: 'Statistical Process Control' })).toBeInTheDocument();
  });

  it('renders spec limits, unit and control-limit provenance from the real field names', async () => {
    await selectCharacteristic();

    const chart = panel('Control Chart: Bore Diameter');
    // specification_nominal / _usl / _lsl — NOT nominal / usl / lsl.
    expect(within(chart).getByText('Nominal: 1.5')).toBeInTheDocument();
    expect(within(chart).getByText('USL: 1.55')).toBeInTheDocument();
    expect(within(chart).getByText('LSL: 1.45')).toBeInTheDocument();
    expect(within(chart).getByText('Unit: in')).toBeInTheDocument();

    // Part + sample_count + calculation_date off the typed SPCControlLimit row.
    expect(await within(chart).findByText(/^P-1000 · limits from 24 subgroups, /)).toBeInTheDocument();

    // Capability panel reads the nullable indices and its own sample_count.
    const cap = panel('Process Capability');
    expect(within(cap).getByText('1.600')).toBeInTheDocument(); // cp
    expect(within(cap).getByText('1.420')).toBeInTheDocument(); // cpk
    expect(within(cap).getByText(/^120 measurements, /)).toBeInTheDocument();
  });

  // =========================================================================
  // 2. CHART — where `mean` / `subgroup_number` / `center_line` actually land
  // =========================================================================

  it('plots chart_points on subgroup_number/mean and draws CL from center_line', async () => {
    await selectCharacteristic();
    const chart = panel('Control Chart: Bore Diameter');

    const charts = within(chart).getAllByTestId('line-chart');
    // X-bar chart + R chart (R is rendered because sample_count > 1).
    expect(charts).toHaveLength(2);

    // The page feeds `chart_points` straight through — not a flat [{index,value,timestamp}].
    expect(JSON.parse(charts[0].getAttribute('data-chart-data') ?? 'null')).toEqual(
      chartData.chart_points
    );

    const axes = within(chart).getAllByTestId('x-axis');
    axes.forEach((axis) => expect(axis).toHaveAttribute('data-key', 'subgroup_number'));

    const series = within(chart).getAllByTestId('chart-series');
    expect(series[0]).toHaveAttribute('data-key', 'mean');
    expect(series[0]).toHaveAttribute('data-name', 'Subgroup mean (X̄)');
    expect(series[1]).toHaveAttribute('data-key', 'range');

    const lines = referenceLines(chart);
    // Control limits — voice of the process. `center_line`, NOT `cl`.
    expect(lines.UCL).toBe('1.552');
    expect(lines.CL).toBe('1.5');
    expect(lines.LCL).toBe('1.448');
    // Spec limits — voice of the customer, drawn separately and never conflated.
    expect(lines.USL).toBe('1.55');
    expect(lines.LSL).toBe('1.45');
    // R-chart limits off the *_range fields.
    expect(lines.UCLr).toBe('0.09');
    expect(lines['R̄']).toBe('0.04');
  });

  // =========================================================================
  // 3. MEASUREMENTS TABLE
  // =========================================================================

  it('renders measurements newest-first from measurement_value, with no Measured By column', async () => {
    await selectCharacteristic();

    const table = screen.getByRole('table');
    const rows = within(table).getAllByRole('row');

    // GET /spc/measurements orders oldest-first and limits from the OLDEST end, so the
    // page slices from the tail and reverses. Newest subgroup must lead.
    expect(within(rows[1]).getByText('#12')).toBeInTheDocument();
    expect(within(rows[1]).getByText('1.562')).toBeInTheDocument(); // measurement_value
    expect(within(rows[1]).getByText('Rule1')).toBeInTheDocument(); // stored violation_rules
    expect(within(rows[2]).getByText('#11')).toBeInTheDocument();
    expect(within(rows[2]).getByText('1.498')).toBeInTheDocument();

    // `measured_by` is an int user id and no SPC route joins `users`; rendering it would
    // print a bare "4", and looking a name up would 403 for the roles most likely here.
    expect(within(table).queryByText('Measured By')).not.toBeInTheDocument();
    expect(within(table).queryByText('4')).not.toBeInTheDocument();
  });

  it('bounds the measurement read by time instead of taking the oldest rows', async () => {
    // GET /spc/measurements orders ASC and THEN applies `limit` (max 5000), so ANY limit
    // returns the OLDEST rows. Past that many measurements the "Recent" panel showed
    // ancient rows with stale out-of-control highlighting. The read is now bounded by a
    // `start_date` derived from the chart window, walking back real `sample_count`s until
    // 25 measurements are covered — here 5 subgroups of 5, i.e. chart_points[25].
    const longHistory: SPCChartPoint[] = Array.from({ length: 30 }, (_, i) => ({
      subgroup_number: i + 1,
      mean: 1.5,
      range: 0.02,
      sample_count: 5,
      is_out_of_control: false,
      violations: [],
      measured_at: `2026-06-01T08:${String(i).padStart(2, '0')}:00Z`,
    }));
    mockedApi.getSPCChartData.mockResolvedValue({ ...chartData, chart_points: longHistory });

    await selectCharacteristic();

    // Naive UTC — the route parses this with `datetime.fromisoformat` and compares it
    // against a NAIVE column, so the `Z` that `to_utc_iso` emits must not be sent.
    expect(mockedApi.getSPCMeasurements).toHaveBeenCalledWith(7, {
      start_date: '2026-06-01T08:25:00',
    });
  });

  it('leaves the measurement read unbounded when the whole history already fits', async () => {
    await selectCharacteristic();
    // 2 subgroups x 5 samples = 10 < 25: no window to bound, and no `limit` either (a
    // limit would take the oldest rows).
    expect(mockedApi.getSPCMeasurements).toHaveBeenCalledWith(7, undefined);
  });

  it('renders capability indices null-safely instead of claiming "Not Capable"', async () => {
    mockedApi.getSPCCapability.mockResolvedValue({
      ...capability,
      cp: null,
      cpk: null,
      pp: null,
      ppk: null,
    });
    await selectCharacteristic();

    const cap = panel('Process Capability');
    // `null >= 1.33` is false, so an unguarded comparison rendered a MISSING index as red
    // "Not Capable" — a false quality claim on an AS9100D surface.
    expect(within(cap).getAllByText('--')).toHaveLength(4);
    expect(within(cap).getAllByText('No data')).toHaveLength(4);
    expect(within(cap).queryByText('Not Capable')).not.toBeInTheDocument();
  });

  it('gives a capability verdict to Cpk/Ppk only, never to the spread-only Cp/Pp', async () => {
    // Cp/Pp ignore centring. A process sitting hard against one limit can carry a high Cp
    // and a failing Cpk at the same time, and the server's own verdict is Cpk-based
    // (`is_capable = cpk >= 1.33`). Labelling the Cp tile "Capable" would print a claim
    // the quality system does not make, right beside "Not Capable" on the Cpk tile.
    mockedApi.getSPCCapability.mockResolvedValue({
      ...capability,
      cp: 2.1,
      cpk: 0.6,
      pp: 2.05,
      ppk: 0.58,
      is_capable: false,
    });
    await selectCharacteristic();

    expect(within(capabilityTile('Cp')).getByText('Spread only')).toBeInTheDocument();
    expect(within(capabilityTile('Pp')).getByText('Spread only')).toBeInTheDocument();
    expect(within(capabilityTile('Cpk')).getByText('Not Capable')).toBeInTheDocument();
    expect(within(capabilityTile('Ppk')).getByText('Not Capable')).toBeInTheDocument();

    // The high-spread indices must not be dressed as a passing verdict anywhere.
    expect(within(panel('Process Capability')).queryByText('Capable')).not.toBeInTheDocument();
  });

  // =========================================================================
  // 4. WRITES
  // =========================================================================

  it('posts the batch measurement shape with subgroup_number/sample_number and no measured_by', async () => {
    await selectCharacteristic();

    fireEvent.click(screen.getByRole('button', { name: /Add Measurement/ }));

    // One rational subgroup of `subgroup_size` samples, and the subgroup number is DERIVED
    // read-only from the last chart point (12) + 1 — mirroring the kiosk's MAX(...)+1.
    expect(await screen.findByText(/Subgroup #13/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Measured By/)).not.toBeInTheDocument();

    const values = ['1.501', '1.499', '1.503', '1.497', '1.5'];
    values.forEach((value, idx) => {
      fireEvent.change(screen.getByLabelText(new RegExp(`^Sample ${idx + 1}\\b`)), {
        target: { value },
      });
    });
    fireEvent.change(screen.getByLabelText(/^Lot Number/), { target: { value: 'LOT-9' } });
    fireEvent.change(screen.getByLabelText(/^Notes/), { target: { value: 'shift A' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save Subgroup' }));

    await waitFor(() => expect(mockedApi.addSPCMeasurements).toHaveBeenCalledTimes(1));

    // MeasurementBatchCreate is a WRAPPER — a flat body 422s on ["measurements"].
    expect(mockedApi.addSPCMeasurements).toHaveBeenCalledWith({
      measurements: values.map((value, idx) => ({
        characteristic_id: 7,
        subgroup_number: 13,
        measurement_value: Number(value),
        sample_number: idx + 1,
        lot_number: 'LOT-9',
        serial_number: null,
        notes: 'shift A',
      })),
    });

    // toHaveBeenCalledWith treats an explicit `undefined` key as absent, so assert the key
    // itself is gone: the server stamps measured_by=current_user.id and silently drops it.
    const body = mockedApi.addSPCMeasurements.mock.calls[0][0];
    body.measurements.forEach((item) => {
      expect(Object.keys(item)).not.toContain('measured_by');
      expect(Object.keys(item)).not.toContain('value');
    });

    expect(await screen.findByText('Subgroup #13 recorded (5 samples).')).toBeInTheDocument();
  });

  it('re-derives the subgroup number from a fresh read at save, not from the stale page state', async () => {
    // There is no unique constraint on (characteristic_id, subgroup_number) and no 409.
    // The kiosk process-sheet capture path derives its own MAX+1 server-side, deliberately
    // under no lock, and writes to the same characteristics. Posting the number computed
    // when the modal opened would MERGE these readings into a subgroup the kiosk has since
    // created, retroactively changing its X̄ and R and duplicating sample_number.
    mockedApi.getSPCChartData.mockImplementation(async (_id: number, params?: { last_n_subgroups?: number }) =>
      params?.last_n_subgroups === 1
        ? // what the kiosk wrote while the modal sat open
          { ...chartData, chart_points: [{ ...chartData.chart_points[1], subgroup_number: 20 }] }
        : chartData
    );

    await selectCharacteristic();
    fireEvent.click(screen.getByRole('button', { name: /Add Measurement/ }));
    // The header still shows the advisory number derived at the last refresh...
    expect(await screen.findByText(/Subgroup #13/)).toBeInTheDocument();

    ['1.5', '1.5', '1.5', '1.5', '1.5'].forEach((value, idx) => {
      fireEvent.change(screen.getByLabelText(new RegExp(`^Sample ${idx + 1}\\b`)), {
        target: { value },
      });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Subgroup' }));

    await waitFor(() => expect(mockedApi.addSPCMeasurements).toHaveBeenCalledTimes(1));

    // ...but every posted row carries 21, re-derived immediately before the write.
    const body = mockedApi.addSPCMeasurements.mock.calls[0][0];
    body.measurements.forEach((item) => expect(item.subgroup_number).toBe(21));
    expect(await screen.findByText('Subgroup #21 recorded (5 samples).')).toBeInTheDocument();
  });

  it('creates a characteristic with characteristic_type and specification_* field names', async () => {
    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });

    fireEvent.click(screen.getAllByRole('button', { name: 'New Characteristic' })[0]);

    // `part_id` is REQUIRED and immutable after create, so it is a real picker rather than
    // an unvalidated text box that sent parseInt('') -> NaN -> JSON null -> 422.
    const partSelect = await screen.findByLabelText(/^Part\b/);
    await waitFor(() => expect(within(partSelect).getByText(/P-1000/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/^Name\b/), { target: { value: 'Flange Thickness' } });
    fireEvent.change(partSelect, { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText(/^Characteristic Type\b/), { target: { value: 'force' } });
    fireEvent.change(screen.getByLabelText(/^Nominal\b/), { target: { value: '2.5' } });
    fireEvent.change(screen.getByLabelText(/^USL\b/), { target: { value: '2.6' } });
    fireEvent.change(screen.getByLabelText(/^LSL\b/), { target: { value: '2.4' } });
    fireEvent.change(screen.getByLabelText(/^Unit\b/), { target: { value: 'mm' } });
    fireEvent.change(screen.getByLabelText(/^Subgroup Size\b/), { target: { value: '4' } });
    fireEvent.click(screen.getByLabelText('Critical characteristic'));

    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockedApi.createSPCCharacteristic).toHaveBeenCalledTimes(1));

    expect(mockedApi.createSPCCharacteristic).toHaveBeenCalledWith({
      name: 'Flange Thickness',
      part_id: 10,
      // REQUIRED by CharacteristicCreate — omitting it is a 422.
      characteristic_type: 'force',
      unit_of_measure: 'mm',
      // The server names these `specification_*`. Sending nominal/usl/lsl is NOT an error:
      // pydantic extra='ignore' drops them, the characteristic is stored with no spec
      // limits, and every capability study then 400s "USL and LSL must be defined".
      specification_nominal: 2.5,
      specification_usl: 2.6,
      specification_lsl: 2.4,
      chart_type: 'xbar_r',
      subgroup_size: 4,
      is_critical: true,
    });

    const body = mockedApi.createSPCCharacteristic.mock.calls[0][0];
    expect(Object.keys(body)).not.toContain('nominal');
    expect(Object.keys(body)).not.toContain('usl');
    expect(Object.keys(body)).not.toContain('lsl');
  });

  it('offers only the one chart model the server actually computes', async () => {
    // calculate_control_limits computes X-bar/R for EVERY chart_type and
    // check_western_electric_rules evaluates X-bar rules against it. A characteristic
    // stored as "P Chart" or "Individual & MR" would therefore record a model the system
    // never runs — and individuals data (one sample per subgroup) collapses X-bar/R limits
    // to UCL = CL = LCL, which also clears every recorded violation.
    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });
    fireEvent.click(screen.getAllByRole('button', { name: 'New Characteristic' })[0]);

    const chartTypeSelect = (await screen.findByLabelText(/^Chart Type\b/)) as HTMLSelectElement;
    expect(chartTypeSelect).toBeDisabled();
    expect(within(chartTypeSelect).getAllByRole('option')).toHaveLength(1);
    expect(chartTypeSelect.value).toBe('xbar_r');
    expect(within(chartTypeSelect).queryByText('Individual & MR')).not.toBeInTheDocument();
    expect(within(chartTypeSelect).queryByText('P Chart')).not.toBeInTheDocument();
  });

  it('caps free-text fields at the backend column widths', async () => {
    // No server-side length validation: on Postgres an over-length value is a 500, not a 422.
    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });
    fireEvent.click(screen.getAllByRole('button', { name: 'New Characteristic' })[0]);

    expect(await screen.findByLabelText(/^Name\b/)).toHaveAttribute('maxlength', '255');
    expect(screen.getByLabelText(/^Unit\b/)).toHaveAttribute('maxlength', '50');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    fireEvent.click(await screen.findByRole('button', { name: /Bore Diameter/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Add Measurement/ }));
    expect(await screen.findByLabelText(/^Lot Number/)).toHaveAttribute('maxlength', '100');
    expect(screen.getByLabelText(/^Serial Number/)).toHaveAttribute('maxlength', '100');
  });

  it('refuses to submit a characteristic with no part selected instead of sending null', async () => {
    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });
    fireEvent.click(screen.getAllByRole('button', { name: 'New Characteristic' })[0]);

    fireEvent.change(await screen.findByLabelText(/^Name\b/), { target: { value: 'Wall Thickness' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    expect(
      await screen.findByText('Select the part this characteristic belongs to.')
    ).toBeInTheDocument();
    expect(mockedApi.createSPCCharacteristic).not.toHaveBeenCalled();
  });

  it('skips the capability study when the spec limits that it requires are absent', async () => {
    mockedApi.getSPCCharacteristics.mockResolvedValue([
      { ...characteristic, specification_usl: null, specification_lsl: null },
    ]);
    await selectCharacteristic();

    fireEvent.click(screen.getByRole('button', { name: /Recalculate/ }));

    await waitFor(() => expect(mockedApi.calculateSPCControlLimits).toHaveBeenCalledWith(7));
    // POST /spc/capability-study 400s "USL and LSL must be defined" without both limits —
    // which is exactly what every UI-created characteristic used to hit, because the create
    // call dropped the spec fields.
    expect(mockedApi.runSPCCapabilityStudy).not.toHaveBeenCalled();
    expect(
      await screen.findByText('Control limits recalculated. Set USL and LSL to run a capability study.')
    ).toBeInTheDocument();
  });

  // =========================================================================
  // 5. UNSUPPORTED SHAPES — say so rather than firing a guaranteed refusal
  // =========================================================================

  it('flags a characteristic whose chart type the server does not model', async () => {
    mockedApi.getSPCCharacteristics.mockResolvedValue([
      { ...characteristic, chart_type: 'p_chart' },
    ]);
    await selectCharacteristic();

    expect(
      screen.getByText(/labelled P Chart, but the server computes control limits/)
    ).toBeInTheDocument();
    // The advisory explains it; the control still works, because the limits it produces
    // are real X-bar/R limits.
    expect(screen.getByRole('button', { name: /Recalculate/ })).toBeInTheDocument();
  });

  it('withholds Recalculate for a subgroup size the limit calculator refuses', async () => {
    // POST .../calculate 400s "Subgroup size must be between 2 and 10 for X-bar/R charts",
    // so the control is not offered — the same principle as not firing a capability study
    // without spec limits.
    mockedApi.getSPCCharacteristics.mockResolvedValue([{ ...characteristic, subgroup_size: 1 }]);
    await selectCharacteristic();

    expect(screen.getByText(/Subgroup size 1 is outside the 2–10 range/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Recalculate/ })).not.toBeInTheDocument();
    expect(mockedApi.calculateSPCControlLimits).not.toHaveBeenCalled();
  });

  // =========================================================================
  // 6. VIOLATIONS + OUT-OF-CONTROL
  // =========================================================================

  it('renders violations from the object payload keyed on subgroup_number', async () => {
    await selectCharacteristic();

    const viol = panel('Control Violations');
    // GET /spc/violations returns an OBJECT; the array lives at `.violations`, its rows
    // carry {subgroup_number, subgroup_mean, rules_violated} and have no id/timestamp.
    expect(within(viol).getByText('Subgroup #12 · mean 1.556 in')).toBeInTheDocument();
    expect(within(viol).getByText('Rule1')).toBeInTheDocument();
    expect(within(viol).getByText('Rule2')).toBeInTheDocument();
    expect(within(viol).getByTitle('One point beyond 3σ (outside a control limit)')).toBeInTheDocument();
    expect(within(viol).queryByText('No violations')).not.toBeInTheDocument();
    expect(screen.getByText('1 violating subgroup total')).toBeInTheDocument();
  });

  it('renders out-of-control alerts from ooc_count and last_ooc', async () => {
    mockedApi.getSPCOutOfControl.mockResolvedValue([oocAlert]);
    renderSPC();

    const alerts = await screen.findByRole('heading', { name: 'Out-of-Control Alerts (1)' });
    const alertPanel = alerts.closest('.card') as HTMLElement;
    // The old interface expected `reason`/`description`, which the endpoint never returns.
    expect(within(alertPanel).getByText('3 out-of-control measurements')).toBeInTheDocument();
    expect(within(alertPanel).getByText(/^Last: /)).toBeInTheDocument();
    expect(within(alertPanel).getByText('Critical')).toBeInTheDocument();
    // part_id is on this payload too — the alert names the part it belongs to.
    expect(await within(alertPanel).findByText('P-1000')).toBeInTheDocument();
  });

  // =========================================================================
  // 7. LIST SCOPE + REQUEST ORDERING
  // =========================================================================

  it('can reach deactivated characteristics and their history', async () => {
    // The list is active-only by default. Without a way back, deactivating a
    // characteristic would hide it AND its whole measurement history from the only page
    // that reads it — `updateSPCCharacteristic` has no UI caller to reactivate it either.
    const retired: SPCCharacteristic = {
      ...characteristic,
      id: 8,
      name: 'Retired Groove Width',
      is_active: false,
    };
    mockedApi.getSPCCharacteristics.mockResolvedValue([characteristic]);

    renderSPC();
    await screen.findByRole('button', { name: /Bore Diameter/ });
    expect(screen.queryByRole('button', { name: /Retired Groove Width/ })).not.toBeInTheDocument();

    mockedApi.getSPCCharacteristics.mockResolvedValue([characteristic, retired]);
    fireEvent.click(screen.getByLabelText('Show inactive'));

    expect(await screen.findByRole('button', { name: /Retired Groove Width/ })).toBeInTheDocument();
    // No `is_active` filter at all — not `is_active: false`, which would hide the active ones.
    expect(mockedApi.getSPCCharacteristics).toHaveBeenLastCalledWith(undefined);
    expect(within(panel('Characteristics')).getByText('Inactive')).toBeInTheDocument();
  });

  it('discards a slow detail response for a characteristic the user has left', async () => {
    // Without a request guard, A's late response overwrites B's: B stays selected and
    // titled while the chart, measurements and violations all show A — and the next
    // subgroup number would then be derived from A's window and posted against B.
    const second: SPCCharacteristic = { ...characteristic, id: 8, name: 'Flange Thickness' };
    mockedApi.getSPCCharacteristics.mockResolvedValue([characteristic, second]);

    const secondPoints: SPCChartPoint[] = [
      {
        subgroup_number: 90,
        mean: 2.222,
        range: 0.01,
        sample_count: 5,
        is_out_of_control: false,
        violations: [],
        measured_at: '2026-06-28T16:00:00Z',
      },
    ];

    let releaseFirst: (value: SPCChartData) => void = () => {};
    mockedApi.getSPCChartData.mockImplementation((id: number) =>
      id === 7
        ? new Promise<SPCChartData>((resolve) => {
            releaseFirst = resolve;
          })
        : Promise.resolve({ ...chartData, chart_points: secondPoints })
    );

    renderSPC();
    fireEvent.click(await screen.findByRole('button', { name: /Bore Diameter/ }));
    fireEvent.click(screen.getByRole('button', { name: /Flange Thickness/ }));

    await screen.findByRole('heading', { name: 'Control Chart: Flange Thickness' });

    // A finally answers, long after the user moved on.
    releaseFirst(chartData);

    await waitFor(() => {
      const chart = within(panel('Control Chart: Flange Thickness')).getAllByTestId('line-chart')[0];
      expect(JSON.parse(chart.getAttribute('data-chart-data') ?? 'null')).toEqual(secondPoints);
    });
    expect(
      screen.queryByRole('heading', { name: 'Control Chart: Bore Diameter' })
    ).not.toBeInTheDocument();
  });

  // =========================================================================
  // 8. EMPTY + ERROR STATES
  // =========================================================================

  it('treats null control limits as data: empty violations state with a recalculate CTA', async () => {
    mockedApi.getSPCControlLimits.mockResolvedValue(null);
    mockedApi.getSPCCapability.mockResolvedValue(null);
    mockedApi.getSPCChartData.mockResolvedValue({ ...chartData, control_limits: null });
    mockedApi.getSPCViolations.mockResolvedValue({
      violations: [],
      message: 'No control limits calculated yet',
    });

    await selectCharacteristic();

    // 200-with-a-JSON-null body is data, not a failure.
    expect(screen.queryByText('Could not load details for Bore Diameter.')).not.toBeInTheDocument();

    const viol = panel('Control Violations');
    expect(within(viol).getByTestId('empty-state')).toBeInTheDocument();
    expect(
      within(viol).getByRole('button', { name: 'Recalculate control limits' })
    ).toBeInTheDocument();

    // No control-limit reference lines, but the spec limits still draw.
    const lines = referenceLines(panel('Control Chart: Bore Diameter'));
    expect(lines.CL).toBeUndefined();
    expect(lines.UCL).toBeUndefined();
    expect(lines.USL).toBe('1.55');

    expect(screen.queryByRole('heading', { name: 'Process Capability' })).not.toBeInTheDocument();
  });

  it('renders the no-measurements empty state and derives subgroup #1 for the first entry', async () => {
    mockedApi.getSPCMeasurements.mockResolvedValue([]);
    mockedApi.getSPCChartData.mockResolvedValue({ ...chartData, chart_points: [] });
    mockedApi.getSPCViolations.mockResolvedValue({ ...violations, violations: [], total_violations: 0 });

    await selectCharacteristic();

    const chart = panel('Control Chart: Bore Diameter');
    expect(within(chart).getByText('No measurements yet')).toBeInTheDocument();
    expect(within(chart).queryByTestId('line-chart')).not.toBeInTheDocument();
    expect(screen.getByText('No measurements recorded.')).toBeInTheDocument();

    fireEvent.click(within(chart).getByRole('button', { name: 'Add Measurement' }));
    // Empty chart window -> (0 + 1). The derivation must not crash or land on NaN.
    expect(await screen.findByText(/Subgroup #1\b/)).toBeInTheDocument();
  });

  it('recovers from a failed dashboard load and renders real rows on retry', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockedApi.getSPCDashboard.mockRejectedValueOnce(new Error('boom'));

    renderSPC();

    expect(await screen.findByText('Could not load SPC dashboard data.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Bore Diameter/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    // The retry path must unwrap the same way the initial load does.
    expect(await screen.findByRole('button', { name: /Bore Diameter/ })).toBeInTheDocument();
    expect(within(statTile('Characteristics Monitored')).getByText('3')).toBeInTheDocument();
    expect(screen.queryByText('Could not load SPC dashboard data.')).not.toBeInTheDocument();

    consoleError.mockRestore();
  });

  it('surfaces a failed detail load with a retry that refetches the characteristic', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockedApi.getSPCChartData.mockRejectedValueOnce(new Error('boom'));

    renderSPC();
    fireEvent.click(await screen.findByRole('button', { name: /Bore Diameter/ }));

    expect(await screen.findByText('Could not load details for Bore Diameter.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(
      await screen.findByRole('heading', { name: 'Control Chart: Bore Diameter' })
    ).toBeInTheDocument();
    expect(mockedApi.getSPCChartData).toHaveBeenCalledTimes(2);

    consoleError.mockRestore();
  });

  it('shows the server error detail when a write is refused', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockedApi.addSPCMeasurements.mockRejectedValue({
      response: { data: { detail: 'Characteristic not found' } },
    });

    await selectCharacteristic();
    fireEvent.click(screen.getByRole('button', { name: /Add Measurement/ }));
    await screen.findByText(/Subgroup #13/);

    ['1.5', '1.5', '1.5', '1.5', '1.5'].forEach((value, idx) => {
      fireEvent.change(screen.getByLabelText(new RegExp(`^Sample ${idx + 1}\\b`)), {
        target: { value },
      });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Subgroup' }));

    expect(await screen.findByText('Characteristic not found')).toBeInTheDocument();

    consoleError.mockRestore();
  });

  it('keeps the page usable when the part list fails to load', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockedApi.getParts.mockRejectedValue(new Error('boom'));

    renderSPC();

    // Characteristics still render; the label degrades to the bare id rather than guessing.
    expect(await screen.findByRole('button', { name: /Bore Diameter/ })).toBeInTheDocument();
    expect(
      within(panel('Characteristics')).getByText('Part #10 · X-bar & R · n=5')
    ).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole('button', { name: 'New Characteristic' })[0]);
    expect(await screen.findByText('Could not load the parts list.')).toBeInTheDocument();

    consoleError.mockRestore();
  });

  // =========================================================================
  // 9. Recalculate gating
  //
  // POST /spc/control-limits/{id}/calculate rewrites is_out_of_control and
  // violation_rules IN PLACE on historical measurements, so the server gates it to
  // ADMIN/MANAGER/QUALITY. /spc itself is only `quality:view`, which operator and viewer
  // both hold — so without this control-level gate those roles see a button whose only
  // outcome is a 403 toast. The server gate is still the enforcement; this is discovery.
  // =========================================================================

  it.each(['admin', 'manager', 'quality'])('shows Recalculate for %s', async (role) => {
    mockRole = role;
    await selectCharacteristic();
    expect(screen.getByRole('button', { name: /Recalculate/ })).toBeInTheDocument();
  });

  it.each(['operator', 'viewer', 'shipping', 'supervisor'])('hides Recalculate for %s', async (role) => {
    mockRole = role;
    await selectCharacteristic();
    // The panel still renders with its real data — only the control that would 403 is withheld.
    expect(screen.getByRole('heading', { name: 'Control Chart: Bore Diameter' })).toBeInTheDocument();
    expect(screen.getByText('Nominal: 1.5')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Recalculate/ })).not.toBeInTheDocument();
  });
});

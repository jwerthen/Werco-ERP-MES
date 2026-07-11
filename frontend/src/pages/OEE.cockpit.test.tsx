/**
 * OEE cockpit — contract + regression guard.
 *
 * CRITICAL: the mocks below mirror the REAL response shapes built in
 * backend/app/api/endpoints/oee.py — metric fields are `*_pct`, per-work-center
 * dashboard metrics are `current_oee_pct` / `availability_pct` / ... and are
 * legitimately `null` until a record exists, and GET /oee/trends returns an
 * OBJECT ({ time_series: [...] }), not a bare array. The previous version of this
 * test mocked invented field names (`plant_oee`, `work_centers:[{oee, ...}]`), so
 * it stayed green while the page threw `undefined.toFixed()` in production. A test
 * that does not mirror the real contract is worthless here — keep these in sync
 * with the endpoint.
 *
 * The page renders a recharts LineChart (trends) via ResponsiveContainer, which
 * needs ResizeObserver — jsdom doesn't provide one and setupTests doesn't mock
 * it, so we install a no-op here.
 */
import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import OEE from './OEE';

// recharts ResponsiveContainer needs ResizeObserver, absent in jsdom.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn() },
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Real GET /oee/dashboard shape (oee.py ~L557). Plant A/P/Q are NOT in the response —
// the page derives them from work_centers (average of the WCs that have data), so with
// these values the strip shows OEE 71.8%, A 80.0%, P 90.0%, Q 98.0%.
const dashboard = {
  plant_oee_pct: 71.8,
  work_centers: [
    {
      work_center_id: 1,
      work_center_code: 'LASER-1',
      work_center_name: 'Laser cell 1',
      current_oee_pct: 82.1,
      availability_pct: 89.0,
      performance_pct: 92.0,
      quality_pct: 99.0,
      record_date: '2026-07-01',
      target_oee_pct: 85,
      target_availability_pct: 90,
      target_performance_pct: 95,
      target_quality_pct: 99,
    },
    {
      work_center_id: 2,
      work_center_code: 'BRAKE-2',
      work_center_name: 'Press brake 2',
      current_oee_pct: 61.5,
      availability_pct: 71.0,
      performance_pct: 88.0,
      quality_pct: 97.0,
      record_date: '2026-07-01',
      target_oee_pct: 85,
      target_availability_pct: 90,
      target_performance_pct: 95,
      target_quality_pct: 99,
    },
  ],
  comparison: [],
  period: '30d',
};

// Real current prod state: 20 work centers, EVERY metric null (no OEE records yet).
// This is exactly the payload that crashed the page before the fix.
const nullMetricDashboard = {
  plant_oee_pct: 0.0,
  work_centers: [
    {
      work_center_id: 1,
      work_center_code: 'LASER-1',
      work_center_name: 'Laser cell 1',
      current_oee_pct: null,
      availability_pct: null,
      performance_pct: null,
      quality_pct: null,
      record_date: null,
      target_oee_pct: 85,
      target_availability_pct: 90,
      target_performance_pct: 95,
      target_quality_pct: 99,
    },
    {
      work_center_id: 2,
      work_center_code: 'BRAKE-2',
      work_center_name: 'Press brake 2',
      current_oee_pct: null,
      availability_pct: null,
      performance_pct: null,
      quality_pct: null,
      record_date: null,
      target_oee_pct: 85,
      target_availability_pct: 90,
      target_performance_pct: 95,
      target_quality_pct: 99,
    },
  ],
  comparison: [],
  period: '30d',
};

// Real GET /oee/trends shape (oee.py ~L616): an object with time_series, not an array.
const trends = {
  time_series: [
    {
      date: '2026-07-01',
      work_center_id: 1,
      work_center_name: 'Laser cell 1',
      oee_pct: 80.0,
      availability_pct: 90.0,
      performance_pct: 92.0,
      quality_pct: 98.0,
      total_parts: 100,
      good_parts: 98,
      defect_parts: 2,
    },
    {
      date: '2026-07-02',
      work_center_id: 1,
      work_center_name: 'Laser cell 1',
      oee_pct: 82.0,
      availability_pct: 91.0,
      performance_pct: 93.0,
      quality_pct: 97.0,
      total_parts: 110,
      good_parts: 107,
      defect_parts: 3,
    },
  ],
  target_oee_pct: 85,
  target_availability_pct: 90,
  target_performance_pct: 95,
  target_quality_pct: 99,
  period: '30d',
};

// Real GET /oee/records shape (oee.py _record_to_response ~L162): `*_pct` metrics,
// `total_parts` / `good_parts` / `defect_parts`, and `work_center_name` (no nested object).
const records = [
  {
    id: 10,
    work_center_id: 1,
    work_center_name: 'Laser cell 1',
    record_date: '2026-07-01',
    shift: '1st',
    availability_pct: 89.0,
    performance_pct: 92.0,
    quality_pct: 99.0,
    oee_pct: 81.1,
    total_parts: 100,
    good_parts: 98,
    defect_parts: 2,
    notes: null,
    created_at: null,
  },
];

const workCenters = [
  { id: 1, code: 'LASER-1', name: 'Laser cell 1', is_active: true },
  { id: 2, code: 'BRAKE-2', name: 'Press brake 2', is_active: true },
];

// Route api.get responses by URL. Overrides let a test swap a single payload.
function mockApiGet(overrides: Partial<Record<string, any>> = {}) {
  const byUrl: Record<string, any> = {
    dashboard: overrides.dashboard ?? dashboard,
    trends: overrides.trends ?? trends,
    records: overrides.records ?? [],
    workCenters: overrides.workCenters ?? workCenters,
  };
  mockedApi.get.mockImplementation((url: string) => {
    if (url.startsWith('/work-centers')) return Promise.resolve({ data: byUrl.workCenters } as any);
    if (url === '/oee/dashboard') return Promise.resolve({ data: byUrl.dashboard } as any);
    if (url === '/oee/trends') return Promise.resolve({ data: byUrl.trends } as any);
    if (url === '/oee/records') return Promise.resolve({ data: byUrl.records } as any);
    return Promise.resolve({ data: [] } as any);
  });
}

const renderOEE = () => render(<MemoryRouter><OEE /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockApiGet();
  mockedApi.post.mockResolvedValue({ data: {} } as any);
});

test('renders the 4-up plant MiniStat strip (plant A/P/Q derived from work centers)', async () => {
  renderOEE();

  // Plant-wide OEE MiniStat is the canonical "loaded" marker (unique label).
  expect(await screen.findByText('Plant-wide OEE')).toBeInTheDocument();
  expect(screen.getAllByText('Availability').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Performance').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Quality').length).toBeGreaterThan(0);

  // Plant OEE is average of the WC current_oee_pct (82.1, 61.5) => 71.8; A/P/Q likewise.
  expect(screen.getByText('71.8%')).toBeInTheDocument();
  expect(screen.getByText('80.0%')).toBeInTheDocument(); // (89.0 + 71.0) / 2
  expect(screen.getByText('90.0%')).toBeInTheDocument(); // (92.0 + 88.0) / 2
  expect(screen.getByText('98.0%')).toBeInTheDocument(); // (99.0 + 97.0) / 2
});

test('renders a Work Center OEE tile per work center', async () => {
  renderOEE();
  await screen.findByText('Work Center OEE');

  // Each work center is a tappable tile (button) carrying its code.
  expect(screen.getByRole('button', { name: /LASER-1/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /BRAKE-2/i })).toBeInTheDocument();
  // The tile shows the WC's current OEE (current_oee_pct), formatted.
  expect(screen.getByText('82.1%')).toBeInTheDocument();
  expect(screen.getByText('61.5%')).toBeInTheDocument();
});

test('clicking a Work Center tile selects it and opens the detail panel', async () => {
  renderOEE();
  await screen.findByText('Work Center OEE');

  // No detail panel before selecting.
  expect(screen.queryByText('Selected work center detail')).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: /LASER-1/i }));

  // The selected-WC detail panel appears, headed by the WC code + name.
  const detail = await screen.findByText('Selected work center detail');
  expect(detail).toBeInTheDocument();
  expect(screen.getByText('LASER-1 — Laser cell 1')).toBeInTheDocument();
});

// THE regression guard: the real current prod payload has every metric `null`.
// Before the fix the page did `wc.oee.toFixed(1)` / `plantOEE.toFixed(1)` on
// undefined and threw, tripping the "Something went wrong" error boundary.
test('renders without crashing when all metrics are null (real prod state), showing --', async () => {
  mockApiGet({ dashboard: nullMetricDashboard });
  renderOEE();

  // Page renders (a throw during render would fail this findByText).
  expect(await screen.findByText('Plant-wide OEE')).toBeInTheDocument();
  // The work-center tiles still render (20 WCs in prod; 2 here).
  expect(screen.getByRole('button', { name: /LASER-1/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /BRAKE-2/i })).toBeInTheDocument();
  // Null metrics read as "no data" (`--`), not a fabricated 0.0%.
  expect(screen.getAllByText('--').length).toBeGreaterThan(0);
  expect(screen.queryByText('0.0%')).toBeNull();
});

// GET /oee/trends returns an OBJECT; the page must unwrap time_series so the chart shows.
test('unwraps the trends time_series object and renders the trend chart', async () => {
  renderOEE();
  // The chart panel only renders when trends.length > 0, which requires the object
  // to have been unwrapped into its time_series array. Title day-count is derived from
  // the (default 30-day) From/To range, so match loosely.
  expect(await screen.findByText(/OEE Trends/)).toBeInTheDocument();
});

// The From/To range scopes the WHOLE dashboard; the dead `days` param is gone; and the
// work-center selection is a detail-only filter (dashboard tiles stay plant-wide).
test('scopes the dashboard and trends by the From/To date range, not the ignored days param', async () => {
  renderOEE();
  await screen.findByText('Plant-wide OEE');

  const calls = mockedApi.get.mock.calls;
  const paramsOf = (url: string) => (calls.find((c) => c[0] === url)?.[1] as any)?.params;

  // Dashboard is scoped by the date range (so the plant strip follows the filter)...
  expect(paramsOf('/oee/dashboard')).toHaveProperty('date_from');
  expect(paramsOf('/oee/dashboard')).toHaveProperty('date_to');
  // ...but NOT by work_center_id (the tile grid stays a plant-wide overview)...
  expect(paramsOf('/oee/dashboard')).not.toHaveProperty('work_center_id');
  // ...and never the dead `days` key the endpoint ignores.
  expect(paramsOf('/oee/dashboard')).not.toHaveProperty('days');

  // Trends + records are scoped by the range too (trends no longer sends `days`).
  expect(paramsOf('/oee/trends')).toHaveProperty('date_from');
  expect(paramsOf('/oee/trends')).toHaveProperty('date_to');
  expect(paramsOf('/oee/trends')).not.toHaveProperty('days');
  expect(paramsOf('/oee/records')).toHaveProperty('date_from');
});

test('renders OEE records using the real *_pct / *_parts fields', async () => {
  mockApiGet({ records });
  renderOEE();

  await screen.findByText('Plant-wide OEE');
  // Scope to the records table (the only <table>) to disambiguate 'Laser cell 1',
  // which also appears as a work-center tile subtitle.
  const table = within(screen.getByRole('table'));
  // work_center_name (not the old nested work_center.code, which would render 'WC-1').
  expect(table.getByText('Laser cell 1')).toBeInTheDocument();
  expect(table.queryByText('WC-1')).toBeNull();
  // The record's OEE via oee_pct.
  expect(table.getByText('81.1%')).toBeInTheDocument();
});

// The "Add Record" POST must use the backend OEERecordCreate field names. The old body
// sent planned_production_time/total_pieces/... which Pydantic silently ignored, so every
// submit created an all-zero record. Records is non-empty here so the empty-state's own
// "Add Record" action button doesn't collide with the header button.
test('Add Record posts the backend contract field names, not the ignored *_pieces names', async () => {
  mockApiGet({ records });
  renderOEE();
  await screen.findByText('Plant-wide OEE');

  // Open the modal (only the header "Add Record" exists while records are present).
  fireEvent.click(screen.getByRole('button', { name: /add record/i }));
  const modal = (await screen.findByText('Add OEE Record')).closest('.modal-box') as HTMLElement;
  const modalUtils = within(modal);

  // Select a work center (the only field the submit handler requires)...
  fireEvent.change(modalUtils.getByRole('combobox', { name: /work center/i }), {
    target: { value: '1' },
  });
  // ...and set a NONZERO Actual Run Time so the double-mapping is genuinely pinned:
  // actual_run_time must feed BOTH actual_run_time_minutes (availability numerator) and
  // actual_operating_time_minutes (performance denominator). All-zero inputs couldn't tell
  // a correct mapping from a regression that pointed both at some other zero source.
  fireEvent.change(modalUtils.getByRole('spinbutton', { name: /actual run time/i }), {
    target: { value: '300' },
  });
  fireEvent.click(modalUtils.getByRole('button', { name: /add record/i }));

  await waitFor(() => expect(mockedApi.post).toHaveBeenCalled());
  const [url, body] = mockedApi.post.mock.calls[0] as [string, Record<string, any>];
  expect(url).toBe('/oee/records');
  expect(body).toMatchObject({
    work_center_id: 1,
    planned_production_time_minutes: 480,
    actual_run_time_minutes: 300,
    actual_operating_time_minutes: 300,
    ideal_cycle_time_seconds: 0,
    total_parts_produced: 0,
    total_parts: 0,
    good_parts: 0,
    defect_parts: 0,
  });
  // The performance operating-time denominator must equal actual_run_time_minutes.
  expect(body.actual_operating_time_minutes).toBe(body.actual_run_time_minutes);
  // Guard against the exact regression: the silently-ignored legacy keys must be gone.
  expect(body).not.toHaveProperty('total_pieces');
  expect(body).not.toHaveProperty('good_pieces');
  expect(body).not.toHaveProperty('planned_production_time');
});

// Regression: the work-centers request must use the TRAILING-SLASH collection path
// (`/work-centers/`) with params. Calling `/work-centers?active_only=true` (no slash)
// triggered a FastAPI 307 redirect whose cross-origin response carried no CORS header,
// so the browser failed it with status 0 and the whole dashboard blanked.
test('requests work-centers with a trailing slash (not the redirect-prone slashless path)', async () => {
  renderOEE();
  await screen.findByText('Plant-wide OEE');

  const calledUrls = mockedApi.get.mock.calls.map((c) => c[0] as string);
  expect(calledUrls).toContain('/work-centers/');
  // Guard against the exact regression: no slashless / query-in-path variant.
  expect(calledUrls.some((u) => u.startsWith('/work-centers?'))).toBe(false);
  expect(calledUrls.some((u) => u === '/work-centers')).toBe(false);

  // active_only is passed as a param, not baked into the path.
  const wcCall = mockedApi.get.mock.calls.find((c) => (c[0] as string) === '/work-centers/');
  expect(wcCall?.[1]).toEqual({ params: { active_only: true } });
});

// Resilience: a failed work-centers call (its only job is the filter dropdown) must
// NOT blank the dashboard. Promise.allSettled lets the core /oee/dashboard render.
test('still renders the dashboard when the work-centers request fails', async () => {
  const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  mockedApi.get.mockImplementation((url: string) => {
    if (url.startsWith('/work-centers')) return Promise.reject(new Error('status 0 (CORS redirect)'));
    if (url === '/oee/dashboard') return Promise.resolve({ data: dashboard } as any);
    if (url === '/oee/trends') return Promise.resolve({ data: trends } as any);
    if (url === '/oee/records') return Promise.resolve({ data: [] } as any);
    return Promise.resolve({ data: [] } as any);
  });

  renderOEE();

  // Core dashboard content still renders...
  expect(await screen.findByText('Plant-wide OEE')).toBeInTheDocument();
  expect(screen.getByText('71.8%')).toBeInTheDocument();
  // ...and the full-page error state is NOT shown.
  expect(screen.queryByText('Could not load the OEE dashboard. Check your connection and try again.')).toBeNull();

  errorSpy.mockRestore();
});

// The inverse guard: if the CORE /oee/dashboard call fails, the error state DOES show.
test('shows the error state when the core dashboard request fails', async () => {
  const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  mockedApi.get.mockImplementation((url: string) => {
    if (url.startsWith('/work-centers')) return Promise.resolve({ data: workCenters } as any);
    if (url === '/oee/dashboard') return Promise.reject(new Error('500'));
    if (url === '/oee/trends') return Promise.resolve({ data: trends } as any);
    if (url === '/oee/records') return Promise.resolve({ data: [] } as any);
    return Promise.resolve({ data: [] } as any);
  });

  renderOEE();

  expect(
    await screen.findByText('Could not load the OEE dashboard. Check your connection and try again.'),
  ).toBeInTheDocument();

  errorSpy.mockRestore();
});

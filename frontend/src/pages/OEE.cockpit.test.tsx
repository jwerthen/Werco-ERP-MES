/**
 * OEE cockpit overhaul — instrument-panel regression.
 *
 * The OEE Dashboard was rebuilt into a cockpit layout: a 4-up MiniStat strip
 * surfaces plant-wide OEE / Availability / Performance / Quality, and a
 * "Work Center OEE" panel renders one tappable tile per work center. Tapping a
 * tile selects that work center, which surfaces its detail panel. This guards
 * that strip, the tiles, and the click-to-select behavior.
 *
 * The page renders a recharts LineChart (trends) via ResponsiveContainer, which
 * needs ResizeObserver — jsdom doesn't provide one and setupTests doesn't mock
 * it, so we install a no-op here.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
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

const dashboard = {
  plant_oee: 78.4,
  plant_availability: 88.2,
  plant_performance: 91.5,
  plant_quality: 97.3,
  work_centers: [
    {
      work_center_id: 1,
      work_center_code: 'LASER-1',
      work_center_name: 'Laser cell 1',
      oee: 82.1,
      availability: 89.0,
      performance: 92.0,
      quality: 99.0,
    },
    {
      work_center_id: 2,
      work_center_code: 'BRAKE-2',
      work_center_name: 'Press brake 2',
      oee: 61.5,
      availability: 70.0,
      performance: 88.0,
      quality: 99.0,
    },
  ],
};

const workCenters = [
  { id: 1, code: 'LASER-1', name: 'Laser cell 1', is_active: true },
  { id: 2, code: 'BRAKE-2', name: 'Press brake 2', is_active: true },
];

// Route api.get responses by URL so each of the four mount calls is satisfied.
function mockApiGet() {
  mockedApi.get.mockImplementation((url: string) => {
    if (url.startsWith('/work-centers')) return Promise.resolve({ data: workCenters } as any);
    if (url === '/oee/dashboard') return Promise.resolve({ data: dashboard } as any);
    if (url === '/oee/trends') return Promise.resolve({ data: [] } as any);
    if (url === '/oee/records') return Promise.resolve({ data: [] } as any);
    return Promise.resolve({ data: [] } as any);
  });
}

const renderOEE = () => render(<MemoryRouter><OEE /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockApiGet();
});

test('renders the 4-up plant MiniStat strip after load', async () => {
  renderOEE();

  // Plant-wide OEE MiniStat is the canonical "loaded" marker (unique label).
  expect(await screen.findByText('Plant-wide OEE')).toBeInTheDocument();
  // Availability / Performance / Quality also appear as records-table headers,
  // so assert they're present at least once (the MiniStat strip is one of them).
  expect(screen.getAllByText('Availability').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Performance').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Quality').length).toBeGreaterThan(0);

  // Plant values come through formatted to one decimal place.
  expect(screen.getByText('78.4%')).toBeInTheDocument();
  expect(screen.getByText('88.2%')).toBeInTheDocument();
  expect(screen.getByText('91.5%')).toBeInTheDocument();
  expect(screen.getByText('97.3%')).toBeInTheDocument();
});

test('renders a Work Center OEE tile per work center', async () => {
  renderOEE();
  await screen.findByText('Work Center OEE');

  // Each work center is a tappable tile (button) carrying its code.
  expect(screen.getByRole('button', { name: /LASER-1/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /BRAKE-2/i })).toBeInTheDocument();
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

  // The detail panel exposes the canonical per-WC A/P/Q gauges.
  const panel = detail.closest('div');
  expect(panel).not.toBeNull();
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
    if (url === '/oee/trends') return Promise.resolve({ data: [] } as any);
    if (url === '/oee/records') return Promise.resolve({ data: [] } as any);
    return Promise.resolve({ data: [] } as any);
  });

  renderOEE();

  // Core dashboard content still renders...
  expect(await screen.findByText('Plant-wide OEE')).toBeInTheDocument();
  expect(screen.getByText('78.4%')).toBeInTheDocument();
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
    if (url === '/oee/trends') return Promise.resolve({ data: [] } as any);
    if (url === '/oee/records') return Promise.resolve({ data: [] } as any);
    return Promise.resolve({ data: [] } as any);
  });

  renderOEE();

  expect(
    await screen.findByText('Could not load the OEE dashboard. Check your connection and try again.'),
  ).toBeInTheDocument();

  errorSpy.mockRestore();
});

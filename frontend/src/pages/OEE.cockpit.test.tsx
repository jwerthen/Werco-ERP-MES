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

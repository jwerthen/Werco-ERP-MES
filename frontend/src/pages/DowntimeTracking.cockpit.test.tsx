/**
 * DowntimeTracking cockpit overhaul — render-correctness regression.
 *
 * The page was rebuilt into the instrument-panel "cockpit" layout: a compact
 * MiniStat KPI strip (Total Downtime / Planned / Unplanned / Top Reason) sits
 * above a cockpit grid of co-visible CockpitPanels (Work Center Status, Active
 * Downtime, and a Pareto bar chart). This guards two of the things that matter:
 *   1. the MiniStat strip renders its KPI tiles from the summary, and
 *   2. an open event renders ONCE as a dense Active Downtime row (cross-linked
 *      to the status board by work_center code), distinct from the log table.
 *
 * jsdom has no ResizeObserver and setupTests does not mock it; the Pareto
 * recharts ResponsiveContainer needs one, so it's stubbed at the top.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import DowntimeTracking from './DowntimeTracking';

// recharts ResponsiveContainer relies on ResizeObserver, absent in jsdom.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getActiveDowntime: jest.fn(),
    getDowntimeEvents: jest.fn(),
    getDowntimeReasonCodes: jest.fn(),
    getDowntimeSummary: jest.fn(),
    getDowntimeByWorkCenter: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenters = [
  { id: 1, code: 'LASER-1', name: 'Laser cell 1', current_status: 'in_use', is_active: true },
  { id: 2, code: 'MILL-2', name: 'Mill 2', current_status: 'idle', is_active: true },
];

// A single open (unresolved) event on LASER-1.
const activeEvent = {
  id: 101,
  work_center_id: 1,
  start_time: '2026-06-28T18:00:00Z',
  category: 'mechanical',
  planned_type: 'unplanned',
  reason_code: 'SPNDL',
  description: 'Spindle fault',
  reported_by: 7,
  created_at: '2026-06-28T18:00:00Z',
  updated_at: '2026-06-28T18:00:00Z',
  work_center: { id: 1, code: 'LASER-1', name: 'Laser cell 1' },
};

const summary = {
  total_downtime_hours: 12.5,
  planned_hours: 4,
  unplanned_hours: 8.5,
  planned_percentage: 32,
  unplanned_percentage: 68,
  by_category: [{ category: 'mechanical', hours: 8.5 }],
  top_reasons: [
    { reason: 'Spindle fault', hours: 6 },
    { reason: 'Tool change', hours: 3 },
  ],
  event_count: 5,
};

const reasonCodes = [
  { id: 1, code: 'SPNDL', name: 'Spindle fault', category: 'mechanical', is_active: true, display_order: 1 },
];

const renderPage = () => render(<MemoryRouter><DowntimeTracking /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue(workCenters as any);
  mockedApi.getActiveDowntime.mockResolvedValue([activeEvent] as any);
  mockedApi.getDowntimeEvents.mockResolvedValue([activeEvent] as any);
  mockedApi.getDowntimeReasonCodes.mockResolvedValue(reasonCodes as any);
  mockedApi.getDowntimeSummary.mockResolvedValue(summary as any);
  mockedApi.getDowntimeByWorkCenter.mockResolvedValue([] as any);
});

test('renders the MiniStat KPI strip from the summary', async () => {
  renderPage();

  // KPI tile labels + their tabular values.
  expect(await screen.findByText('Total Downtime')).toBeInTheDocument();
  expect(screen.getByText('12.5h')).toBeInTheDocument();
  expect(screen.getByText('5 events')).toBeInTheDocument();

  expect(screen.getByText('Planned')).toBeInTheDocument();
  expect(screen.getByText('4h')).toBeInTheDocument();

  // "Unplanned" also appears as a row badge + filter option, so assert on the
  // unique tile value instead of the (ambiguous) label.
  expect(screen.getByText('8.5h')).toBeInTheDocument();

  expect(screen.getByText('Top Reason')).toBeInTheDocument();
});

test('renders the active-downtime event once as a dense row in the Active Downtime panel', async () => {
  renderPage();

  // The Active Downtime cockpit panel. CockpitPanel renders the title in a
  // `card-header`, with the body (rows) as a sibling — walk up past the header
  // to the panel root (the `card card-compact` container) so the body is in scope.
  const heading = await screen.findByText('Active Downtime');
  const panel = heading.closest('.card.card-compact') as HTMLElement;
  expect(panel).not.toBeNull();

  // The open event renders as a row with the resolving control. There is exactly
  // one Resolve control in the active panel (one open event).
  const resolveButtons = within(panel as HTMLElement).getAllByTitle('Resolve');
  expect(resolveButtons).toHaveLength(1);

  // The row carries the work-center code and the Unplanned badge.
  expect(within(panel as HTMLElement).getByText('LASER-1')).toBeInTheDocument();
  expect(within(panel as HTMLElement).getByText('Unplanned')).toBeInTheDocument();

  // The Active Downtime footer reflects exactly one active event
  // (CockpitPanel renders the footer as "<count> total").
  expect(within(panel).getByText('1 active total')).toBeInTheDocument();
});

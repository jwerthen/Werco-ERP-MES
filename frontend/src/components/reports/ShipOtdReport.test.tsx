/**
 * ShipOtdReport — the Reports "Ship OTD" tab (Lean Phase 1 / issue #88).
 *
 * With the report resolving, the view must show: the headline OTD (shipped) /
 * OTIF stat strip, the per-customer rollup, the per-WO rows with the on-time /
 * late(+days) / open badges, and the promise-hygiene panel with its count
 * badge. Null percentages (empty denominator) must render "—", never a fake
 * 100. A failed load renders the shared ErrorState with a Retry that refetches.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import ShipOtdReport from './ShipOtdReport';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getShipOtdReport: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const report = {
  period_start: '2026-06-09',
  period_end: '2026-07-09',
  otd_ship_pct: 66.7,
  otif_pct: 50.0,
  rows: [
    {
      work_order_id: 1,
      work_order_number: 'WO-1001',
      customer_name: 'Acme',
      part_number: 'PN-7',
      status: 'complete',
      quantity_ordered: 10,
      quantity_shipped: 10,
      promise_source: 'due_date',
      promise_date: '2026-06-20',
      first_ship_date: '2026-06-15',
      last_ship_date: '2026-06-18',
      full_ship_date: '2026-06-18',
      fully_shipped: true,
      on_time: true,
      days_late: -2,
    },
    {
      work_order_id: 2,
      work_order_number: 'WO-1002',
      customer_name: 'Acme',
      part_number: 'PN-8',
      status: 'complete',
      quantity_ordered: 5,
      quantity_shipped: 5,
      promise_source: 'must_ship_by',
      promise_date: '2026-06-10',
      first_ship_date: '2026-06-13',
      last_ship_date: '2026-06-13',
      full_ship_date: '2026-06-13',
      fully_shipped: true,
      on_time: false,
      days_late: 3,
    },
    {
      work_order_id: 3,
      work_order_number: 'WO-1003',
      customer_name: 'Beta Corp',
      part_number: 'PN-9',
      status: 'in_progress',
      quantity_ordered: 8,
      quantity_shipped: 2,
      promise_source: 'due_date',
      promise_date: '2026-07-20',
      first_ship_date: '2026-07-01',
      last_ship_date: '2026-07-01',
      full_ship_date: null,
      fully_shipped: false,
      on_time: null,
      days_late: null,
    },
  ],
  by_customer: [
    {
      customer_name: 'Acme',
      work_orders: 2,
      on_time: 1,
      late: 1,
      otd_pct: 50.0,
      avg_days_late: 3.0,
    },
  ],
  promise_hygiene: [
    {
      work_order_id: 9,
      work_order_number: 'WO-0999',
      customer_name: 'Gamma LLC',
      status: 'released',
      quantity_ordered: 4,
      quantity_shipped: 4,
      last_ship_date: '2026-06-30',
    },
  ],
  generated_at: '2026-07-09T12:00:00Z',
};

function renderReport() {
  return render(
    <MemoryRouter>
      <ShipOtdReport periodDays={30} />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getShipOtdReport.mockResolvedValue(report as any);
});

test('renders the headline stats, customer rollup, WO badges, and promise hygiene', async () => {
  renderReport();

  // Headline strip (requested with the page period).
  expect(await screen.findByText('OTD (shipped)')).toBeInTheDocument();
  expect(mockedApi.getShipOtdReport).toHaveBeenCalledWith({ period: '30d' });
  expect(screen.getByText('66.7%')).toBeInTheDocument();
  expect(screen.getByText('OTIF')).toBeInTheDocument();
  // 50.0% appears on the OTIF tile AND the Acme rollup's OTD cell.
  expect(screen.getAllByText('50.0%').length).toBeGreaterThanOrEqual(2);

  // Customer rollup row (Acme 50% OTD across its 2 measured WOs).
  expect(screen.getAllByText('Acme').length).toBeGreaterThan(0);

  // Per-WO on-time vocabulary: on time, late with days, open while undeterminable.
  expect(screen.getAllByText('WO-1001').length).toBeGreaterThan(0);
  expect(screen.getAllByText('On time').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Late +3d').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Open').length).toBeGreaterThan(0);

  // Promise hygiene: the unmeasurable WO and the amber count badge.
  expect(screen.getByText('Promise Hygiene')).toBeInTheDocument();
  expect(screen.getAllByText('WO-0999').length).toBeGreaterThan(0);
  expect(screen.getByText('Missing Promise')).toBeInTheDocument();
});

test('renders "—" for null percentages (empty denominator), never a fake 100', async () => {
  mockedApi.getShipOtdReport.mockResolvedValue({
    ...report,
    otd_ship_pct: null,
    otif_pct: null,
    rows: [],
    by_customer: [],
    promise_hygiene: [],
  } as any);

  renderReport();

  expect(await screen.findByText('OTD (shipped)')).toBeInTheDocument();
  expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  expect(screen.queryByText('100.0%')).not.toBeInTheDocument();
  // Empty states, not blank sections.
  expect(screen.getByText(/no measurable shipments/i)).toBeInTheDocument();
  expect(screen.getByText(/all work orders carry a promise date/i)).toBeInTheDocument();
});

test('a failed load renders ErrorState and Retry refetches', async () => {
  mockedApi.getShipOtdReport.mockRejectedValueOnce(new Error('boom'));

  renderReport();

  expect(await screen.findByText(/could not load the ship otd report/i)).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: /retry/i }));
  await waitFor(() => expect(mockedApi.getShipOtdReport).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('OTD (shipped)')).toBeInTheDocument();
});

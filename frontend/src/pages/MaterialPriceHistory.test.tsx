import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import MaterialPriceHistory from './MaterialPriceHistory';
import type { MaterialPriceHistoryDetail } from '../types/materialPriceHistory';

// Browser tests exercise the real chart, which requires layout and ResizeObserver.
// These component tests focus on requests, selection, errors and exports.
jest.mock('recharts', () => ({
  ...jest.requireActual('recharts'),
  ResponsiveContainer: () => <div data-testid="price-chart" />,
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaterialPriceHistory: jest.fn(),
    getMaterialPriceHistoryDetail: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const aluminum = {
  part_id: 7,
  part_number: 'AL-5052',
  part_name: 'Aluminum sheet 5052',
  part_type: 'raw_material',
  unit_of_measure: 'sheets',
  currency: null,
  latest_unit_price: 126,
  previous_unit_price: 120,
  price_change: 6,
  price_change_percent: 5,
  last_order_date: '2026-09-10',
  latest_po_id: 102,
  latest_po_number: 'PO-0102',
  latest_vendor_id: 5,
  latest_vendor_name: 'Acme Metals',
  order_count: 2,
  total_quantity: 30,
  total_spend: 3720,
  sparkline: [
    { purchase_order_id: 101, order_date: '2026-08-01', unit_price: 120 },
    { purchase_order_id: 102, order_date: '2026-09-10', unit_price: 126 },
  ],
};

const rivet = {
  ...aluminum,
  part_id: 8,
  part_number: 'HW-RIVET',
  part_name: 'Steel rivet',
  part_type: 'hardware',
  unit_of_measure: 'each',
  latest_unit_price: 0.48,
  previous_unit_price: 0.5,
  price_change: -0.02,
  price_change_percent: -4,
  latest_po_id: 202,
  latest_po_number: 'PO-0202',
};

const overview = {
  items: [aluminum, rivet],
  total: 2,
  page: 1,
  page_size: 25,
  summary: { tracked_parts: 2, price_increases: 1, price_decreases: 1, unchanged_parts: 0, new_parts: 0 },
};

function makeDetail(part = aluminum): MaterialPriceHistoryDetail {
  const history = [
    {
      purchase_order_id: part.latest_po_id,
      po_number: part.latest_po_number,
      order_date: '2026-09-10',
      status: 'sent',
      vendor_id: 5,
      vendor_name: 'Acme Metals',
      quantity_ordered: 20,
      unit_price: part.latest_unit_price,
      extended_price: 20 * part.latest_unit_price,
      line_count: 2,
      previous_unit_price: part.previous_unit_price,
      price_change: part.price_change,
      price_change_percent: part.price_change_percent,
      unit_of_measure: part.unit_of_measure,
      currency: null,
    },
    {
      purchase_order_id: part.latest_po_id - 1,
      po_number: `PO-0${part.latest_po_id - 1}`,
      order_date: '2026-08-01',
      status: 'closed',
      vendor_id: 6,
      vendor_name: 'Bravo Supply',
      quantity_ordered: 10,
      unit_price: part.previous_unit_price,
      extended_price: 10 * part.previous_unit_price,
      line_count: 1,
      previous_unit_price: null,
      price_change: null,
      price_change_percent: null,
      unit_of_measure: part.unit_of_measure,
      currency: null,
    },
  ];
  return {
    part,
    history,
    total: 2,
    page: 1,
    page_size: 50,
    stats: {
      latest_unit_price: part.latest_unit_price,
      previous_unit_price: part.previous_unit_price,
      price_change: part.price_change,
      price_change_percent: part.price_change_percent,
      lowest_unit_price: Math.min(part.latest_unit_price, part.previous_unit_price),
      highest_unit_price: Math.max(part.latest_unit_price, part.previous_unit_price),
      weighted_average_unit_price: (20 * part.latest_unit_price + 10 * part.previous_unit_price) / 30,
      total_quantity: 30,
      total_spend: 20 * part.latest_unit_price + 10 * part.previous_unit_price,
      order_count: 2,
    },
    chart: [...history].reverse().map(row => ({
      purchase_order_id: row.purchase_order_id,
      order_date: row.order_date,
      unit_price: row.unit_price,
      vendor_name: row.vendor_name,
      quantity_ordered: row.quantity_ordered,
      po_number: row.po_number,
    })),
    chart_truncated: false,
    vendor_options: [
      { id: 5, name: 'Acme Metals' },
      { id: 6, name: 'Bravo Supply' },
    ],
    notes: ['Unit prices exclude tax and shipping.'],
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-search">{location.search}</div>;
}

function renderAt(url = '/purchasing/price-history') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <MaterialPriceHistory />
      <LocationProbe />
    </MemoryRouter>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  jest.resetAllMocks();
  mockedApi.getMaterialPriceHistory.mockResolvedValue(overview);
  mockedApi.getMaterialPriceHistoryDetail.mockImplementation(async id => makeDetail(id === 8 ? rivet : aluminum));
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

test('shows PO-to-PO history with links to the source purchase orders', async () => {
  renderAt();

  expect(await screen.findByRole('heading', { level: 1, name: /material price history/i })).toBeInTheDocument();
  const latestPurchase = await screen.findByRole('link', { name: /PO-0102/ });
  expect(latestPurchase).toHaveAttribute('href', '/purchasing?po=102');
  expect(screen.getByRole('link', { name: /PO-0101/ })).toHaveAttribute('href', '/purchasing?po=101');
  expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledWith(7, expect.any(Object));
  expect(screen.getByLabelText('Supplier')).toHaveValue('');
});

test('opens an incoming part deep link even when the item is outside the inventory page', async () => {
  mockedApi.getMaterialPriceHistory.mockResolvedValue({ ...overview, items: [aluminum] });
  renderAt('/purchasing/price-history?part=8');

  expect(await screen.findByRole('link', { name: /PO-0202/ })).toBeInTheDocument();
  expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledWith(8, expect.any(Object));
  expect(mockedApi.getMaterialPriceHistoryDetail.mock.calls.every(([id]) => id === 8)).toBe(true);
});

test('selecting inventory updates the URL and shows that item’s purchases', async () => {
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.click(screen.getByRole('button', { name: /HW-RIVET/ }));

  expect(await screen.findByRole('link', { name: /PO-0202/ })).toBeInTheDocument();
  expect(screen.getByTestId('current-search')).toHaveTextContent('part=8');
  expect(screen.queryByRole('link', { name: /PO-0102/ })).not.toBeInTheDocument();
});

test('supplier and period filters request a new, scoped purchase history', async () => {
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Supplier'), { target: { value: '6' } });
  await waitFor(() =>
    expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenLastCalledWith(
      7,
      expect.objectContaining({ vendor_id: 6, page: 1 })
    )
  );
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Purchase period'), { target: { value: '90d' } });
  await waitFor(() =>
    expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenLastCalledWith(
      7,
      expect.objectContaining({ vendor_id: 6, start_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/), page: 1 })
    )
  );
});

test('filter controls stay available while a refreshed history hides the previous costs', async () => {
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  const pending = deferred<MaterialPriceHistoryDetail>();
  mockedApi.getMaterialPriceHistoryDetail.mockReturnValueOnce(pending.promise);
  fireEvent.change(screen.getByLabelText('Supplier'), { target: { value: '6' } });

  expect(screen.getByLabelText('Supplier')).toHaveValue('6');
  expect(screen.getByLabelText('Purchase period')).toBeEnabled();
  expect(screen.queryByRole('link', { name: /PO-0102/ })).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent(/loading/i);
  await act(async () => {
    pending.resolve(makeDetail());
  });
  expect(screen.getByRole('link', { name: /PO-0102/ })).toBeInTheDocument();
});

test('CSV export fetches every page in the selected scope and protects spreadsheet formulas', async () => {
  const source = makeDetail();
  const rows = Array.from({ length: 101 }, (_, index) => ({
    ...source.history[0],
    purchase_order_id: 5000 + index,
    po_number: `EXPORT-${index + 1}`,
    vendor_name: index === 0 ? '=HYPERLINK("https://example.test","Supplier")' : 'Bravo Supply',
  }));
  mockedApi.getMaterialPriceHistoryDetail.mockImplementation(async (_id, params) => {
    if (params?.page_size !== 100) return source;
    const start = ((params.page ?? 1) - 1) * 100;
    return { ...source, history: rows.slice(start, start + 100), total: rows.length };
  });
  const createUrl = jest.spyOn(URL, 'createObjectURL').mockReturnValue('blob:price-history-test');
  const anchorClick = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Supplier'), { target: { value: '6' } });
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Purchase period'), { target: { value: '90d' } });
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));

  await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(1));
  const exportCalls = mockedApi.getMaterialPriceHistoryDetail.mock.calls.filter(
    ([, params]) => params?.page_size === 100
  );
  expect(exportCalls).toEqual([
    [
      7,
      expect.objectContaining({
        vendor_id: 6,
        start_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
        page: 1,
        page_size: 100,
      }),
    ],
    [
      7,
      expect.objectContaining({
        vendor_id: 6,
        start_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
        page: 2,
        page_size: 100,
      }),
    ],
  ]);
  const csv = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(createUrl.mock.calls[0][0] as Blob);
  });
  expect(csv.split('\r\n')).toHaveLength(102);
  expect(csv).toContain('"EXPORT-1"');
  expect(csv).toContain('"EXPORT-101"');
  expect(csv).toContain('"\'=HYPERLINK(""https://example.test"",""Supplier"")"');
  expect(anchorClick).toHaveBeenCalledTimes(1);
});

test('inventory search is debounced and type changes fetch immediately', async () => {
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  const initialCalls = mockedApi.getMaterialPriceHistory.mock.calls.length;
  jest.useFakeTimers();
  fireEvent.change(screen.getByLabelText('Search inventory'), { target: { value: 'rivet' } });
  expect(mockedApi.getMaterialPriceHistory).toHaveBeenCalledTimes(initialCalls);
  await act(async () => {
    jest.advanceTimersByTime(300);
  });
  expect(mockedApi.getMaterialPriceHistory).toHaveBeenLastCalledWith(
    expect.objectContaining({ search: 'rivet', page: 1 })
  );

  fireEvent.change(screen.getByLabelText('Inventory type'), { target: { value: 'hardware' } });
  expect(mockedApi.getMaterialPriceHistory).toHaveBeenLastCalledWith(
    expect.objectContaining({ search: 'rivet', part_type: 'hardware', page: 1 })
  );
  await act(async () => {});
});

test('a failed inventory read offers a working retry', async () => {
  mockedApi.getMaterialPriceHistory.mockRejectedValueOnce(new Error('Network unavailable'));
  renderAt();
  const alert = await screen.findByRole('alert');
  fireEvent.click(within(alert).getByRole('button', { name: /retry/i }));

  expect(await screen.findByRole('link', { name: /PO-0102/ })).toBeInTheDocument();
  expect(mockedApi.getMaterialPriceHistory).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('a failed detail read can recover without reloading the inventory list', async () => {
  mockedApi.getMaterialPriceHistoryDetail.mockRejectedValueOnce(new Error('Network unavailable'));
  renderAt();
  const alert = await screen.findByRole('alert');
  fireEvent.click(within(alert).getByRole('button', { name: /retry/i }));

  expect(await screen.findByRole('link', { name: /PO-0102/ })).toBeInTheDocument();
  expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledTimes(2);
  expect(mockedApi.getMaterialPriceHistory).toHaveBeenCalledTimes(1);
});

test('an item without purchase history shows a normal empty state and lets the buyer select another item', async () => {
  mockedApi.getMaterialPriceHistoryDetail.mockRejectedValueOnce({ response: { status: 404 } });
  renderAt('/purchasing/price-history?part=99');

  expect(await screen.findByText('No purchase history available')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /HW-RIVET/ }));

  expect(await screen.findByRole('link', { name: /PO-0202/ })).toBeInTheDocument();
  expect(screen.queryByText('No purchase history available')).not.toBeInTheDocument();
});

test('purchase pagination retains filters and changing supplier returns to the first page', async () => {
  mockedApi.getMaterialPriceHistoryDetail.mockResolvedValue({ ...makeDetail(), total: 45 });
  renderAt();
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Supplier'), { target: { value: '6' } });
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.click(screen.getByRole('button', { name: 'Next purchases' }));
  await waitFor(() =>
    expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenLastCalledWith(
      7,
      expect.objectContaining({ vendor_id: 6, page: 2 })
    )
  );
  await screen.findByRole('link', { name: /PO-0102/ });
  fireEvent.change(screen.getByLabelText('Supplier'), { target: { value: '5' } });
  await waitFor(() =>
    expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenLastCalledWith(
      7,
      expect.objectContaining({ vendor_id: 5, page: 1 })
    )
  );
});

test('a slower old item response cannot overwrite the currently selected history', async () => {
  const oldRequest = deferred<ReturnType<typeof makeDetail>>();
  mockedApi.getMaterialPriceHistoryDetail.mockReturnValueOnce(oldRequest.promise);
  renderAt();
  await waitFor(() => expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledWith(7, expect.any(Object)));
  fireEvent.click(screen.getByRole('button', { name: /HW-RIVET/ }));
  await screen.findByRole('link', { name: /PO-0202/ });

  await act(async () => {
    oldRequest.resolve(makeDetail());
  });
  expect(screen.getByRole('link', { name: /PO-0202/ })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: /PO-0102/ })).not.toBeInTheDocument();
});

test('a superseded failure neither shows an error nor ends the newer detail load', async () => {
  const oldRequest = deferred<ReturnType<typeof makeDetail>>();
  const newRequest = deferred<ReturnType<typeof makeDetail>>();
  mockedApi.getMaterialPriceHistoryDetail
    .mockReturnValueOnce(oldRequest.promise)
    .mockReturnValueOnce(newRequest.promise);
  renderAt();
  await waitFor(() => expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledWith(7, expect.any(Object)));
  fireEvent.click(screen.getByRole('button', { name: /HW-RIVET/ }));
  await waitFor(() => expect(mockedApi.getMaterialPriceHistoryDetail).toHaveBeenCalledWith(8, expect.any(Object)));
  await act(async () => {
    oldRequest.reject(new Error('Old request failed'));
  });

  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: /PO-0102|PO-0202/ })).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toBeInTheDocument();
  await act(async () => {
    newRequest.resolve(makeDetail(rivet));
  });
  expect(screen.getByRole('link', { name: /PO-0202/ })).toBeInTheDocument();
});

test('a zero previous price never renders a misleading infinite percentage', async () => {
  const detail = makeDetail();
  detail.stats.previous_unit_price = 0;
  detail.stats.price_change = 126;
  detail.stats.price_change_percent = null;
  detail.history[0].previous_unit_price = 0;
  detail.history[0].price_change_percent = null;
  mockedApi.getMaterialPriceHistoryDetail.mockResolvedValue(detail);
  renderAt();

  await screen.findByRole('link', { name: /PO-0102/ });
  expect(screen.queryByText(/Infinity|NaN/)).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: /PO-0102/ }).closest('tr')).not.toHaveTextContent('0.0%');
});

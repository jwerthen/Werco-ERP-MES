/**
 * WorkOrders — the "work order type" quick filter (All types / Nest packages / Production).
 *
 * Locks three contracts:
 *
 *   1. SERVER-SIDE, URL-BORNE. The selected chip rides in the URL as `?type=`
 *      (absent = all types, so the default URL stays clean) and reaches
 *      `GET /work-orders/browse` as `work_order_type`. "All types" removes both.
 *
 *   2. COUNTS COME FROM `type_counts`, which the server computes under every
 *      OTHER filter but never the type filter itself — so selecting a chip does
 *      not change any chip's count, and "All types" is their sum. Counts are
 *      hidden until the first response carries them, never shown as a guessed 0.
 *
 *   3. AN UNKNOWN `?type=` (stale bookmark, hand-edited URL) reads as All and is
 *      never sent — the server's enum would 422 the whole list.
 *
 * Mocks match WorkOrders.urlFilters.test.tsx; responses come from the shared
 * `workOrderBrowseFixture`, which honors `work_order_type` and computes
 * `type_counts` the way the server does (the SQL itself is covered by
 * backend/tests/api/test_work_order_browse.py).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { workOrderBrowseFixture } from '../testUtils/workOrderBrowseFixture';
import { WorkOrderBrowseParams } from '../types/workOrderBrowse';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    browseWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: true },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workOrders = [
  {
    id: 1,
    work_order_number: 'WO-1001',
    part_id: 10,
    work_order_type: 'production',
    part_number: 'PN-AAA',
    part_name: 'Bracket Assembly',
    part_type: 'manufactured',
    status: 'released' as const,
    priority: 2,
    quantity_ordered: 50,
    quantity_complete: 0,
    customer_name: 'Acme Aero',
  },
  {
    id: 2,
    work_order_number: 'WO-1002',
    part_id: 20,
    work_order_type: 'production',
    part_number: 'PN-BBB',
    part_name: 'Housing',
    part_type: 'manufactured',
    status: 'in_progress' as const,
    priority: 3,
    quantity_ordered: 20,
    quantity_complete: 10,
    customer_name: 'Beta Defense',
  },
  {
    // A standalone nest package: part-less, which only laser_cutting allows.
    id: 3,
    work_order_number: 'NEST-3001',
    part_id: null,
    work_order_type: 'laser_cutting',
    part_number: null,
    part_name: null,
    part_type: null,
    status: 'released' as const,
    priority: 3,
    quantity_ordered: 4,
    quantity_complete: 0,
    customer_name: 'Acme Aero',
  },
];

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <WorkOrders />
      <LocationProbe />
    </MemoryRouter>
  );
}

const locationSearch = () => screen.getByTestId('location-search').textContent;
const chip = (label: string) =>
  within(screen.getByRole('group', { name: 'Work order type' })).getByRole('button', {
    name: new RegExp(`^${label}`),
  });
/** The count badge inside a chip, or null while counts are hidden. */
const chipCount = (label: string) => within(chip(label)).queryByText(/^\d+$/)?.textContent ?? null;
const allCounts = () => ['All types', 'Nest packages', 'Production'].map(chipCount);
const pressed = () =>
  ['All types', 'Nest packages', 'Production'].filter(label => chip(label).getAttribute('aria-pressed') === 'true');
const browseCalls = () => mockedApi.browseWorkOrders.mock.calls.map(([params]) => params);
const lastBrowseParams = (): WorkOrderBrowseParams | undefined => browseCalls()[browseCalls().length - 1];

/** Wait for a WO row link (table and/or mobile card) to render. */
async function waitForRow(number: string) {
  expect((await screen.findAllByRole('link', { name: number })).length).toBeGreaterThan(0);
}

describe('WorkOrders — work order type quick filter', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.browseWorkOrders.mockImplementation(async params => workOrderBrowseFixture(workOrders, params));
  });

  it('renders the three chips with All pressed, and shows server counts only once a response carries them', async () => {
    let release!: () => void;
    const gate = new Promise<void>(resolve => {
      release = resolve;
    });
    mockedApi.browseWorkOrders.mockImplementation(async params => {
      await gate;
      return workOrderBrowseFixture(workOrders, params);
    });
    renderAt('/work-orders');

    expect(pressed()).toEqual(['All types']);
    // Request in flight: no badge at all, rather than a misleading 0.
    expect(allCounts()).toEqual([null, null, null]);

    await act(async () => release());
    await waitForRow('WO-1001');
    // All = sum of type_counts; each chip shows its own key.
    expect(allCounts()).toEqual(['3', '1', '2']);
    expect(lastBrowseParams()?.work_order_type).toBeUndefined();
  });

  it('clicking "Nest packages" writes ?type=laser_cutting, presses the chip and narrows the server request', async () => {
    renderAt('/work-orders');
    await waitForRow('WO-1001');
    expect(locationSearch()).toBe('');

    fireEvent.click(chip('Nest packages'));
    expect(locationSearch()).toBe('?type=laser_cutting');
    expect(pressed()).toEqual(['Nest packages']);
    await waitFor(() =>
      expect(lastBrowseParams()).toEqual(expect.objectContaining({ work_order_type: 'laser_cutting', skip: 0 }))
    );
    await waitForRow('NEST-3001');
    expect(screen.queryAllByRole('link', { name: 'WO-1001' })).toHaveLength(0);
    expect(screen.queryAllByRole('link', { name: 'WO-1002' })).toHaveLength(0);
    // type_counts ignore the type filter, so selecting a chip changes no count.
    expect(allCounts()).toEqual(['3', '1', '2']);
  });

  it('arriving at ?type=laser_cutting pre-selects the chip and the very first request carries it', async () => {
    renderAt('/work-orders?type=laser_cutting');
    expect(pressed()).toEqual(['Nest packages']);

    await waitForRow('NEST-3001');
    expect(browseCalls()[0]).toEqual(expect.objectContaining({ work_order_type: 'laser_cutting' }));
    expect(browseCalls().every(params => params?.work_order_type === 'laser_cutting')).toBe(true);
    expect(screen.queryAllByRole('link', { name: 'WO-1001' })).toHaveLength(0);
  });

  it('"Production" swaps the type in place and "All types" deletes it, keeping the other filters', async () => {
    renderAt('/work-orders?type=laser_cutting&customer=Acme+Aero');
    await waitForRow('NEST-3001');

    fireEvent.click(chip('Production'));
    expect(locationSearch()).toBe('?type=production&customer=Acme+Aero');
    expect(pressed()).toEqual(['Production']);
    await waitFor(() =>
      expect(lastBrowseParams()).toEqual(
        expect.objectContaining({ work_order_type: 'production', customer: 'Acme Aero' })
      )
    );
    await waitForRow('WO-1001');
    expect(screen.queryAllByRole('link', { name: 'NEST-3001' })).toHaveLength(0);

    const before = browseCalls().length;
    fireEvent.click(chip('All types'));
    expect(locationSearch()).toBe('?customer=Acme+Aero');
    expect(pressed()).toEqual(['All types']);
    await waitFor(() => expect(browseCalls().length).toBeGreaterThan(before));
    expect(lastBrowseParams()).toEqual(expect.objectContaining({ customer: 'Acme Aero' }));
    expect(lastBrowseParams()?.work_order_type).toBeUndefined();
    await waitForRow('NEST-3001');
    await waitForRow('WO-1001');
    // Counts still honor the customer filter (WO-1002 is Beta): 1 + 1.
    expect(allCounts()).toEqual(['2', '1', '1']);
  });

  it('an unknown ?type= is ignored: All is pressed and no work_order_type ever reaches the server', async () => {
    renderAt('/work-orders?type=bogus');
    await waitForRow('WO-1001');
    await waitForRow('NEST-3001');

    expect(pressed()).toEqual(['All types']);
    expect(browseCalls().length).toBeGreaterThan(0);
    expect(browseCalls().map(params => params?.work_order_type)).toEqual(browseCalls().map(() => undefined));

    // Choosing a real type replaces the bad value.
    fireEvent.click(chip('Nest packages'));
    expect(locationSearch()).toBe('?type=laser_cutting');
    await waitFor(() => expect(lastBrowseParams()?.work_order_type).toBe('laser_cutting'));
  });
});

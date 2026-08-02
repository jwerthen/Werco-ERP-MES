/**
 * WorkOrders — URL-param filters, shared debounced search, grouped CSV export.
 *
 * Locks the three contracts of the filters-in-URL change:
 *
 *   1. URL ROUND-TRIP. Structured filters (status / customer / cots / group)
 *      read from and write to search params (the ProcessSheets idiom):
 *      arriving with `?status=in_progress` pre-selects the status filter,
 *      changing a select writes the param, and DEFAULT state keeps the URL
 *      clean (absent param = default — hideCOTS ON, no grouping), so existing
 *      bookmarks and the landing URL never grow params.
 *
 *   2. DEBOUNCED SEARCH. The free-text search stays LOCAL state, debounced via
 *      the shared useDebouncedValue(search, 250) — no refetch fires until the
 *      250ms window closes (the hand-rolled setTimeout state pair is gone).
 *
 *   3. GROUPED CSV EXPORT. The grouped view renders one DataTable per group,
 *      each with its own Export CSV control whose filename is
 *      `work-orders-<group>` with the group name sanitized (lowercase,
 *      non-alphanumeric runs → single dashes) and whose file contains ONLY
 *      that group's rows — the flat view previously had csvExport while
 *      groups had none.
 *
 * Desktop table + mobile cards BOTH mount in jsdom (CSS visibility classes
 * don't prune the DOM); queries scope accordingly.
 */

import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
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
    status: 'draft' as const,
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
    // Punctuation + spaces exercise the filename sanitizer.
    customer_name: 'Beta & Defense, Inc.',
  },
];

/** Exposes the live URL search string so param round-trips are assertable. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

/** History probe: a real Back control, since jsdom has no browser chrome. */
function BackButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      test-back
    </button>
  );
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <WorkOrders />
      <LocationProbe />
      <BackButton />
    </MemoryRouter>
  );
}

const locationSearch = () => screen.getByTestId('location-search').textContent;

/** jsdom's Blob has no .text(); read exported CSV content via FileReader. */
const readBlobText = (blob: Blob) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });

/** Wait for the loaded list (the WO link renders in table + mobile card). */
async function waitForLoadedList() {
  await screen.findAllByRole('link', { name: 'WO-1001' });
}

describe('WorkOrders — filters round-trip through URL params', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkOrders.mockResolvedValue(workOrders);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('reflects an incoming ?status=in_progress in the status select and the fetch', async () => {
    renderAt('/work-orders?status=in_progress');
    await waitForLoadedList();

    expect(screen.getByLabelText('Status filter')).toHaveValue('in_progress');
    expect(mockedApi.getWorkOrders).toHaveBeenCalledWith({ status: 'in_progress' });
  });

  it('writes select changes to the URL and clears the param back to a clean URL', async () => {
    renderAt('/work-orders');
    await waitForLoadedList();

    // Default state → clean URL.
    expect(locationSearch()).toBe('');

    fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'released' } });
    expect(locationSearch()).toBe('?status=released');
    await waitFor(() =>
      expect(mockedApi.getWorkOrders).toHaveBeenLastCalledWith({ status: 'released' })
    );

    // Back to the default ("All Active") deletes the param entirely.
    fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: '' } });
    expect(locationSearch()).toBe('');
  });

  it('round-trips customer, COTS toggle, and grouping — params only when non-default', async () => {
    renderAt('/work-orders');
    await waitForLoadedList();

    // Customer filter (options are derived from the loaded rows).
    fireEvent.change(screen.getByLabelText('Customer filter'), { target: { value: 'Acme Aero' } });
    expect(locationSearch()).toBe('?customer=Acme+Aero');
    fireEvent.change(screen.getByLabelText('Customer filter'), { target: { value: '' } });
    expect(locationSearch()).toBe('');

    // hideCOTS defaults ON → the param appears only when SHOWING COTS.
    const cots = screen.getByLabelText('Hide COTS/Hardware') as HTMLInputElement;
    expect(cots.checked).toBe(true);
    fireEvent.click(cots);
    expect(locationSearch()).toBe('?cots=1');
    fireEvent.click(cots);
    expect(locationSearch()).toBe('');

    // groupBy defaults none → the param appears only when grouping.
    fireEvent.change(screen.getByLabelText('Group work orders'), { target: { value: 'customer' } });
    expect(locationSearch()).toBe('?group=customer');
    fireEvent.change(screen.getByLabelText('Group work orders'), { target: { value: 'none' } });
    expect(locationSearch()).toBe('');
  });

  it('keeps a URL-borne customer selected even when no loaded row carries it', async () => {
    // The customer options are derived from the LOADED rows, so a shared URL can
    // name a customer with no current work orders. The filter must still apply
    // AND the select must still show it — without the injected option the select
    // renders blank while the filter quietly hides every row.
    renderAt('/work-orders?customer=Vanished+Corp');

    const select = await screen.findByLabelText('Customer filter');
    expect(select).toHaveValue('Vanished Corp');
    // Every row is filtered out (neither loaded customer matches).
    expect(screen.queryByRole('link', { name: 'WO-1001' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'WO-1002' })).not.toBeInTheDocument();
  });

  it('applies an incoming ?cots=1 (show COTS) and ?group=status to the controls', async () => {
    renderAt('/work-orders?cots=1&group=status');
    await waitForLoadedList();

    expect((screen.getByLabelText('Hide COTS/Hardware') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByLabelText('Group work orders')).toHaveValue('status');
    // Grouped view is active: group headers render (one per status).
    expect(screen.getByRole('heading', { name: 'draft' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'in progress' })).toBeInTheDocument();
  });

  it('applies a combined bookmark — status + customer + group in one URL', async () => {
    const qs = new URLSearchParams({
      status: 'in_progress',
      customer: 'Beta & Defense, Inc.',
      group: 'part',
    }).toString();
    renderAt(`/work-orders?${qs}`);

    // All three controls reflect the URL...
    expect(await screen.findByLabelText('Status filter')).toHaveValue('in_progress');
    expect(screen.getByLabelText('Customer filter')).toHaveValue('Beta & Defense, Inc.');
    expect(screen.getByLabelText('Group work orders')).toHaveValue('part');
    // ...the fetch carried the status...
    expect(mockedApi.getWorkOrders).toHaveBeenCalledWith({ status: 'in_progress' });
    // ...and only the matching row renders, grouped under its part.
    expect(await screen.findByRole('heading', { name: 'PN-BBB' })).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'WO-1002' }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('heading', { name: 'PN-AAA' })).not.toBeInTheDocument();
    expect(screen.queryAllByRole('link', { name: 'WO-1001' })).toHaveLength(0);
  });

  it('browser Back reverts both the controls and the visible rows', async () => {
    renderAt('/work-orders');
    await waitForLoadedList();

    // Two filter steps → two history entries.
    fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'in_progress' } });
    expect(locationSearch()).toBe('?status=in_progress');
    fireEvent.change(screen.getByLabelText('Customer filter'), { target: { value: 'Acme Aero' } });
    expect(locationSearch()).toBe('?status=in_progress&customer=Acme+Aero');

    // The client-side customer filter hides the other customer's rows.
    await waitFor(() => expect(screen.queryAllByRole('link', { name: 'WO-1002' })).toHaveLength(0));
    expect(screen.getAllByRole('link', { name: 'WO-1001' }).length).toBeGreaterThan(0);

    // Back: the URL is the single source of truth, so BOTH the select and the
    // rows revert — a mount-read useState copy of the params would keep
    // filtering by customer while the URL said otherwise.
    fireEvent.click(screen.getByRole('button', { name: 'test-back' }));
    expect(locationSearch()).toBe('?status=in_progress');
    expect(screen.getByLabelText('Customer filter')).toHaveValue('');
    expect(screen.getByLabelText('Status filter')).toHaveValue('in_progress');
    await waitFor(() =>
      expect(screen.getAllByRole('link', { name: 'WO-1002' }).length).toBeGreaterThan(0)
    );

    // A second Back lands on the pristine entry: clean URL, default controls.
    fireEvent.click(screen.getByRole('button', { name: 'test-back' }));
    expect(locationSearch()).toBe('');
    expect(screen.getByLabelText('Status filter')).toHaveValue('');
  });
});

describe('WorkOrders — search debounced through the shared hook', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkOrders.mockResolvedValue(workOrders);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('does not refetch while typing; a single fetch fires 250ms after the last keystroke', async () => {
    renderAt('/work-orders');
    await waitForLoadedList();
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);

    jest.useFakeTimers();
    const input = screen.getByLabelText('Search work orders') as HTMLInputElement;

    // Keystrokes update the input immediately but trigger no fetch.
    fireEvent.change(input, { target: { value: 'WO-1' } });
    expect(input.value).toBe('WO-1');
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);

    // Part-way through the window, keep typing — the timer resets, still no fetch.
    act(() => {
      jest.advanceTimersByTime(200);
    });
    fireEvent.change(input, { target: { value: 'WO-1002' } });
    act(() => {
      jest.advanceTimersByTime(249);
    });
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);

    // Crossing 250ms idle fires exactly ONE fetch, with the FINAL query.
    await act(async () => {
      jest.advanceTimersByTime(1);
    });
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2);
    expect(mockedApi.getWorkOrders).toHaveBeenLastCalledWith({ search: 'WO-1002' });
  });
});

describe('WorkOrders — grouped view exports CSV per group', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkOrders.mockResolvedValue(workOrders);
  });

  it('renders an Export CSV control per group with the sanitized filename and ONLY that group\'s rows', async () => {
    const downloads: string[] = [];
    const blobs: Blob[] = [];
    const createSpy = jest
      .spyOn(URL, 'createObjectURL')
      .mockImplementation((obj: Blob | MediaSource) => {
        blobs.push(obj as Blob);
        return 'blob:mock';
      });
    const revokeSpy = jest.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const clickSpy = jest
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloads.push(this.download);
      });

    renderAt('/work-orders?group=customer');
    await waitForLoadedList();

    // One group card per customer (sorted), each with its own export control.
    expect(screen.getByRole('heading', { name: 'Acme Aero' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Beta & Defense, Inc.' })).toBeInTheDocument();
    const exportButtons = screen.getAllByRole('button', { name: /Export CSV/i });
    expect(exportButtons).toHaveLength(2);

    // Each button downloads under the sanitized `work-orders-<group>` filename:
    // lowercase, runs of non-alphanumerics collapsed to single dashes.
    fireEvent.click(exportButtons[0]);
    fireEvent.click(exportButtons[1]);
    expect(downloads).toEqual(['work-orders-acme-aero.csv', 'work-orders-beta-defense-inc.csv']);

    // And each file carries ONLY its own group's rows — matching filenames
    // alone would tolerate every group exporting the full flat list.
    expect(blobs).toHaveLength(2);
    const acmeCsv = await readBlobText(blobs[0]);
    const betaCsv = await readBlobText(blobs[1]);
    expect(acmeCsv).toContain('WO-1001');
    expect(acmeCsv).not.toContain('WO-1002');
    expect(betaCsv).toContain('WO-1002');
    expect(betaCsv).not.toContain('WO-1001');

    createSpy.mockRestore();
    revokeSpy.mockRestore();
    clickSpy.mockRestore();
  });
});

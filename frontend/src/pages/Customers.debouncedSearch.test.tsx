/**
 * Batch 10 — perceived performance: Customers debounced search.
 *
 * The customer search input stays responsive (its value reflects every keystroke
 * immediately), but the list FILTER is keyed off useDebouncedValue(search, 250),
 * not the raw input. This locks both halves of that contract:
 *
 *   1. RESPONSIVE INPUT — typing updates the visible input value synchronously,
 *      before any debounce window elapses.
 *   2. DEBOUNCED FILTER — the rendered rows do NOT re-filter on each keystroke.
 *      Typing several characters and only then advancing the fake timer past the
 *      delay produces exactly ONE filter transition to the FINAL query: an
 *      intermediate query that matches a different row never gets to filter the
 *      list (the changes are coalesced).
 *
 * Customers filters client-side off the already-loaded list (no per-keystroke
 * refetch), so api.getCustomers is called once at mount and never again from
 * typing — also asserted, since a debounce that leaked would re-run the filter,
 * and a non-debounced search-to-server would re-fetch.
 *
 * The customer rows render in BOTH the desktop <table> and the md:hidden mobile
 * cards; row assertions are scoped to the desktop DataTable (data-testid).
 */

import React from 'react';
import { render, screen, within, fireEvent, act, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Customers from './Customers';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getCustomers: jest.fn(),
    getCustomerStats: jest.fn(),
    createCustomer: jest.fn(),
    updateCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const customers = [
  {
    id: 1, name: 'Apex Mfg', code: 'CUST-A', contact_name: 'Ana Diaz', email: 'ana@apex.test',
    city: 'Reno', state: 'NV', payment_terms: 'Net 45', requires_coc: false, requires_fai: true,
    is_active: true, created_at: '2026-01-02T00:00:00Z',
  },
  {
    id: 2, name: 'Beacon Aero', code: 'CUST-B', contact_name: 'Bo Reed', email: 'bo@beacon.test',
    city: 'Mesa', state: 'AZ', payment_terms: 'Net 30', requires_coc: true, requires_fai: false,
    is_active: true, created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 3, name: 'Crest Systems', code: 'CUST-C', contact_name: 'Cal Howe', email: 'cal@crest.test',
    city: 'Tempe', state: 'AZ', payment_terms: 'Net 15', requires_coc: true, requires_fai: true,
    is_active: true, created_at: '2026-01-03T00:00:00Z',
  },
];

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/customers']}>
      <Customers />
    </MemoryRouter>
  );
}

function desktopRowNames(): string[] {
  const table = screen.getByTestId('data-table');
  return within(table)
    .getAllByRole('row')
    .slice(1) // drop header
    .map((r) => within(r).getAllByRole('cell')[1]?.textContent?.trim() || '')
    .filter(Boolean);
}

describe('Customers — debounced search (Batch 10)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getCustomers.mockResolvedValue(customers as any);
    mockedApi.getCustomerStats.mockResolvedValue({} as any);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('keeps the input responsive while coalescing keystrokes into ONE debounced filter', async () => {
    renderPage();
    // Flush the mount load (real microtask) before switching to fake timers.
    await screen.findAllByText('Apex Mfg');
    expect(mockedApi.getCustomers).toHaveBeenCalledTimes(1);

    // All three rows present pre-search.
    expect(desktopRowNames()).toEqual(['Apex Mfg', 'Beacon Aero', 'Crest Systems']);

    jest.useFakeTimers();
    // The search box lives above the table, not inside it.
    const input = screen.getByPlaceholderText('Search customers...') as HTMLInputElement;

    // Type "B" (would match only Beacon), then quickly correct to "Crest".
    fireEvent.change(input, { target: { value: 'B' } });
    // Input value is responsive — reflects the keystroke immediately.
    expect(input.value).toBe('B');
    // …but the list has NOT re-filtered yet (debounce window still open).
    expect(desktopRowNames()).toEqual(['Apex Mfg', 'Beacon Aero', 'Crest Systems']);

    // Advance partway — still within the 250ms window — and keep typing toward the
    // final query. Each change resets the timer, so no intermediate filter fires.
    act(() => {
      jest.advanceTimersByTime(100);
    });
    fireEvent.change(input, { target: { value: 'Crest' } });
    expect(input.value).toBe('Crest');
    // The interim "B" query never reached the filter — the full list is still shown,
    // and crucially the list was never narrowed to Beacon.
    expect(desktopRowNames()).toEqual(['Apex Mfg', 'Beacon Aero', 'Crest Systems']);

    // Cross the debounce boundary once → a SINGLE transition to the final query.
    act(() => {
      jest.advanceTimersByTime(250);
    });
    expect(desktopRowNames()).toEqual(['Crest Systems']);

    // Client-side filter — no refetch was triggered by typing.
    expect(mockedApi.getCustomers).toHaveBeenCalledTimes(1);
  });

  it('does not narrow the list until the debounce elapses for a single query', async () => {
    renderPage();
    await screen.findAllByText('Apex Mfg');

    jest.useFakeTimers();
    const input = screen.getByPlaceholderText('Search customers...') as HTMLInputElement;

    fireEvent.change(input, { target: { value: 'Apex' } });
    // Just under the delay: input updated, list unchanged.
    act(() => {
      jest.advanceTimersByTime(249);
    });
    expect(input.value).toBe('Apex');
    expect(desktopRowNames()).toEqual(['Apex Mfg', 'Beacon Aero', 'Crest Systems']);

    // The final millisecond crosses the boundary and the filter applies.
    act(() => {
      jest.advanceTimersByTime(1);
    });
    await waitFor(() => expect(desktopRowNames()).toEqual(['Apex Mfg']));
  });
});

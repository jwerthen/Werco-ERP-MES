/**
 * Quotes — `?id=<id>` notification deep-link landing.
 *
 * The `quote.expiring` cron (a wired daily 9:00 AM job) used to emit
 * `/quotes/<id>`, which is not a route — every one of those notifications
 * rendered the app's 404 screen. It now emits `/quotes?id=<id>`.
 *
 * `?id=` already selected a quote from the loaded list, but on a MISS it fell
 * straight through with no fetch and no message. That miss is the normal case
 * for this notification: `list_quotes` caps at 100 and excludes CONVERTED and
 * EXPIRED, so a quote clicked after it actually expires is invisible to the
 * list. A silent no-op reads as success, so these tests pin the by-id fallback,
 * that it fires exactly once (the effect depends on `quotes` and the fallback
 * writes `selectedQuote` off the back of it), and that a rejection is surfaced.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuotes: jest.fn(),
    getParts: jest.fn(),
    getQuote: jest.fn(),
    convertQuote: jest.fn(),
    sendQuote: jest.fn(),
    createQuote: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const listedQuote = {
  id: 7,
  quote_number: 'QUO-0007',
  revision: 'A',
  customer_name: 'Acme Aerospace',
  status: 'sent',
  quote_date: '2026-07-01',
  subtotal: 1000,
  total: 1000,
  lines: [],
};

/**
 * The quote a `quote.expiring` link points at once it has actually expired:
 * outside the list window, but `GET /quotes/{id}` returns the SAME
 * QuoteResponse shape, so no field mapping is needed.
 */
const expiredQuote = {
  id: 42,
  quote_number: 'QUO-0042',
  revision: 'B',
  customer_name: 'Beta Turbines',
  status: 'expired',
  quote_date: '2026-05-01',
  valid_until: '2026-06-01',
  subtotal: 500,
  total: 500,
  lines: [],
};

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <Routes>
          <Route path="/quotes" element={<Quotes />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQuotes.mockResolvedValue([listedQuote] as any);
  mockedApi.getParts.mockResolvedValue([] as any);
  mockedApi.getQuote.mockResolvedValue(expiredQuote as any);
});

/** The "Selected quote" panel, which renders from `selectedQuote` alone. */
const selectedPanel = async (): Promise<HTMLElement> => {
  const label = await screen.findByText('Selected quote');
  return label.closest('div.mb-4') as HTMLElement;
};

describe('?id= hits the loaded list', () => {
  test('selects the quote without an extra fetch', async () => {
    renderAt('/quotes?id=7');
    const panel = await selectedPanel();
    expect(within(panel).getByText(/QUO-0007/)).toBeInTheDocument();
    expect(mockedApi.getQuote).not.toHaveBeenCalled();
  });
});

describe('?id= misses the loaded list (EXPIRED / CONVERTED / beyond the 100 cap)', () => {
  test('falls back to a by-id fetch and shows the quote', async () => {
    renderAt('/quotes?id=42');
    await waitFor(() => expect(mockedApi.getQuote).toHaveBeenCalledWith(42));
    const panel = await selectedPanel();
    expect(within(panel).getByText(/QUO-0042/)).toBeInTheDocument();
    expect(within(panel).getByText('Beta Turbines')).toBeInTheDocument();
  });

  test('the fallback fetch fires EXACTLY ONCE', async () => {
    renderAt('/quotes?id=42');
    await waitFor(() => expect(mockedApi.getQuote).toHaveBeenCalledWith(42));
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(mockedApi.getQuote).toHaveBeenCalledTimes(1);
  });

  test('a rejected fetch surfaces an error toast and does not retry', async () => {
    mockedApi.getQuote.mockRejectedValue(new Error('404'));
    renderAt('/quotes?id=42');

    expect(await screen.findByText('Quote not found')).toBeInTheDocument();
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(mockedApi.getQuote).toHaveBeenCalledTimes(1);
  });
});

describe('no ?id= param', () => {
  test('nothing is selected and nothing is fetched by id', async () => {
    renderAt('/quotes');
    await waitFor(() => expect(mockedApi.getQuotes).toHaveBeenCalled());
    expect(screen.queryByText('Selected quote')).not.toBeInTheDocument();
    expect(mockedApi.getQuote).not.toHaveBeenCalled();
  });
});

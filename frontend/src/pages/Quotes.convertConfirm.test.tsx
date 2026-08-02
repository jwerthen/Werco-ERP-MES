/**
 * Quotes — convert-to-work-order confirm (the INFO-variant ConfirmDialog shape).
 *
 * The row's Convert action no longer fires a native window.confirm: it opens
 * the shared ConfirmDialog (variant="info" — a non-destructive go/no-go), and
 * api.convertQuote fires only from the dialog's Convert button, with the
 * in-flight `pending` state guarding a double fire. Cancel closes without any
 * API call. This file pins that pattern once for the info-confirm shape.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuotes: jest.fn(),
    getParts: jest.fn(),
    convertQuote: jest.fn(),
    sendQuote: jest.fn(),
    createQuote: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const sentQuote = {
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

function renderQuotes() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <Quotes />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('Quotes convert-to-work-order confirm (info variant)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getQuotes.mockResolvedValue([sentQuote] as any);
    mockedApi.getParts.mockResolvedValue([] as any);
  });

  it('opens the info confirm dialog and converts only on confirm', async () => {
    mockedApi.convertQuote.mockResolvedValue({ work_order_number: 'WO-2044' } as any);
    renderQuotes();

    // The Convert action renders in both the desktop table and the mobile card
    // in JSDOM — either opens the same dialog.
    const convertActions = await screen.findAllByLabelText('Convert to Work Order');
    fireEvent.click(convertActions[0]);

    // Opening the dialog writes nothing.
    const dialog = await screen.findByRole('dialog');
    expect(mockedApi.convertQuote).not.toHaveBeenCalled();
    expect(within(dialog).getByText('Convert this quote to a work order?')).toBeInTheDocument();

    // Info variant: the confirm button is the primary (blue) chrome, not the
    // red danger or amber warning treatment.
    const confirmButton = within(dialog).getByRole('button', { name: 'Convert' });
    expect(confirmButton.className).toContain('btn-primary');
    expect(confirmButton.className).not.toContain('bg-amber-500');

    fireEvent.click(confirmButton);
    await waitFor(() => {
      expect(mockedApi.convertQuote).toHaveBeenCalledWith(7);
    });

    // Success toast + dialog closes on settle.
    expect(await screen.findByText('Work Order WO-2044 created!')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancel closes the dialog without calling the API', async () => {
    renderQuotes();

    const convertActions = await screen.findAllByLabelText('Convert to Work Order');
    fireEvent.click(convertActions[0]);
    const dialog = await screen.findByRole('dialog');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.convertQuote).not.toHaveBeenCalled();
  });
});

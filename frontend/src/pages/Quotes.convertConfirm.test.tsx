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
    getQuote: jest.fn(),
    getQuoteConversionPlan: jest.fn(),
    getParts: jest.fn(),
    convertQuote: jest.fn(),
    sendQuote: jest.fn(),
    createQuote: jest.fn(),
    updateQuote: jest.fn(),
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
    mockedApi.getQuote.mockResolvedValue(sentQuote as any);
    mockedApi.getQuoteConversionPlan.mockResolvedValue({
      lines: [
        {
          line_id: 12,
          line_number: 2,
          part_id: 3,
          part_number: 'PART-3',
          quantity: 20,
          description: 'Bracket',
          eligible: true,
          outcome: 'Create work order',
        },
      ],
    });
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
    expect(await within(dialog).findByText(/Line 2: PART-3 · Qty 20/)).toBeInTheDocument();

    // Info variant: the confirm button is the primary (blue) chrome, not the
    // red danger or amber warning treatment.
    const confirmButton = within(dialog).getByRole('button', { name: 'Create 1 work order' });
    expect(confirmButton.className).toContain('btn-primary');
    expect(confirmButton.className).not.toContain('bg-amber-500');

    fireEvent.click(confirmButton);
    await waitFor(() => {
      expect(mockedApi.convertQuote).toHaveBeenCalledWith(7, { line_ids: [12], acknowledge_unlinked: false });
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

  it('edits the actual expiry date and preserves line identity and internal costs', async () => {
    const draft = {
      ...sentQuote,
      status: 'draft',
      valid_until: '2026-10-30',
      customer_po: 'PO-FIXTURE',
      lines: [
        {
          id: 91,
          line_number: 1,
          part_id: null,
          description: 'Fixture service',
          quantity: 2,
          unit_price: 20,
          line_total: 40,
          labor_hours: 3,
          material_cost: 9,
          labor_cost: 12,
          notes: 'Keep line notes',
        },
      ],
    };
    mockedApi.getQuotes.mockResolvedValue([draft] as any);
    mockedApi.updateQuote.mockResolvedValue({ ...draft, valid_until: '2026-11-02' } as any);
    renderQuotes();
    fireEvent.click((await screen.findAllByText('QUO-0007'))[0]);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('Valid Until')).toHaveValue('2026-10-30');
    expect(within(dialog).getByLabelText('Customer PO')).toHaveValue('PO-FIXTURE');
    fireEvent.change(within(dialog).getByLabelText('Valid Until'), { target: { value: '2026-11-02' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
    await waitFor(() =>
      expect(mockedApi.updateQuote).toHaveBeenCalledWith(
        7,
        expect.objectContaining({
          valid_until: '2026-11-02',
          customer_po: 'PO-FIXTURE',
          lines: [
            expect.objectContaining({
              id: 91,
              quantity: 2,
              labor_hours: 3,
              material_cost: 9,
              labor_cost: 12,
              notes: 'Keep line notes',
            }),
          ],
        })
      )
    );
  });

  it('keeps fresh production links when the list refresh fails after conversion', async () => {
    mockedApi.getQuotes.mockResolvedValueOnce([sentQuote]).mockRejectedValueOnce(new Error('List offline'));
    mockedApi.convertQuote.mockResolvedValue({ work_order_number: 'WO-FRESH' });
    mockedApi.getQuote.mockResolvedValue({
      ...sentQuote,
      status: 'converted',
      work_order_id: 77,
      lines: [
        {
          id: 12,
          line_number: 2,
          part_id: 3,
          part_number: 'PART-3',
          description: 'Bracket',
          quantity: 20,
          unit_price: 50,
          line_total: 1000,
          work_order_id: 77,
          work_order_number: 'WO-FRESH',
        },
      ],
    });
    renderQuotes();
    fireEvent.click((await screen.findAllByLabelText('Convert to Work Order'))[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Create 1 work order' }));
    expect(await screen.findByRole('link', { name: 'WO-FRESH' })).toHaveAttribute('href', '/work-orders/77');
    expect(screen.queryByLabelText('Convert to Work Order')).not.toBeInTheDocument();
  });

  it('keeps a committed conversion successful when only the detail refresh fails', async () => {
    mockedApi.convertQuote.mockResolvedValue({ work_order_number: 'WO-2044' } as any);
    mockedApi.getQuote.mockRejectedValue(new Error('refresh offline'));
    renderQuotes();
    fireEvent.click((await screen.findAllByLabelText('Convert to Work Order'))[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Create 1 work order' }));
    expect(
      await screen.findByText('Work orders were created. Refresh the quote to see its production links.')
    ).toBeInTheDocument();
    expect(screen.queryByText('Could not convert quote. Review the current lines and retry.')).not.toBeInTheDocument();
    expect(mockedApi.convertQuote).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

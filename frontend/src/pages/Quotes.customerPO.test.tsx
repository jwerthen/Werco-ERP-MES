import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'admin', is_superuser: false } }),
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuotes: jest.fn(),
    getQuote: jest.fn(),
    getParts: jest.fn(),
    updateQuote: jest.fn(),
    getCustomerPOSuggestions: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

it('autocompletes an existing customer PO while editing a quote and preserves it when saving', async () => {
  const draft = {
    id: 7,
    quote_number: 'QUO-0007',
    revision: 'A',
    customer_name: 'Acme Aerospace',
    status: 'draft',
    quote_date: '2026-07-01',
    subtotal: 40,
    total: 40,
    valid_until: '2026-10-30',
    customer_po: '',
    lines: [
      {
        id: 91,
        line_number: 1,
        part_id: null,
        description: 'Fixture service',
        quantity: 2,
        unit_price: 20,
        line_total: 40,
      },
    ],
  };
  mockedApi.getQuotes.mockResolvedValue([draft]);
  mockedApi.getQuote.mockResolvedValue(draft);
  mockedApi.getParts.mockResolvedValue([]);
  mockedApi.updateQuote.mockResolvedValue({ ...draft, customer_po: 'PO/42 #5' });
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({
    items: [{ customer_name: draft.customer_name, customer_po: 'PO/42 #5' }],
    has_more: false,
  });
  render(
    <MemoryRouter>
      <ToastProvider>
        <Quotes />
      </ToastProvider>
    </MemoryRouter>
  );
  fireEvent.click((await screen.findAllByText('QUO-0007'))[0]);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Customer PO' }), { target: { value: 'PO/4' } });
  fireEvent.click(await within(dialog).findByRole('option', { name: 'PO/42 #5 Acme Aerospace' }));
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenCalledWith(
    { search: 'PO/4', customer_name: 'Acme Aerospace', limit: 10 },
    expect.any(AbortSignal)
  );
  expect(mockedApi.updateQuote).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(mockedApi.updateQuote).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ customer_po: 'PO/42 #5', customer_name: 'Acme Aerospace' })
    )
  );
});

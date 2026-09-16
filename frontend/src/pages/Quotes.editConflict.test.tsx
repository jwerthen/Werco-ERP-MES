jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, company_id: 1, role: 'admin', is_superuser: false } }) }));
import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({ __esModule: true, default: {
  getQuotes: jest.fn(), getParts: jest.fn(), getQuote: jest.fn(), updateQuote: jest.fn(),
} }));
const mockedApi = api as jest.Mocked<typeof api>;

it('keeps entered draft values after a stale snapshot conflict and sends the reviewed timestamp', async () => {
  const quote = {
    id: 7, quote_number: 'QUO-0007', revision: 'A', customer_name: 'Acme', status: 'draft',
    quote_date: '2026-09-06', updated_at: '2026-09-06T12:00:00.123456', subtotal: 40, total: 40,
    notes: 'Initial notes', lines: [{ id: 12, line_number: 1, description: 'Fixture', quantity: 4, unit_price: 10, line_total: 40 }],
  };
  mockedApi.getQuotes.mockResolvedValue([quote]);
  mockedApi.getParts.mockResolvedValue([]);
  const detail = 'Quote changed since this editor was opened. Your entries are kept; reopen the latest quote before applying them.';
  mockedApi.updateQuote.mockRejectedValue({ response: { status: 409, data: { detail } } });
  render(<MemoryRouter initialEntries={['/quotes?id=7']}><ToastProvider><Quotes /></ToastProvider></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Notes' }), { target: { value: 'My unsaved planning note' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
  expect(await within(dialog).findByText(detail)).toBeInTheDocument();
  expect(within(dialog).getByRole('textbox', { name: 'Notes' })).toHaveValue('My unsaved planning note');
  expect(mockedApi.updateQuote).toHaveBeenCalledWith(7, expect.objectContaining({ expected_updated_at: quote.updated_at, notes: 'My unsaved planning note' }));
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Save draft' })).toBeEnabled());
  expect(mockedApi.updateQuote).toHaveBeenCalledTimes(1);
});


it('starts new estimates in fabrication quoting and ignores retired calculator navigation state', async () => {
  mockedApi.getQuotes.mockResolvedValue([]);
  const Location = () => {
    const location = useLocation();
    return <div data-testid="quote-location">{location.pathname}</div>;
  };
  render(<MemoryRouter initialEntries={[{ pathname: '/quotes', state: { calculatorDraft: { customer_name: 'Old draft', lines: [] } } }]}><ToastProvider><Quotes /><Location /></ToastProvider></MemoryRouter>);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  fireEvent.click(screen.getAllByRole('button', { name: 'New Quote' })[0]);
  expect(screen.getByTestId('quote-location')).toHaveTextContent('/fabrication-quotes');
});

it.each([null, 42])('keeps customer quote prices read only and saves only commercial fields (fabrication link %s)', async fabricationId => {
  const quote = {
    id: 8, quote_number: 'QUO-0008', revision: 'A', customer_name: 'Acme', status: 'draft',
    quote_date: '2026-09-06', updated_at: '2026-09-06T12:00:00', subtotal: 100, total: 100,
    fabrication_quote_id: fabricationId, lines: [{ id: 13, line_number: 1, description: 'Approved fabrication package', quantity: 1, unit_price: 100, line_total: 100 }],
  };
  mockedApi.getQuotes.mockResolvedValue([quote]);
  mockedApi.updateQuote.mockResolvedValue(quote);
  render(<MemoryRouter initialEntries={['/quotes?id=8']}><ToastProvider><Quotes /></ToastProvider></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).queryByRole('spinbutton', { name: 'Line item unit price' })).not.toBeInTheDocument();
  expect(within(dialog).queryByRole('button', { name: /Add Line/i })).not.toBeInTheDocument();
  expect(within(dialog).getByRole('link', { name: fabricationId ? 'Open fabrication estimate to revise' : 'Create a fabrication estimate for new pricing' })).toHaveAttribute('href', fabricationId ? '/fabrication-quotes?id=42' : '/fabrication-quotes');
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Notes' }), { target: { value: 'Customer delivery note' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
  await waitFor(() => expect(mockedApi.updateQuote).toHaveBeenCalledWith(8, expect.objectContaining({ notes: 'Customer delivery note' })));
  const payload = mockedApi.updateQuote.mock.calls.at(-1)?.[1];
  expect(payload).not.toHaveProperty('lines');
  expect(payload).not.toHaveProperty('valid_days');
});

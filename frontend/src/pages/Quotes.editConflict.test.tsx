jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, company_id: 1, role: 'admin', is_superuser: false } }) }));
import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({ __esModule: true, default: {
  getQuotes: jest.fn(), getParts: jest.fn(), getQuote: jest.fn(), updateQuote: jest.fn(), createQuote: jest.fn(),
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

describe('quote creation replay protection', () => {
  const draft = { customer_name: 'Acme', lines: [{ description: 'Fixture', part_id: 0, quantity: 4, unit_price: 10, labor_hours: 0 }] };
  const saved = { id: 7, quote_number: 'QUO-SAVED', revision: 'A', customer_name: 'Acme', status: 'draft', quote_date: '2026-09-06', updated_at: '2026-09-06T12:00:00', subtotal: 40, total: 40, lines: [{ ...draft.lines[0], id: 12, line_number: 1, line_total: 40 }] };
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getQuotes.mockResolvedValue([]);
    mockedApi.getParts.mockResolvedValue([]);
    mockedApi.getQuote.mockResolvedValue(saved);
  });
  function renderDraft() {
    render(<MemoryRouter initialEntries={[{ pathname: '/quotes', state: { calculatorDraft: draft } }]}><ToastProvider><Quotes /></ToastProvider></MemoryRouter>);
  }
  it('reuses one create request key after an ambiguous failure', async () => {
    mockedApi.createQuote.mockRejectedValueOnce(new Error('Response lost')).mockResolvedValueOnce(saved);
    renderDraft();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create Quote' }));
    expect(await within(dialog).findByText(/retrying this draft will recover its saved quote/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create Quote' }));
    await waitFor(() => expect(mockedApi.createQuote).toHaveBeenCalledTimes(2));
    const first = mockedApi.createQuote.mock.calls[0][0];
    const retry = mockedApi.createQuote.mock.calls[1][0];
    expect(first.request_key).toEqual(expect.any(String));
    expect(first.request_key.length).toBeGreaterThanOrEqual(8);
    expect(retry.request_key).toBe(first.request_key);
  });
  it('freezes the submitted fields while the create response is pending', async () => {
    let resolveCreate!: (value: typeof saved) => void;
    mockedApi.createQuote.mockImplementationOnce(() => new Promise(resolve => { resolveCreate = resolve; }));
    renderDraft();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create Quote' }));
    expect(within(dialog).getByRole('textbox', { name: 'Notes' })).toBeDisabled();
    expect(within(dialog).getByRole('spinbutton', { name: 'Line item quantity' })).toBeDisabled();
    resolveCreate(saved);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
  it('keeps the draft and exposes the saved quote when its committed key conflicts', async () => {
    mockedApi.createQuote.mockRejectedValue({ response: { status: 409, data: { detail: { message: 'Request already saved with different content', quote_id: 7, quote_number: 'QUO-SAVED' } } } });
    renderDraft();
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Notes' }), { target: { value: 'My changed terms' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create Quote' }));
    expect(await within(dialog).findByText('Request already saved with different content')).toBeInTheDocument();
    expect(within(dialog).getByRole('textbox', { name: 'Notes' })).toHaveValue('My changed terms');
    expect(within(dialog).getByRole('button', { name: 'Open existing quote QUO-SAVED' })).toBeInTheDocument();
  });
});

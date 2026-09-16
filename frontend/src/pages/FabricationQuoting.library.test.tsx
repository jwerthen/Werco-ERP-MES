import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { fabricationQuoteApi } from '../features/fabrication-quote/api';
import { emptyPlan } from '../features/fabrication-quote/types';
import type { QuoteRecord, QuoteSummary } from '../features/fabrication-quote/types';
import FabricationQuoting from './FabricationQuoting';

jest.mock('../services/api', () => ({ __esModule: true, default: { getCustomerNames: jest.fn() } }));
jest.mock('../features/fabrication-quote/api', () => ({ fabricationQuoteApi: { list: jest.fn(), get: jest.fn(), capabilities: jest.fn() } }));

const quotes = Array.from({ length: 31 }, (_, index): QuoteSummary => ({ id: index + 1, title: index === 30 ? 'Archived bracket' : `Fixture ${index + 1}`, customer_id: null, status: 'draft', revision: 1 }));
const record: QuoteRecord = { ...quotes[0], plan: emptyPlan(), calculation: null, files: [] };
const list = jest.mocked(fabricationQuoteApi.list);

beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(api.getCustomerNames).mockResolvedValue([]);
  jest.mocked(fabricationQuoteApi.capabilities).mockResolvedValue({ can_write: true });
  jest.mocked(fabricationQuoteApi.get).mockResolvedValue(record);
  list.mockImplementation(async ({ page = 1, per_page = 30, search = '' } = {}) => {
    const results = quotes.filter(quote => quote.title.toLowerCase().includes(search.toLowerCase()));
    return { items: results.slice((page - 1) * per_page, page * per_page), total: results.length };
  });
});

function renderWorkspace() {
  render(<MemoryRouter initialEntries={['/fabrication-quotes?id=1']}><FabricationQuoting /></MemoryRouter>);
  return screen.getByRole('complementary', { name: 'Quote library' });
}

it('pages beyond 30 quotes and searches the server without replacing the selected draft or losing edits', async () => {
  const library = renderWorkspace();
  expect(await within(library).findByText('31 quotes · Page 1 of 2')).toBeInTheDocument();
  const title = screen.getByRole('textbox', { name: 'Quote title' });
  await waitFor(() => expect(title).toBeEnabled());
  fireEvent.change(title, { target: { value: 'Unsaved estimator work' } });
  fireEvent.click(within(library).getByRole('button', { name: 'Next' }));
  expect(await within(library).findByText('31 quotes · Page 2 of 2')).toBeInTheDocument();
  expect(list).toHaveBeenLastCalledWith({ page: 2, per_page: 30, search: '' });
  expect(within(library).getByRole('button', { name: /Archived bracket/ })).toBeInTheDocument();
  expect(within(library).getByRole('button', { name: 'Next' })).toBeDisabled();
  expect(title).toHaveValue('Unsaved estimator work');
  expect(fabricationQuoteApi.get).toHaveBeenCalledTimes(1);

  fireEvent.click(within(library).getByRole('button', { name: /Archived bracket/ }));
  expect(screen.getByText('This draft has unsaved changes.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
  fireEvent.change(within(library).getByRole('textbox', { name: 'Find a quote' }), { target: { value: 'Archived' } });
  expect(await within(library).findByText('1 matching quote · Page 1 of 1')).toBeInTheDocument();
  expect(list).toHaveBeenLastCalledWith({ page: 1, per_page: 30, search: 'Archived' });
  expect(within(library).getByRole('button', { name: 'Previous' })).toBeDisabled();
  expect(title).toHaveValue('Unsaved estimator work');
  expect(fabricationQuoteApi.get).toHaveBeenCalledTimes(1);

  fireEvent.change(within(library).getByRole('textbox', { name: 'Find a quote' }), { target: { value: '' } });
  expect(await within(library).findByText('31 quotes · Page 1 of 2')).toBeInTheDocument();
  expect(list).toHaveBeenLastCalledWith({ page: 1, per_page: 30, search: '' });
});

it('ignores an older search response that arrives after the current results', async () => {
  let resolveOlder!: (value: { items: QuoteSummary[]; total: number }) => void;
  const implementation = list.getMockImplementation()!;
  list.mockImplementation(params => params?.search === 'Fixture' ? new Promise(resolve => { resolveOlder = resolve; }) : implementation(params));
  const library = renderWorkspace();
  await within(library).findByText('31 quotes · Page 1 of 2');
  fireEvent.change(within(library).getByRole('textbox', { name: 'Find a quote' }), { target: { value: 'Fixture' } });
  await waitFor(() => expect(list).toHaveBeenLastCalledWith({ page: 1, per_page: 30, search: 'Fixture' }));
  fireEvent.change(within(library).getByRole('textbox', { name: 'Find a quote' }), { target: { value: 'Archived' } });
  await within(library).findByText('1 matching quote · Page 1 of 1');
  await act(async () => resolveOlder({ items: quotes.slice(0, 30), total: 30 }));
  expect(within(library).getByText('1 matching quote · Page 1 of 1')).toBeInTheDocument();
  expect(within(library).queryByRole('button', { name: /Fixture 2 / })).not.toBeInTheDocument();
  expect(within(library).getByRole('button', { name: /Archived bracket/ })).toBeInTheDocument();
});

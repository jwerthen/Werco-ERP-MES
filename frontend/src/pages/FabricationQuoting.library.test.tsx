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
let mockUser = { id: 1, company_id: 1 };
let mockCompany = { id: 1 };
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: mockCompany }) }));

const quotes = Array.from({ length: 31 }, (_, index): QuoteSummary => ({ id: index + 1, title: index === 30 ? 'Archived bracket' : `Fixture ${index + 1}`, customer_id: null, status: 'draft', revision: 1 }));
const record: QuoteRecord = { ...quotes[0], plan: emptyPlan(), calculation: null, files: [] };
const list = jest.mocked(fabricationQuoteApi.list);

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.removeItem('token');
  mockUser = { id: 1, company_id: 1 };
  mockCompany = { id: 1 };
  jest.mocked(api.getCustomerNames).mockResolvedValue([]);
  jest.mocked(fabricationQuoteApi.capabilities).mockResolvedValue({ can_write: true });
  jest.mocked(fabricationQuoteApi.get).mockResolvedValue(record);
  list.mockImplementation(async ({ page = 1, per_page = 30, search = '' } = {}) => {
    const results = quotes.filter(quote => quote.title.toLowerCase().includes(search.toLowerCase()));
    return { items: results.slice((page - 1) * per_page, page * per_page), total: results.length };
  });
});

afterEach(() => { jest.useRealTimers(); sessionStorage.removeItem('token'); });

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

it('retries transient failures after backoff while the draft stays editable', async () => {
  jest.useFakeTimers();
  list.mockRejectedValueOnce(new Error('Network Error'));
  const library = renderWorkspace();
  await act(async () => {});
  expect(list).toHaveBeenCalledTimes(1);
  expect(within(library).getByText(/Retrying quote library automatically/)).toBeInTheDocument();
  const title = screen.getByRole('textbox', { name: 'Quote title' });
  expect(title).toBeEnabled();
  fireEvent.change(title, { target: { value: 'Keep this draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Hardware' }));
  expect(screen.getByRole('button', { name: 'Hardware' })).toHaveAttribute('aria-current', 'page');
  await act(async () => { jest.advanceTimersByTime(499); });
  expect(list).toHaveBeenCalledTimes(1);
  await act(async () => { jest.advanceTimersByTime(1); });
  expect(list).toHaveBeenCalledTimes(2);
  expect(within(library).getByRole('button', { name: /Fixture 2 / })).toBeInTheDocument();
  expect(title).toHaveValue('Keep this draft');
  expect(within(library).queryByRole('alert')).not.toBeInTheDocument();
});

it('keeps last-loaded quotes during retries and failure, and supports manual recovery', async () => {
  jest.useFakeTimers();
  const library = renderWorkspace();
  await act(async () => {});
  const title = screen.getByRole('textbox', { name: 'Quote title' });
  fireEvent.change(title, { target: { value: 'Unsaved work' } });
  list.mockRejectedValue(new Error('Network Error'));
  fireEvent.click(within(library).getByRole('button', { name: 'Refresh quote library' }));
  await act(async () => {});
  expect(within(library).getByRole('button', { name: /Fixture 2 / })).toBeEnabled();
  await act(async () => { jest.advanceTimersByTime(500); });
  expect(list).toHaveBeenCalledTimes(3);
  await act(async () => { jest.advanceTimersByTime(999); });
  expect(list).toHaveBeenCalledTimes(3);
  await act(async () => { jest.advanceTimersByTime(1); });
  expect(list).toHaveBeenCalledTimes(4);
  expect(within(library).getByRole('alert')).toHaveTextContent('You can keep editing your draft');
  expect(within(library).getByRole('button', { name: /Fixture 2 / })).toBeEnabled();
  expect(within(library).getByText(/Showing last loaded quotes/)).toBeInTheDocument();
  expect(title).toHaveValue('Unsaved work');
  list.mockResolvedValue({ items: [quotes[30]], total: 1 });
  fireEvent.click(within(library).getByRole('button', { name: 'Retry quote library' }));
  await act(async () => {});
  expect(within(library).getByRole('button', { name: /Archived bracket/ })).toBeInTheDocument();
  expect(within(library).queryByRole('alert')).not.toBeInTheDocument();
  expect(title).toHaveValue('Unsaved work');
});

it('identifies old search results when a different search fails', async () => {
  jest.useFakeTimers();
  const library = renderWorkspace();
  await act(async () => {});
  list.mockRejectedValue({ response: { status: 400 } });
  fireEvent.change(within(library).getByRole('textbox', { name: 'Find a quote' }), { target: { value: 'Missing' } });
  await act(async () => { jest.advanceTimersByTime(250); });
  expect(within(library).getByRole('button', { name: /Fixture 2 / })).toBeInTheDocument();
  expect(within(library).getByText(/Showing last loaded quotes · Page 1/)).toBeInTheDocument();
  expect(within(library).getByText('31 quotes · Page 1 of 2 · Last loaded')).toBeInTheDocument();
  expect(list).toHaveBeenCalledTimes(2);
});

it.each(['account', 'company'])('clears last-known quotes on a change of %s', async scope => {
  const view = render(<MemoryRouter><FabricationQuoting /></MemoryRouter>);
  await screen.findByText('31 quotes · Page 1 of 2');
  list.mockRejectedValue({ response: { status: 403 } });
  if (scope === 'account') mockUser = { id: 2, company_id: 1 };
  else mockCompany = { id: 2 };
  view.rerender(<MemoryRouter><FabricationQuoting /></MemoryRouter>);
  const library = screen.getByRole('complementary', { name: 'Quote library' });
  await within(library).findByRole('alert');
  expect(within(library).queryByRole('button', { name: /Fixture 2 / })).not.toBeInTheDocument();
  expect(within(library).queryByText(/Showing last loaded/)).not.toBeInTheDocument();
  expect(within(library).getByText(/Your current draft stays open/)).toBeInTheDocument();
});

const tokenFor = (company: number, expiry: number) => `header.${btoa(JSON.stringify({ sub: '1', cid: company, exp: expiry }))}.signature`;

it('clears old quotes as soon as the token switches company, before company metadata resolves', async () => {
  sessionStorage.setItem('token', tokenFor(1, 1000));
  const library = renderWorkspace();
  await within(library).findByText('31 quotes · Page 1 of 2');
  let resolveOldRequest!: (value: { items: QuoteSummary[]; total: number }) => void;
  list.mockImplementationOnce(() => new Promise(resolve => { resolveOldRequest = resolve; }));
  fireEvent.click(within(library).getByRole('button', { name: 'Refresh quote library' }));
  list.mockRejectedValue({ response: { status: 403 } });
  act(() => {
    sessionStorage.setItem('token', tokenFor(2, 2000));
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  // The provider still has company 1: the new token must take precedence.
  expect(mockCompany.id).toBe(1);
  const nextLibrary = screen.getByRole('complementary', { name: 'Quote library' });
  await within(nextLibrary).findByRole('alert');
  await act(async () => resolveOldRequest({ items: quotes, total: quotes.length }));
  expect(within(nextLibrary).queryByRole('button', { name: /Fixture 2 / })).not.toBeInTheDocument();
  expect(within(nextLibrary).queryByText(/Showing last loaded/)).not.toBeInTheDocument();
});

it('preserves the draft and library when an access token refresh keeps the same account and company', async () => {
  sessionStorage.setItem('token', tokenFor(1, 1000));
  const library = renderWorkspace();
  await within(library).findByText('31 quotes · Page 1 of 2');
  const title = screen.getByRole('textbox', { name: 'Quote title' });
  await waitFor(() => expect(title).toBeEnabled());
  fireEvent.change(title, { target: { value: 'Preserve this unsaved draft' } });
  act(() => {
    sessionStorage.setItem('token', tokenFor(1, 2000));
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(screen.getByRole('textbox', { name: 'Quote title' })).toHaveValue('Preserve this unsaved draft');
  expect(list).toHaveBeenCalledTimes(1);
  expect(fabricationQuoteApi.get).toHaveBeenCalledTimes(1);
  expect(within(library).getByRole('button', { name: /Fixture 2 / })).toBeInTheDocument();
});

it('does not retry an obsolete search after its backoff expires', async () => {
  jest.useFakeTimers();
  const implementation = list.getMockImplementation()!;
  list.mockImplementation(params => params?.search === 'Fixture' ? Promise.reject(new Error('Network Error')) : implementation(params));
  const library = renderWorkspace();
  await act(async () => {});
  const search = within(library).getByRole('textbox', { name: 'Find a quote' });
  fireEvent.change(search, { target: { value: 'Fixture' } });
  await act(async () => { jest.advanceTimersByTime(250); });
  fireEvent.change(search, { target: { value: 'Archived' } });
  await act(async () => { jest.advanceTimersByTime(250); });
  await act(async () => { jest.advanceTimersByTime(1000); });
  expect(list).toHaveBeenCalledTimes(3);
  expect(within(library).getByRole('button', { name: /Archived bracket/ })).toBeInTheDocument();
  expect(within(library).queryByRole('alert')).not.toBeInTheDocument();
});

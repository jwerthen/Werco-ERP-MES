import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import SearchResults from './SearchResults';
import { EntitySearchResponse } from '../types/search';

jest.mock('../hooks/usePermissions', () => ({ usePermissions: () => ({ can: () => true }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 1 } }) }));
jest.mock('../services/api', () => ({ __esModule: true, default: { search: jest.fn() } }));
const search = api.search as jest.MockedFunction<typeof api.search>;
const fixture = (title: string, extra: Partial<EntitySearchResponse> = {}): EntitySearchResponse => ({
  query: 'OLD',
  total: 30,
  offset: 0,
  limit: 25,
  has_more: true,
  categories: { part: 30 },
  results: [{ id: 1, type: 'part', title, subtitle: 'Plate', matched_alias: 'OLD', url: '/parts/1', icon: 'cube' }],
  ...extra,
});
beforeEach(() => jest.clearAllMocks());
const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/search?q=OLD']}>
      <SearchResults />
    </MemoryRouter>
  );
it('shows the full total, retired number, and requests subsequent pages and types', async () => {
  search.mockResolvedValue(fixture('CURRENT'));
  renderPage();
  await screen.findByText('Formerly OLD');
  expect(screen.getByText('1–1 of 30 matches')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await waitFor(() =>
    expect(search).toHaveBeenLastCalledWith(
      'OLD',
      'part,work_order,customer,bom,routing,user,vendor,purchase_order,quote',
      { offset: 25, limit: 25 }
    )
  );
  fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'part' } });
  await waitFor(() => expect(search).toHaveBeenLastCalledWith('OLD', 'part', { offset: 0, limit: 25 }));
});
it('drops an older query response after the user submits another query', async () => {
  let finish!: (data: EntitySearchResponse) => void;
  search
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    )
    .mockResolvedValue(fixture('NEW HIT'));
  renderPage();
  fireEvent.change(screen.getByLabelText('Search records'), { target: { value: 'NEW' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search' }));
  await screen.findByText('NEW HIT');
  await act(async () => finish(fixture('OLD HIT')));
  expect(screen.queryByText('OLD HIT')).not.toBeInTheDocument();
  expect(screen.getByText('NEW HIT')).toBeInTheDocument();
});

it('offers a safe reset when a bookmarked page is beyond the last result', async () => {
  search.mockResolvedValue(fixture('unused', { results: [], total: 30, has_more: false }));
  render(
    <MemoryRouter initialEntries={['/search?q=OLD&offset=100']}>
      <SearchResults />
    </MemoryRouter>
  );
  await screen.findByText('No results on this page. 30 matches are available.');
  fireEvent.click(screen.getByRole('button', { name: 'First page' }));
  await waitFor(() => expect(search).toHaveBeenLastCalledWith('OLD', expect.any(String), { offset: 0, limit: 25 }));
});

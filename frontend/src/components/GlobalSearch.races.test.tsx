import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import GlobalSearch from './GlobalSearch';
import api from '../services/api';
const navigate = jest.fn();
jest.mock('react-router-dom', () => ({ ...jest.requireActual('react-router-dom'), useNavigate: () => navigate }));
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: (permission: string) => permission !== 'admin:settings' }),
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getRecentItems: jest.fn(), search: jest.fn(), naturalLanguageSearch: jest.fn() },
}));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => {
    resolve = yes;
  });
  return { promise, resolve };
}
const result = (title: string) => ({ id: 1, type: 'part', title, url: '/parts/1', icon: 'cube' });
beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  (api.getRecentItems as jest.Mock).mockResolvedValue([]);
  (api.search as jest.Mock).mockResolvedValue({ results: [] });
});
afterEach(() => jest.useRealTimers());
async function mount() {
  await act(async () => {
    render(
      <MemoryRouter>
        <GlobalSearch isOpen onClose={jest.fn()} />
      </MemoryRouter>
    );
    jest.advanceTimersByTime(100);
  });
  return screen.getByLabelText('Search across your workspace');
}
it('keeps a typed query after delayed recents arrive', async () => {
  const recent = deferred<any[]>();
  (api.getRecentItems as jest.Mock).mockReturnValue(recent.promise);
  const input = await mount();
  fireEvent.change(input, { target: { value: 'bolt' } });
  await act(async () => {
    recent.resolve([result('Yesterday')]);
  });
  expect(input).toHaveValue('bolt');
});
it('ignores a cleared request and retries an explicit failure', async () => {
  const old = deferred<any>();
  (api.search as jest.Mock)
    .mockReturnValueOnce(old.promise)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ results: [result('Found bolt')] });
  const input = await mount();
  fireEvent.change(input, { target: { value: 'old' } });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  fireEvent.click(screen.getByLabelText('Clear search'));
  await act(async () => {
    old.resolve({ results: [result('Stale')] });
  });
  expect(screen.queryByText('Stale')).not.toBeInTheDocument();
  fireEvent.change(input, { target: { value: 'bolt' } });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(screen.getByRole('alert')).toHaveTextContent('Search is unavailable');
  expect(screen.queryByText(/No results found/)).not.toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByText('Retry search'));
  });
  expect(screen.getByText('Found bolt')).toBeInTheDocument();
});
it('filters inaccessible quick actions and activates them by keyboard', async () => {
  const input = await mount();
  expect(screen.queryByText('Setup Wizard')).not.toBeInTheDocument();
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(navigate).toHaveBeenCalledWith('/traceability');
});

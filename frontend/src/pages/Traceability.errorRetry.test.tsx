/**
 * Traceability — a failed search renders the shared <ErrorState> whose Retry
 * re-runs the search (replacing the old bare red banner div).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Traceability from './Traceability';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    searchLots: jest.fn(),
    traceLot: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail: string) => {
  const err = new Error(detail) as Error & {
    response: { status: number; data: { detail: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/traceability']}>
      <Traceability />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
});

test('a failed search renders ErrorState and Retry re-runs the search to success', async () => {
  mockedApi.searchLots
    .mockRejectedValueOnce(http(500, 'Trace backend unavailable'))
    .mockResolvedValueOnce([
      { type: 'lot', number: 'LOT-100', part_number: 'PN-1', quantity: 5, location: 'A1' },
      { type: 'serial', number: 'SER-9', part_number: 'PN-1' },
    ] as any);

  renderPage();

  fireEvent.change(screen.getByLabelText(/search by lot/i), { target: { value: 'LOT-100' } });
  fireEvent.click(screen.getByRole('button', { name: /trace/i }));

  // The shared ErrorState renders with the server detail.
  const errorState = await screen.findByTestId('error-state');
  expect(errorState).toHaveTextContent('Trace backend unavailable');

  // Retry re-runs the same search; the second call succeeds and results render.
  fireEvent.click(screen.getByRole('button', { name: /retry/i }));

  await waitFor(() => expect(mockedApi.searchLots).toHaveBeenCalledTimes(2));
  expect(mockedApi.searchLots).toHaveBeenLastCalledWith('LOT-100');
  expect(await screen.findByText('LOT-100')).toBeInTheDocument();
  expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
});

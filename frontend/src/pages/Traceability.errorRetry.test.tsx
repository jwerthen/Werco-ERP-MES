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
    traceSerial: jest.fn(),
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


test('a serial result uses the serial endpoint and keeps loading until its history arrives', async () => {
  let resolveTrace: (value: any) => void = () => {};
  mockedApi.searchLots.mockResolvedValue([{ type: 'serial', number: 'SER-9' }] as any);
  mockedApi.traceSerial.mockReturnValue(new Promise(resolve => { resolveTrace = resolve; }));
  renderPage();
  fireEvent.change(screen.getByLabelText(/search by lot/i), { target: { value: 'SER-9' } });
  fireEvent.click(screen.getByRole('button', { name: /trace/i }));
  await waitFor(() => expect(mockedApi.traceSerial).toHaveBeenCalledWith('SER-9'));
  expect(mockedApi.traceLot).not.toHaveBeenCalled();
  expect(screen.getByRole('status')).toHaveTextContent('Loading trace');
  resolveTrace({ serial_number: 'SER-9', lot_number: 'LOT-1', status: 'available', history: [], work_orders_used: [], ncrs: [] });
  expect(await screen.findByRole('heading', { name: 'Serial: SER-9' })).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

test('a successful search with no matches has an explicit recovery message', async () => {
  mockedApi.searchLots.mockResolvedValue([]);
  renderPage();
  fireEvent.change(screen.getByLabelText(/search by lot/i), { target: { value: 'NO-MATCH' } });
  fireEvent.click(screen.getByRole('button', { name: /trace/i }));
  expect(await screen.findByText(/No matching lot or serial records/)).toBeInTheDocument();
});

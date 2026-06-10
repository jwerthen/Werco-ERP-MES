/**
 * Wallboard — the full-screen shop-floor TV board (A0.5).
 *
 * Covers: rendering work-center cards / jobs / ticker from mock payload data
 * (including truncated operator names and blocked/down badges), the OFFLINE
 * banner that keeps the last good data on a failed poll, the ?dept= filter
 * being passed through to the fetch helper, and the no-token guidance screen.
 *
 * services/wallboardClient is mocked at the module boundary — the page must
 * never touch the global axios client (a display token cannot enter it).
 */

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Wallboard from './Wallboard';
import {
  captureWallboardTokenFromUrl,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type { WallboardResponse } from '../types/wallboard';

jest.mock('../services/wallboardClient', () => ({
  __esModule: true,
  captureWallboardTokenFromUrl: jest.fn(),
  getWallboardToken: jest.fn(() => 'display-jwt'),
  fetchWallboard: jest.fn(),
}));

const mockFetchWallboard = fetchWallboard as jest.MockedFunction<typeof fetchWallboard>;
const mockGetToken = getWallboardToken as jest.MockedFunction<typeof getWallboardToken>;
const mockCapture = captureWallboardTokenFromUrl as jest.MockedFunction<
  typeof captureWallboardTokenFromUrl
>;

const payload: WallboardResponse = {
  work_centers: [
    {
      id: 1,
      code: 'LASER-1',
      name: 'Laser 1',
      status: 'in_use',
      active_jobs: [
        {
          wo_number: 'WO-1001',
          part_number: 'PN-77',
          op_name: 'Laser Cut',
          operator_name: 'Jon W.',
          elapsed_minutes: 75,
          qty_done: 12,
          qty_target: 50,
        },
      ],
      queued_count: 3,
      blocked_count: 0,
      down: null,
    },
    {
      id: 2,
      code: 'WELD-2',
      name: 'Weld 2',
      status: 'available',
      active_jobs: [],
      queued_count: 0,
      blocked_count: 2,
      down: { category: 'mechanical', since: '2026-06-10T12:00:00Z', minutes: 18 },
    },
  ],
  late_wos: [
    { wo_number: 'WO-0999', part_number: 'PN-12', due_date: '2026-06-07', days_late: 3, status: 'in_progress' },
  ],
  blocked_wos: [{ wo_number: 'WO-0998', category: 'material_missing', age_hours: 5.5 }],
  generated_at: '2026-06-10T13:00:00Z',
};

function renderWallboard(initialEntry = '/wallboard') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/wallboard" element={<Wallboard />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetToken.mockReturnValue('display-jwt');
});

describe('Wallboard', () => {
  it('renders work-center cards, jobs, and the ticker from the payload', async () => {
    mockFetchWallboard.mockResolvedValue(payload);
    renderWallboard();

    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
    expect(mockCapture).toHaveBeenCalled();

    // Card 1: running job with truncated operator name + elapsed + qty
    expect(screen.getByText('Laser 1')).toBeInTheDocument();
    expect(screen.getByText(/WO-1001/)).toBeInTheDocument();
    expect(screen.getByText(/Laser Cut — Jon W\./)).toBeInTheDocument();
    expect(screen.getByText('1h 15m')).toBeInTheDocument();
    expect(screen.getByText('12/50')).toBeInTheDocument();
    expect(screen.getByText(/Queue 3/)).toBeInTheDocument();

    // Card 2: idle, blocked + down badges
    expect(screen.getByText('Weld 2')).toBeInTheDocument();
    expect(screen.getByText('Idle')).toBeInTheDocument();
    expect(screen.getByText(/2 blocked/i)).toBeInTheDocument();
    expect(screen.getByText(/Down · mechanical/i)).toBeInTheDocument();

    // Ticker shows late/blocked rotation content
    expect(screen.getByTestId('ticker')).toHaveTextContent(/LATE\s+WO-0999/);

    // No OFFLINE banner when healthy
    expect(screen.queryByTestId('offline-banner')).not.toBeInTheDocument();
  });

  it('shows the OFFLINE banner but keeps the last good data when a poll fails', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();

      // Next poll fails
      mockFetchWallboard.mockRejectedValueOnce(new Error('HTTP_500'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });

      expect(await screen.findByTestId('offline-banner')).toBeInTheDocument();
      // Last good data still on screen
      expect(screen.getByText('Laser 1')).toBeInTheDocument();
      expect(screen.getByText(/WO-1001/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it('passes the ?dept= param through to the fetch helper and shows it in the header', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, work_centers: [payload.work_centers[0]] });
    renderWallboard('/wallboard?dept=machining');

    await waitFor(() => expect(mockFetchWallboard).toHaveBeenCalledWith('machining'));
    expect(screen.getByTestId('dept-label')).toHaveTextContent('machining');
  });

  it('shows guidance when no token is available', async () => {
    mockGetToken.mockReturnValue(null);
    mockFetchWallboard.mockRejectedValue(new Error('NO_TOKEN'));
    renderWallboard();

    expect(await screen.findByText('No display token')).toBeInTheDocument();
    expect(screen.getByText(/Admin Settings/)).toBeInTheDocument();
  });
});

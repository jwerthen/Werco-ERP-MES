/**
 * OperatorKiosk — the run-order rank survives the trip from the server payload
 * to the queue card.
 *
 * KioskRunOrderChip.test.tsx proves the chip renders a rank it is HANDED. This
 * proves the page actually hands it one: the rank arrives on a realistic
 * `GET /shop-floor/work-center-queue/{id}` payload and reaches the card, and
 * the page renders the queue in the order the SERVER returned it (the sort is
 * server-side and advisory — the kiosk must not re-sort or filter by rank).
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

/** Queue rows shaped like the server's, already in the server's order. */
const RANKED = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-RANK-0001',
  part_number: 'PN-1',
  part_name: 'Bracket',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
  run_order: 1,
};

const SECOND = { ...RANKED, operation_id: 32, work_order_id: 10, work_order_number: 'WO-RANK-0002', run_order: 2 };
// Unranked work sorts last server-side and shows NO chip.
const UNRANKED = { ...RANKED, operation_id: 33, work_order_id: 11, work_order_number: 'WO-RANK-0003', run_order: null };

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

describe('OperatorKiosk run order', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedUseAuth.mockReturnValue({
      user: { id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' },
      isAuthenticated: true,
      isLoading: false,
      loginWithEmployeeId: jest.fn(),
      logout: jest.fn(),
    });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
  });

  it('passes the server rank through to each queue card, in server order', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [RANKED, SECOND, UNRANKED] });
    renderKiosk();

    const cards = await screen.findAllByRole('button', { name: /WO-RANK-000/ });
    expect(cards.map((card) => card.textContent)).toHaveLength(3);

    expect(within(cards[0]).getByTestId('kiosk-run-order-chip')).toHaveTextContent('1');
    expect(within(cards[1]).getByTestId('kiosk-run-order-chip')).toHaveTextContent('2');
    // Third row is unranked: no chip at all, and it stays where the server put it.
    expect(within(cards[2]).queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
    expect(cards[2]).toHaveTextContent('WO-RANK-0003');
  });

  it('does not re-sort by rank — the server order is rendered verbatim', async () => {
    // A deliberately "wrong-looking" payload: rank 2 ahead of rank 1. The kiosk
    // renders exactly what it was given, so a client-side sort would show up here.
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [SECOND, RANKED] });
    renderKiosk();

    const cards = await screen.findAllByRole('button', { name: /WO-RANK-000/ });
    expect(cards[0]).toHaveTextContent('WO-RANK-0002');
    expect(within(cards[0]).getByTestId('kiosk-run-order-chip')).toHaveTextContent('2');
    expect(cards[1]).toHaveTextContent('WO-RANK-0001');
  });

  it('renders a queue with no ranks at all unchanged (rank is optional)', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [{ ...RANKED, run_order: undefined }] });
    renderKiosk();

    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });
});

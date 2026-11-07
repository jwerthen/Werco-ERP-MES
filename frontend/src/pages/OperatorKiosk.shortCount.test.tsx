/**
 * The single-operator kiosk's COMPLETE and CLOCK OUT · KEEP OPEN verbs under owner
 * ruling 1 (2026-11-05): a short count at operation completion is reported as scrap
 * with a reason, or the operation stays open.
 *
 * The matrix pinned here (the full-count and covered-short COMPLETE paths, and the
 * chain, are in `OperatorKiosk.test.tsx` / `OperatorKiosk.foundry.test.tsx`):
 *  - KEEP OPEN is its own verb on the running panel: GOOD starts at 0, it fires the
 *    clock-out ONLY, never `/complete`, never a chained clock-in, and says how much is
 *    still unaccounted for;
 *  - a clock-out refused before anything landed renders INLINE in the overlay, which
 *    stays open with the entry intact (e.g. the operation was completed from the office
 *    while this operator was still clocked in) — and the operator can then keep it open
 *    with 0;
 *  - a crewmate on the queue row's roster blocks a SHORT finish in the overlay itself;
 *  - once the clock-out landed, a refused close is reported honestly.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: null }) }));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    getScrapReasonCodes: jest.fn(),
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

const ROSA = {
  id: 3,
  first_name: 'Rosa',
  last_name: 'Vega',
  employee_id: 'EMP-4217',
  role: 'operator',
  email: 'r@x.y',
};

const ROSTER_ROSA = {
  time_entry_id: 501,
  user_id: 3,
  operator_name: 'Rosa V',
  employee_id: 'EMP-4217',
  entry_type: 'run',
  clock_in: '2026-11-07T12:00:00Z',
};
const ROSTER_BOB = { ...ROSTER_ROSA, time_entry_id: 502, user_id: 11, operator_name: 'Bob T', employee_id: 'E011' };

const ACTIVE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 10,
  quantity_complete: 2,
  quantity_scrapped: 0,
  upstream_quantity_short: 0,
  quantity_good_ceiling: 10,
  quantity_unaccounted: 8,
  priority: 5,
  due_date: null,
  roster: [ROSTER_ROSA],
};

const NEXT_ITEM = {
  ...ACTIVE_ITEM,
  operation_id: 32,
  work_order_id: 10,
  work_order_number: 'WO-2026-0143',
  status: 'ready',
  quantity_complete: 0,
  roster: [],
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 10,
  quantity_complete: 2,
  operation_quantity_scrapped: 0,
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

function key(digits: string) {
  for (const d of digits) fireEvent.click(screen.getByTestId(`kiosk-key-${d}`));
}

describe('OperatorKiosk — short count (owner ruling 1)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedUseAuth.mockReturnValue({
      user: ROSA,
      isAuthenticated: true,
      isLoading: false,
      loginWithEmployeeId: jest.fn(),
      logout: jest.fn(),
    });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getScrapReasonCodes.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [ACTIVE_ITEM, NEXT_ITEM] });
  });

  it('CLOCK OUT · KEEP OPEN clocks out ONLY — no /complete, no chain — and says what is left', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    renderKiosk();

    fireEvent.click(await screen.findByTestId('kiosk-active-keep-open'));
    // GOOD starts at 0 in this mode — the operator enters this session's pieces.
    expect(within(screen.getByTestId('kiosk-qty-good')).getByText('0')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('kiosk-qty-good'));
    key('3');
    fireEvent.click(screen.getByTestId('kiosk-complete-leave-open-confirm'));

    expect(await screen.findByText('Clocked out — Op 20 left open (5 not yet accounted for)')).toBeInTheDocument();
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 3,
      quantity_scrapped: 0,
      scrap_reason: undefined,
      scrap_reason_code_id: undefined,
      source: 'kiosk',
    });
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('KEEP OPEN with a full count: the clock-out completed the op, so it says Completed — still no chain', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'complete' });
    renderKiosk();

    fireEvent.click(await screen.findByTestId('kiosk-active-keep-open'));
    fireEvent.click(screen.getByTestId('kiosk-qty-good'));
    key('8');
    expect(screen.getByTestId('kiosk-complete-leave-open-full')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('kiosk-complete-leave-open-confirm'));

    expect(await screen.findByText('Completed WO-2026-0142')).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('a clock-out refused before anything landed renders INLINE and keeps the overlay and entry', async () => {
    mockedApi.clockOut.mockRejectedValue({
      response: {
        status: 400,
        data: { detail: 'Quantity (9) exceeds the 8 units that can reach Op 20 — 2 were closed short upstream.' },
      },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Quantity (9) exceeds the 8 units that can reach Op 20 — 2 were closed short upstream.'
    );
    // Still on the overlay with the entry as keyed; nothing else fired.
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeInTheDocument();
    expect(within(screen.getByTestId('kiosk-qty-good')).getByText('8')).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    expect(screen.queryByText(/Clocked out, but/)).not.toBeInTheDocument();
  });

  it('an op the office already completed: the 409 shows inline, and KEEP OPEN with 0 then clocks out', async () => {
    const already = 'Op 20 is already complete; clock out with 0 pieces and tell a supervisor about any extra pieces.';
    mockedApi.clockOut
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: already } } })
      .mockResolvedValueOnce({ operation_status: 'complete' });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));
    expect(await screen.findByRole('alert')).toHaveTextContent(already);

    // The operator answers it in place: switch to KEEP OPEN (GOOD resets to 0) and go.
    fireEvent.click(screen.getByTestId('kiosk-complete-leave-open'));
    expect(within(screen.getByTestId('kiosk-qty-good')).getByText('0')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('kiosk-complete-leave-open-confirm'));

    await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalledTimes(2));
    expect(mockedApi.clockOut).toHaveBeenLastCalledWith(501, expect.objectContaining({ quantity_produced: 0 }));
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('a crewmate on the roster blocks a SHORT finish in the overlay — leave-open is the way out', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [{ ...ACTIVE_ITEM, roster: [ROSTER_ROSA, ROSTER_BOB] }, NEXT_ITEM],
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-good'));
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    key('6');
    fireEvent.click(screen.getByTestId('kiosk-complete-report-scrap'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));

    expect(screen.getByTestId('kiosk-complete-others')).toHaveTextContent(
      'Bob T still clocked in — they must clock out with their count first'
    );
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
  });

  it('once the clock-out landed, a refused close is reported honestly and verbatim', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail:
            'Op 20: 2 of 10 not yet accounted for (8 good, 0 scrapped). Record finished pieces (clock out with your count, or Report production), report lost pieces as scrap with a reason, or leave the operation open. Pieces never started: ask a supervisor to lower the quantity.',
        },
      },
    });
    renderKiosk();

    // A stale snapshot: the overlay thought the 8 covered it; the server knows better.
    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    expect(
      await screen.findByText(/^Clocked out, but completing failed: Op 20: 2 of 10 not yet accounted for/)
    ).toBeInTheDocument();
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(31, { source: 'kiosk' });
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });
});

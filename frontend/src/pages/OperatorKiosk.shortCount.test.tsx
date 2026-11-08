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
 *  - once the clock-out landed, a refused close is reported honestly — including that
 *    scrap which rode that close was NOT saved, and (on a draft / held job, where the
 *    floor cannot clock back in) without advising a clock-in: a held job goes to the
 *    work order page (never "resume it"), a draft is released first (the office offers
 *    no Complete on a draft);
 *  - the handler decides from the overlay's own figures, so a crewmate's report that
 *    lands while the overlay is open cannot strand a real replacement scrap.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { WORK_ORDER_NOT_RELEASED_REASON, WORK_ORDER_ON_HOLD_REASON } from '../components/kiosk/heldOperationFixtures';

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

    const leftOpen = await screen.findByText('Clocked out — Op 20 left open (5 not yet accounted for)');
    // An INFO notice (status, blue) — never the green "success" a completion would show.
    const leftOpenToast = leftOpen.closest('[role="status"]');
    expect(leftOpenToast).not.toBeNull();
    expect(leftOpenToast).toHaveClass('text-fd-blue');
    expect(leftOpenToast).not.toHaveClass('text-fd-green');
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
    // No one-tap scrap fill while Bob is clocked in — the operator keys real scrap.
    expect(screen.queryByTestId('kiosk-complete-report-scrap')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
    key('2');
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));

    expect(screen.getByTestId('kiosk-complete-others')).toHaveTextContent(
      'Bob T still clocked in — they must clock out with their count first'
    );
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
  });

  it('scrap covering a short that the floor no longer has: the op completes, and the scrap is NOT invented', async () => {
    // The overlay saw 8 unaccounted; a crewmate reported the rest meanwhile, so the 6-good
    // clock-out completed the operation on its own. The 2 "scrap" was never sent anywhere.
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'complete' });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-good'));
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    key('6');
    fireEvent.click(screen.getByTestId('kiosk-complete-report-scrap'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    const notice = await screen.findByText(/^Completed WO-2026-0142 with the pieces already recorded — the 2 scrap/);
    expect(notice.closest('[role="alert"]')).not.toBeNull();
    expect(mockedApi.clockOut).toHaveBeenCalledWith(
      501,
      expect.objectContaining({ quantity_produced: 6, quantity_scrapped: 0 })
    );
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('a scrim tap / Escape while the clock-out is in flight does not close the overlay under it', async () => {
    let rejectClockOut: (reason: unknown) => void = () => undefined;
    mockedApi.clockOut.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectClockOut = reject;
        })
    );
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));
    await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' });
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeInTheDocument();

    rejectClockOut({ response: { status: 400, data: { detail: 'Quantity produced exceeds quantity ordered' } } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Quantity produced exceeds quantity ordered');
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

    const failed = await screen.findByText(/^Clocked out, but completing failed: Op 20: 2 of 10 not yet accounted for/);
    // ... and names the way forward: the operator is clocked out, the operation open.
    expect(failed).toHaveTextContent('Op 20 is still open; clock in and Complete again, or ask a supervisor.');
    // An ERROR (alert): the clock-out landed, the close did not.
    expect(failed.closest('[role="alert"]')).toHaveClass('text-fd-red');
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(31, { source: 'kiosk' });
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });
  it('scrap that rode a refused close says it was NOT saved', async () => {
    const detail =
      'Bob T is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.';
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-good'));
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    key('6');
    fireEvent.click(screen.getByTestId('kiosk-complete-report-scrap'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    expect(
      await screen.findByText(
        `Clocked out, but completing failed (the 2 scrap was not saved): ${detail} — Op 20 is still open; clock in and Complete again, or ask a supervisor.`
      )
    ).toBeInTheDocument();
    // The clock-out carried the good only; the 2 scrap rode the refused close.
    expect(mockedApi.clockOut).toHaveBeenCalledWith(
      501,
      expect.objectContaining({ quantity_produced: 6, quantity_scrapped: 0 })
    );
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(
      31,
      expect.objectContaining({ quantity_scrapped: 2, scrap_reason: 'Material defect' })
    );
  });

  /**
   * The clock-out landed and the close was refused on a job the WORK ORDER refuses to
   * start, so the floor cannot clock back in — and the advice must name a way out that
   * exists. HELD: the running operation is closable as it stands from the work order
   * page (never "resume it"). DRAFT: the office offers no Complete on a draft, so "the
   * work order page" alone would dead-end — the supervisor releases first.
   */
  const refusedCloseOn = async (row: Record<string, unknown>) => {
    const detail =
      'Bob T is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.';
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [{ ...ACTIVE_ITEM, ...row }] });
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    return screen.findByText(/^Clocked out, but completing failed: Bob T is still clocked in/);
  };

  it('on a held work order the advice is the work order page — never a clock-in the floor cannot make', async () => {
    const failed = await refusedCloseOn({
      work_order_status: 'on_hold',
      start_blocked_reason: WORK_ORDER_ON_HOLD_REASON,
    });
    expect(failed).toHaveTextContent('Op 20 is still open; ask a supervisor to complete it from the work order page.');
    expect(failed).not.toHaveTextContent(/clock in and Complete again/i);
    // Nor does it send anyone to resume a held job just to close the running work.
    expect(failed).not.toHaveTextContent(/resume/i);
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('on a DRAFT work order the supervisor releases first — "complete it from the work order page" would dead-end', async () => {
    const failed = await refusedCloseOn({
      work_order_status: 'draft',
      start_blocked_reason: WORK_ORDER_NOT_RELEASED_REASON,
    });
    expect(failed).toHaveTextContent(
      'Op 20 is still open; ask a supervisor to release the work order first — then it can be completed here or from the work order page.'
    );
    expect(failed).not.toHaveTextContent(/ask a supervisor to complete it from the work order page/);
    expect(failed).not.toHaveTextContent(/resume/i);
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('a queue row without work_order_status still tells draft from held by the server reason text', async () => {
    const failed = await refusedCloseOn({
      work_order_status: undefined,
      start_blocked_reason: WORK_ORDER_NOT_RELEASED_REASON,
    });
    expect(failed).toHaveTextContent('ask a supervisor to release the work order first');
  });

  it('a work-order refusal it cannot place (terminal) gets generic supervisor advice, no door named', async () => {
    const failed = await refusedCloseOn({
      work_order_status: 'complete',
      start_blocked_reason: 'cannot start operation: work order is complete',
    });
    expect(failed).toHaveTextContent(/— Op 20 is still open; ask a supervisor\.$/);
    expect(failed).not.toHaveTextContent(/work order page|clock in and Complete again|release|resume/i);
  });

  describe('a crewmate reports while the overlay is open', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('the overlay shows a full count, so the replacement scrap rides the clock-out — not a /complete that never runs', async () => {
      mockedApi.clockOut.mockResolvedValue({ operation_status: 'complete' });
      renderKiosk();

      // Opened at 2 of 10 good.
      fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
      // A crewmate reports 4 meanwhile; the 15 s poll brings the live job (6 of 10).
      mockedApi.getMyActiveJob.mockResolvedValue({
        active_jobs: [{ ...ACTIVE_JOB, quantity_complete: 6 }],
        active_job: { ...ACTIVE_JOB, quantity_complete: 6 },
      });
      mockedApi.getWorkCenterQueue.mockResolvedValue({
        queue: [{ ...ACTIVE_ITEM, quantity_complete: 6, quantity_unaccounted: 4 }, NEXT_ITEM],
      });
      const pollsBefore = mockedApi.getMyActiveJob.mock.calls.length;
      await act(async () => {
        jest.advanceTimersByTime(15_000);
      });
      await waitFor(() => expect(mockedApi.getMyActiveJob.mock.calls.length).toBeGreaterThan(pollsBefore));

      // 4 good (the full 10 now) + 1 replacement scrap, with its reason.
      fireEvent.click(screen.getByTestId('kiosk-qty-good'));
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
      key('4');
      fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
      key('1');
      fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
      // The overlay's readout: a full count, nothing short.
      expect(screen.queryByTestId('kiosk-complete-short')).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      expect(await screen.findByText('Completed WO-2026-0142')).toBeInTheDocument();
      // The scrap and its reason rode the clock-out that completed the operation …
      expect(mockedApi.clockOut).toHaveBeenCalledWith(
        501,
        expect.objectContaining({ quantity_produced: 4, quantity_scrapped: 1, scrap_reason: 'Material defect' })
      );
      // … so nothing was parked on a close that never ran, and nothing was dropped.
      expect(mockedApi.completeOperation).not.toHaveBeenCalled();
      expect(screen.queryByText(/scrap you entered was not recorded/)).not.toBeInTheDocument();
    });
  });
});

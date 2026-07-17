import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
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

const QUEUE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
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
  quantity_ordered: 50,
  quantity_complete: 5,
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

function authAs(user: object | null) {
  mockedUseAuth.mockReturnValue({
    user,
    isAuthenticated: !!user,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  });
}

describe('OperatorKiosk', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    authAs({ id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  });

  it('clock-in is two taps and sends source:"kiosk"', async () => {
    mockedApi.clockIn.mockResolvedValue({ id: 501 });
    renderKiosk();

    // Tap 1: the queue card.
    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
    // Tap 2: confirm CLOCK IN.
    fireEvent.click(screen.getByRole('button', { name: /^clock in$/i }));

    await waitFor(() =>
      expect(mockedApi.clockIn).toHaveBeenCalledWith({
        work_order_id: 9,
        operation_id: 31,
        work_center_id: 7,
        entry_type: 'run',
        source: 'kiosk',
      })
    );
  });

  it('surfaces backend gating errors verbatim instead of suppressing them', async () => {
    mockedApi.clockIn.mockRejectedValue({
      response: { data: { detail: 'Previous operations must be completed first' } },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
    fireEvent.click(screen.getByRole('button', { name: /^clock in$/i }));

    expect(await screen.findByText('Previous operations must be completed first')).toBeInTheDocument();
  });

  it('requires a scrap reason before production can be saved, then reports it structurally with source:"kiosk"', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.reportOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));

    // Enter 3 good, 2 scrap on the keypad.
    fireEvent.click(screen.getByTestId('kiosk-key-3'));
    fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
    fireEvent.click(screen.getByTestId('kiosk-key-2'));

    // Scrap entered but no reason chosen → save must be blocked.
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(screen.getByText(/scrap reason — required/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeEnabled();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 2,
        // Structured field, not the old `notes: 'Scrap reason: …'` workaround.
        scrap_reason: 'Material defect',
        source: 'kiosk',
      })
    );
  });

  it('COMPLETE clocks out first (with quantities + source) then completes at the target qty', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete$/i }));
    // GOOD prefills with the remaining quantity (50 - 5 = 45); confirm as-is.
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalled());
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 45,
      quantity_scrapped: 0,
      scrap_reason: undefined,
      source: 'kiosk',
    });
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(31, { quantity_complete: 50, source: 'kiosk' });
    const clockOutOrder = mockedApi.clockOut.mock.invocationCallOrder[0];
    const completeOrder = mockedApi.completeOperation.mock.invocationCallOrder[0];
    expect(clockOutOrder).toBeLessThan(completeOrder);
  });

  it('hold requires choosing a reason and files the matching blocker category', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.holdOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^hold$/i }));
    expect(screen.getByRole('button', { name: /^hold job$/i })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Machine down' }));
    fireEvent.click(screen.getByRole('button', { name: /^hold job$/i }));

    await waitFor(() =>
      expect(mockedApi.holdOperation).toHaveBeenCalledWith(31, {
        category: 'machine_down',
        severity: 'medium',
        source: 'kiosk',
      })
    );
    // A non-Other category already files a blocker on its own — no stub note.
    expect(mockedApi.holdOperation.mock.calls[0][1]).not.toHaveProperty('note');
  });

  it('the catch-all "Other" hold sends a stub note so the backend still files a blocker', async () => {
    // Backend only files a WorkOrderBlocker when the hold carries a note OR a
    // non-OTHER category; a category-only "other" hold would silently skip it.
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.holdOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^hold$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Other' }));
    fireEvent.click(screen.getByRole('button', { name: /^hold job$/i }));

    await waitFor(() =>
      expect(mockedApi.holdOperation).toHaveBeenCalledWith(31, {
        category: 'other',
        severity: 'medium',
        note: 'Other (reported at kiosk)',
        source: 'kiosk',
      })
    );
  });

  it('over-count correction reports the entered quantity + reason with source:"kiosk"', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.reduceOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByTestId('kiosk-active-correct'));

    // Enter 2 to remove on the digits-only keypad. Confirm is blocked until a reason.
    fireEvent.click(screen.getByTestId('kiosk-correct-key-2'));
    expect(screen.getByTestId('kiosk-correct-confirm')).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Double-counted' }));
    fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));

    await waitFor(() =>
      expect(mockedApi.reduceOperationProduction).toHaveBeenCalledWith(31, {
        quantity_delta: 2,
        reason: 'Double-counted',
        source: 'kiosk',
      })
    );
    // The additive report is never called on the correction path.
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
  });

  it('renders the backend refusal verbatim INLINE on the correction screen', async () => {
    const refusal = "Completed work can't be corrected here -- ask a supervisor";
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.reduceOperationProduction.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    renderKiosk();

    fireEvent.click(await screen.findByTestId('kiosk-active-correct'));
    fireEvent.click(screen.getByTestId('kiosk-correct-key-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Scanned twice' }));
    fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));

    // The refusal renders INLINE next to the confirm button as role="alert"
    // (production feedback: a toast alone was unreadable on the floor) and the
    // screen stays open with the entered correction intact for a retry.
    const inline = await screen.findByTestId('kiosk-correct-error');
    expect(inline).toHaveTextContent(refusal);
    expect(inline).toHaveAttribute('role', 'alert');
    expect(screen.getByRole('region', { name: /correct over-count/i })).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-correct-confirm')).toBeEnabled();
  });

  it('says so honestly (with the backend detail verbatim) when clock-out lands but complete is refused', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockRejectedValue({
      response: { data: { detail: 'Final inspection has not been recorded' } },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    expect(
      await screen.findByText('Clocked out, but completing failed: Final inspection has not been recorded')
    ).toBeInTheDocument();
  });

  it('disables Log out while a mutation is in flight', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    let resolveHold: (value: unknown) => void = () => undefined;
    mockedApi.holdOperation.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveHold = resolve;
        })
    );
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^hold$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Machine down' }));
    fireEvent.click(screen.getByRole('button', { name: /^hold job$/i }));

    // Mid-mutation: logging out now would 401 the in-flight write and bounce
    // the tablet off /kiosk.
    expect(screen.getByRole('button', { name: /log out/i })).toBeDisabled();

    resolveHold({});
    await waitFor(() => expect(screen.getByRole('button', { name: /log out/i })).toBeEnabled());
  });

  it('shows the badge login screen when unauthenticated', async () => {
    authAs(null);
    renderKiosk();
    expect(await screen.findByText(/scan your badge/i)).toBeInTheDocument();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
  });

  describe('offline gating (mutationsBlocked = busy || !online)', () => {
    // `online` flips to false when a poll refresh rejects, and to true when it
    // succeeds. To exercise the OFFLINE state with last-known data still on
    // screen, we let the FIRST refresh succeed (populating the active job +
    // queue, online=true), then make subsequent refreshes reject and advance the
    // 15s poll timer so the next tick trips offline while the data is retained.
    const POLL_MS = 15_000;

    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    async function goOffline() {
      // Make the next refresh fail (both calls reject -> Promise.all rejects).
      mockedApi.getWorkCenterQueue.mockRejectedValue(new Error('network down'));
      mockedApi.getMyActiveJob.mockRejectedValue(new Error('network down'));
      // Fire the poll tick and let the rejected refresh settle.
      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });
    }

    it('disables the active-job mutation buttons (Report/Complete/Hold) when offline', async () => {
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
      renderKiosk();

      // Online first: the active-job actions are enabled.
      const report = await screen.findByRole('button', { name: /report production/i });
      expect(report).toBeEnabled();
      expect(screen.getByRole('button', { name: /^complete$/i })).toBeEnabled();
      expect(screen.getByRole('button', { name: /^hold$/i })).toBeEnabled();

      await goOffline();

      // The OFFLINE banner appears and the mutation actions are hard-disabled.
      expect(await screen.findByText(/actions are disabled until the connection is restored/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /report production/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /^complete$/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /^hold$/i })).toBeDisabled();
    });

    it('disables the clock-in confirm button and labels it "Offline" while offline', async () => {
      // No active job: the queue card -> confirm screen is the clock-in path.
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
      renderKiosk();

      // Online: open the confirm screen and verify Clock in is enabled.
      fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
      const clockIn = screen.getByRole('button', { name: /^clock in$/i });
      expect(clockIn).toBeEnabled();

      await goOffline();

      // Same confirm screen (form state retained), but Clock in is now disabled
      // and relabelled "Offline" — the tap can't silently drop the record.
      const offlineBtn = await screen.findByRole('button', { name: /^offline$/i });
      expect(offlineBtn).toBeDisabled();
    });
  });
});

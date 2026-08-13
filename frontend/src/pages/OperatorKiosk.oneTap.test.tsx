/**
 * OperatorKiosk — the one-tap `+1 PIECE` lane on the single-operator REPORT
 * overlay, and the two seams that bank a tapped delta before it can be lost.
 *
 * The lane is owned at PAGE level rather than inside the modal, and that is the
 * whole point: a pending delta is production the operator has already committed
 * to, and a delta sitting in a `setTimeout` inside an unmounting subtree is
 * silently lost production. So every teardown this surface has must be a flush.
 *
 * The IDLE LOGOUT is the sharpest of them, and its ORDER is load-bearing.
 * `logout()` used to run first, which meant the exit flush the view change
 * triggers went out against a cleared session and 401'd — an idle timeout would
 * quietly destroy pieces the operator had tapped and watched themselves commit
 * to. The fix is why `flush()` resolves only once the post has SETTLED: the page
 * banks, and only then drops the credential.
 *
 * This surface deliberately carries NO `keepalive` (axios cannot set it, and
 * reaching around the shared client would post outside the interceptor that
 * keeps the session alive), so the page-unload flush is best-effort here and the
 * guarantees are carried by the two seams below, which both run while the
 * document is still alive.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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

/** 50-piece operation, nothing recorded → the server will take 50 more. */
const QUEUE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
};

/** A DIFFERENT operation on a DIFFERENT work order — job "Y" below. */
const QUEUE_ITEM_Y = {
  operation_id: 32,
  work_order_id: 14,
  work_order_number: 'WO-2026-0199',
  part_number: 'PN-8802',
  part_name: 'Weldment, frame',
  operation_number: '30',
  operation_name: 'Grind',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 40,
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
  quantity_complete: 0,
};

const ACTIVE_JOB_Y = {
  ...ACTIVE_JOB,
  time_entry_id: 502,
  work_order_id: 14,
  operation_id: 32,
  work_order_number: 'WO-2026-0199',
  part_number: 'PN-8802',
  part_name: 'Weldment, frame',
  operation_name: 'Grind',
  operation_number: '30',
  quantity_ordered: 40,
};

/** How the lane labels the pieces Rosa tapped on job X: who, AND which job. */
const ROSA_ON_X_LABEL = 'Rosa Vega · WO-2026-0142 Op 20';

let logout: jest.Mock;

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

/** Open the REPORT PRODUCTION overlay on the clocked-in job. */
async function openReportModal() {
  fireEvent.click(await screen.findByRole('button', { name: /report production/i }));
  await screen.findByTestId('kiosk-onetap');
}

/** Queue card → confirm → Clock in, with the server then reporting `nextJob` active. */
async function clockInTo(workOrderNumber: string, nextJob: typeof ACTIVE_JOB) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(`work order ${workOrderNumber}`, 'i') }));
  await screen.findByRole('region', { name: /confirm clock in/i });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [nextJob], active_job: nextJob });
  fireEvent.click(screen.getByRole('button', { name: /^clock in$/i }));
  await screen.findByRole('button', { name: /report production/i });
}

const lane = () => screen.getByTestId('kiosk-onetap');
const tapAdd = () => fireEvent.click(screen.getByTestId('kiosk-onetap-add'));

/** Every operation id the page has actually reported production against. */
const productionCallOps = () => mockedApi.reportOperationProduction.mock.calls.map(([operationId]) => operationId);

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  logout = jest.fn();
  mockedUseAuth.mockReturnValue({
    user: { id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' },
    isAuthenticated: true,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout,
  });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  mockedApi.reportOperationProduction.mockResolvedValue({});
});

describe('OperatorKiosk — the one-tap lane', () => {
  it('renders on the REPORT overlay and takes +1 out of the quick-add row', async () => {
    renderKiosk();
    await openReportModal();

    expect(lane()).toHaveAttribute('data-phase', 'idle');
    // Parity with the crew station: `+1` means exactly one thing on the screen.
    expect(screen.queryByRole('button', { name: '+1' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+5' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+25' })).toBeInTheDocument();
    // Fixed geometry: UNDO is present from the start, merely dim.
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeDisabled();
  });

  it('holds a tapped piece in the undo window and locks CONFIRM while it is un-banked', async () => {
    renderKiosk();
    await openReportModal();

    tapAdd();
    tapAdd();

    expect(lane()).toHaveAttribute('data-phase', 'pending');
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeEnabled();
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();

    // Exactly one mechanism owns the count at a time.
    const confirm = screen.getByTestId('kiosk-qty-confirm');
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent('Recording 2 pcs…');
  });

  it('undoing inside the window reaches the server never', async () => {
    renderKiosk();
    await openReportModal();

    tapAdd();
    fireEvent.click(screen.getByTestId('kiosk-onetap-undo'));

    expect(lane()).toHaveAttribute('data-phase', 'idle');
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
  });
});

describe('OperatorKiosk — banking a pending delta', () => {
  it('CLOSING the overlay posts the tapped pieces rather than discarding them', async () => {
    renderKiosk();
    await openReportModal();

    tapAdd();
    tapAdd();
    tapAdd();
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 0,
        source: 'kiosk',
      })
    );
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/3 pcs recorded/i)).toBeInTheDocument();
  });

  it('LOG OUT banks BEFORE dropping the credential, so the flush cannot 401 itself', async () => {
    // The order is the property. `logout()` first meant the exit flush went out
    // against a cleared session and an idle timeout quietly destroyed pieces the
    // operator had already committed to.
    renderKiosk();
    await openReportModal();

    tapAdd();
    tapAdd();

    fireEvent.click(screen.getByRole('button', { name: /log out/i }));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 2,
        quantity_scrapped_delta: 0,
        source: 'kiosk',
      })
    );
    await waitFor(() => expect(logout).toHaveBeenCalled());

    // The credential outlived the request that needed it.
    expect(mockedApi.reportOperationProduction.mock.invocationCallOrder[0]).toBeLessThan(
      logout.mock.invocationCallOrder[0]
    );
    // …and the delta went out once, not once per teardown seam.
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1);
  });

  it('logs out normally when nothing was tapped — an untouched screen writes no row', async () => {
    renderKiosk();
    await openReportModal();

    fireEvent.click(screen.getByRole('button', { name: /log out/i }));

    await waitFor(() => expect(logout).toHaveBeenCalled());
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
  });

  it('keeps a refused delta on screen, retryable, and never retries it on its own', async () => {
    mockedApi.reportOperationProduction.mockRejectedValue({
      response: { data: { detail: 'Operation is on hold' } },
    });
    renderKiosk();
    await openReportModal();

    tapAdd();
    tapAdd();
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    // The overlay is gone, but the pieces are not: the refusal is reported
    // verbatim and the count is still the operator's.
    await waitFor(() => expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/2 pcs NOT saved — Operation is on hold/i)).toBeInTheDocument();

    // Reopening shows them still pending, and nothing retried behind the scenes.
    await openReportModal();
    expect(lane()).toHaveAttribute('data-phase', 'failed');
    expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent('Operation is on hold');
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1);
  });
});

describe('OperatorKiosk — a parked delta may NEVER post against the next job', () => {
  /**
   * THE MIS-ATTRIBUTION SEQUENCE on the single-operator station. One person is
   * logged in here, so the operator half of the pair rarely moves — but the
   * OPERATION half moves constantly, and that is enough to make the row wrong in
   * every way that matters:
   *
   *   Rosa taps two finished pieces on WO-2026-0142 Op 20. The post is refused.
   *   The count parks, still hers, still that operation's. She moves on to the
   *   next job in the queue, WO-2026-0199 Op 30, and opens its REPORT overlay —
   *   which is the first moment since the refusal that the station has a live
   *   target to post against.
   *
   * The defect was that the target ref was re-aimed by whatever overlay opened
   * last, so those two pieces would have gone out against Op 30: the actor was
   * right and the work order, part, lot and operation were all wrong. Stock
   * moves on the wrong operation (invariant 6) and the row is permanent.
   *
   * So the assertion is `reportOperationProduction` is never called with job
   * Y's operation id for job X's pieces — and Rosa clocking back into X banks
   * them where they were made.
   */
  beforeEach(() => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM, QUEUE_ITEM_Y] });
    mockedApi.clockIn.mockResolvedValue({});
  });

  /** Tap 2 on job X, have the post definitively refused, land back on the queue. */
  async function parkTwoPiecesOnX() {
    await openReportModal();
    tapAdd();
    tapAdd();
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();

    // A DEFINITIVE refusal: the server answered, so nothing was written and the
    // ambiguity bar is not what holds these pieces — the binding is.
    mockedApi.reportOperationProduction.mockRejectedValueOnce({
      response: { status: 409, data: { detail: 'Operation is on hold' } },
    });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(await screen.findByText(/2 pcs NOT saved — Operation is on hold/i)).toBeInTheDocument();
    expect(productionCallOps()).toEqual([31]);
  }

  it('holds them when the operator moves to another job, and posts nothing against it', async () => {
    renderKiosk();
    await parkTwoPiecesOnX();

    // The next job. Live session, live binding, wrong operation.
    await clockInTo('WO-2026-0199', ACTIVE_JOB_Y);
    await openReportModal();

    // THE ASSERTION. Op 32 never appears — job X's pieces stayed on job X.
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'orphaned'));
    expect(productionCallOps()).toEqual([31]);

    // The lane names the job the pieces were made on, not the one on screen.
    expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(ROSA_ON_X_LABEL);
    expect(screen.getByTestId('kiosk-onetap-add')).toBeDisabled();
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeDisabled();
  });

  it('does not launder them through the exit flush when that overlay closes', async () => {
    // Closing the overlay BANKS — every teardown on this surface is a flush. It
    // must not become the seam that re-aims a held delta onto the open job.
    renderKiosk();
    await parkTwoPiecesOnX();
    await clockInTo('WO-2026-0199', ACTIVE_JOB_Y);
    await openReportModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryByTestId('kiosk-onetap')).not.toBeInTheDocument());
    expect(productionCallOps()).toEqual([31]);
  });

  it('BANKS them against job X once the operator clocks back into it', async () => {
    // A guard that only ever refuses would turn mis-attribution into silent lost
    // production. The pieces are recoverable — on exactly one operation.
    renderKiosk();
    await parkTwoPiecesOnX();

    await clockInTo('WO-2026-0199', ACTIVE_JOB_Y);
    await openReportModal();
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'orphaned'));
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByTestId('kiosk-onetap')).not.toBeInTheDocument());

    await clockInTo('WO-2026-0142', ACTIVE_JOB);
    await openReportModal();
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'pending'));

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(productionCallOps()).toEqual([31, 31]));
    expect(mockedApi.reportOperationProduction).toHaveBeenLastCalledWith(31, {
      quantity_complete_delta: 2,
      quantity_scrapped_delta: 0,
      source: 'kiosk',
    });
  });
});

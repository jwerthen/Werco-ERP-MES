/**
 * Crew-station COMPLETE under owner ruling 1 (2026-11-05): a short count at operation
 * completion is reported as scrap with a reason, or the job stays open.
 *
 * Pinned here:
 *  - the COMPLETE quantity screen pre-fills what is still unaccounted for and goes SHORT
 *    (Continue disabled, "Report n as scrap" offered) when the entry leaves any of it;
 *  - WHERE the final pieces ride depends on the signer: a rostered badge reports good
 *    AND scrap on `/production` (idempotent, request-id'd) and `/complete` carries only
 *    the acknowledgement; a badge with NO entry cannot record good (nothing is sent, and
 *    the screen says which badge to scan — without "JOIN" when the work order refuses a
 *    start, because JOIN is refused there too — and, when nobody is clocked in, the
 *    office way out: the work order page on a held job, release first on a draft), and
 *    its scrap rides `/complete` itself;
 *  - a refusal of a `/complete` that carried the scrap says "Nothing was saved";
 *  - on a POOLED job whose earlier operation already declared the loss
 *    (`upstream_short_uncredited`), the one-tap scrap fill is withheld, as on the kiosk.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';
import type { KioskCrewQueueItem } from '../components/kiosk/kioskConstants';
import { WORK_ORDER_NOT_RELEASED_REASON, WORK_ORDER_ON_HOLD_REASON } from '../components/kiosk/heldOperationFixtures';

jest.mock('../services/kioskStationClient', () => {
  const actual = jest.requireActual('../services/kioskStationClient');
  return {
    __esModule: true,
    ...actual,
    getStationToken: jest.fn(),
    setStationToken: jest.fn(),
    clearStationToken: jest.fn(),
    getStoredStation: jest.fn(),
    stationLogin: jest.fn(),
    getQueue: jest.fn(),
    mintBadgeToken: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportProduction: jest.fn(),
    reduceProduction: jest.fn(),
    completeOperation: jest.fn(),
    holdOperation: jest.fn(),
  };
});

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;

const STATION = {
  id: 3,
  label: 'Weld Bay Kiosk',
  work_center_id: 7,
  work_center_code: 'WELD1',
  work_center_name: 'Weld Bay 1',
};

const ROSTER = [
  {
    time_entry_id: 501,
    user_id: 11,
    operator_name: 'Bob T',
    employee_id: 'E011',
    entry_type: 'run',
    clock_in: '2026-11-07T15:00:00Z',
  },
  {
    time_entry_id: 502,
    user_id: 12,
    operator_name: 'Charlie M',
    employee_id: 'E012',
    entry_type: 'run',
    clock_in: '2026-11-07T16:22:00Z',
  },
];

// 10 target, 4 good + 0 scrap recorded: 6 still unaccounted for.
const ITEM: KioskCrewQueueItem = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 10,
  quantity_complete: 4,
  quantity_scrapped: 0,
  upstream_quantity_short: 0,
  quantity_good_ceiling: 10,
  quantity_unaccounted: 6,
  priority: 5,
  due_date: null,
  roster: ROSTER,
};

const queueWith = (item: KioskCrewQueueItem) => ({
  queue: [item],
  server_time: new Date().toISOString(),
  station: STATION,
});

const BOB_MINT = { access_token: 'op-token-bob', user: { id: 11, full_name: 'Bob T', employee_id: 'E011' } };
const ALICE_MINT = { access_token: 'op-token-alice', user: { id: 13, full_name: 'Alice W', employee_id: 'E013' } };

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

function scanBadge(id: string) {
  id.split('').forEach(key => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

async function openComplete() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
  await screen.findByRole('region', { name: /job detail/i });
  fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));
  await screen.findByTestId('kiosk-qty-good');
}

function key(digits: string) {
  for (const d of digits) fireEvent.click(screen.getByTestId(`kiosk-key-${d}`));
}

/** GOOD 4 of the 6 still unaccounted for, the other 2 reported as scrap with a reason. */
function enterCoveredShort() {
  fireEvent.click(screen.getByTestId('kiosk-key-clear'));
  key('4');
  fireEvent.click(screen.getByTestId('kiosk-qty-report-scrap'));
  fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue(queueWith(ITEM));
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
});

describe('CrewStationKiosk — short count (owner ruling 1)', () => {
  it('pre-fills what is unaccounted for, and disables Continue while the entry leaves a short', async () => {
    renderKiosk();
    await openComplete();

    expect(screen.getByTestId('kiosk-qty-good')).toHaveTextContent('6');
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeEnabled();

    fireEvent.click(screen.getByTestId('kiosk-key-clear'));
    key('4');
    expect(screen.getByTestId('kiosk-qty-short')).toHaveTextContent('Short 2 pcs');
    expect(screen.getByTestId('kiosk-qty-short')).toHaveTextContent('2 of 10 not yet accounted for');
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(screen.getByTestId('kiosk-qty-short-hint')).toHaveTextContent(
      '2 short — report as scrap, or Cancel and have each operator LEAVE with their count'
    );

    // Reporting the 2 as scrap still needs a reason before Continue.
    fireEvent.click(screen.getByTestId('kiosk-qty-report-scrap'));
    expect(screen.getByTestId('kiosk-qty-scrap')).toHaveTextContent('2');
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeEnabled();
  });

  it('a rostered signer: good rides /production, and the scrap covering the short rides /complete', async () => {
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.reportProduction.mockResolvedValue({});
    mocked.completeOperation.mockResolvedValue({ closed_time_entries: [] });
    renderKiosk();
    await openComplete();
    enterCoveredShort();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E011');

    await waitFor(() =>
      expect(mocked.completeOperation).toHaveBeenCalledWith('op-token-bob', 31, {
        source: 'kiosk',
        acknowledge_open_entries: true,
        // The close re-checks this against what is still unaccounted under the row lock
        // (a stale board cannot over-state it), and attributes it to Bob's own entry.
        quantity_scrapped: 2,
        scrap_reason: 'Material defect',
        scrap_reason_code_id: undefined,
      })
    );
    expect(mocked.reportProduction).toHaveBeenCalledWith('op-token-bob', 31, {
      request_id: expect.any(String),
      quantity_complete_delta: 4,
      quantity_scrapped_delta: 0,
      source: 'kiosk',
    });
    expect(mocked.reportProduction.mock.invocationCallOrder[0]).toBeLessThan(
      mocked.completeOperation.mock.invocationCallOrder[0]
    );
  });

  it('replacement scrap on a full good count still rides /production with the pieces', async () => {
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.reportProduction.mockResolvedValue({});
    mocked.completeOperation.mockResolvedValue({ closed_time_entries: [] });
    renderKiosk();
    await openComplete();
    // The prefilled 6 is the full count; 1 scrap on top was replaced.
    fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
    fireEvent.click(screen.getByTestId('kiosk-key-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));
    await screen.findByRole('dialog');
    scanBadge('E011');

    await waitFor(() =>
      expect(mocked.completeOperation).toHaveBeenCalledWith('op-token-bob', 31, {
        source: 'kiosk',
        acknowledge_open_entries: true,
      })
    );
    expect(mocked.reportProduction).toHaveBeenCalledWith(
      'op-token-bob',
      31,
      expect.objectContaining({ quantity_complete_delta: 6, quantity_scrapped_delta: 1 })
    );
  });

  it('a badge with no entry cannot record good: nothing is sent, and it says which badge to scan', async () => {
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    renderKiosk();
    await openComplete();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This badge is not clocked in to this job — scan a badge that is, or JOIN first, to record the 6 good.'
    );
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  it('on a job the work order refuses to start, the off-roster message drops "JOIN" (JOIN is refused there too)', async () => {
    mocked.getQueue.mockResolvedValue(
      queueWith({
        ...ITEM,
        work_order_status: 'on_hold',
        start_blocked_reason: 'Work order is on hold — a supervisor must Resume Work Order first',
      })
    );
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    renderKiosk();
    await openComplete();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'This badge is not clocked in to this job — scan a badge that is clocked in to this job to record the 6 good.'
    );
    expect(alert).not.toHaveTextContent(/JOIN/);
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  /**
   * Nobody is clocked in and JOIN is refused, so there is no floor way to record the good.
   * The advice names the office way out that ACTUALLY exists, which differs by refusal:
   * a held job's running operation is closable as it stands from the work order page
   * (never "once a supervisor resumes it" — a hold is not lifted just to close running
   * work), while a draft offers no Complete in the office at all, so it is released first.
   */
  const nobodyClockedInAlert = async (row: Partial<KioskCrewQueueItem>, enter?: () => void) => {
    mocked.getQueue.mockResolvedValue(queueWith({ ...ITEM, roster: [], ...row }));
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    renderKiosk();
    await openComplete();
    enter?.();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));
    await screen.findByRole('dialog');
    scanBadge('E013');
    return screen.findByRole('alert');
  };

  it('on a HELD job nobody is clocked in to, the office records the good on the held job — never "resumes it"', async () => {
    const alert = await nobodyClockedInAlert({
      work_order_status: 'on_hold',
      start_blocked_reason: WORK_ORDER_ON_HOLD_REASON,
    });
    expect(alert).toHaveTextContent(
      "No one is clocked in to this job, and it can't be joined while it is on hold. Ask a supervisor to record the 6 good from the work order page."
    );
    expect(alert).not.toHaveTextContent(/resume/i);
    expect(alert).not.toHaveTextContent(/scan a badge/);
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  it('on a DRAFT job nobody is clocked in to, the supervisor releases first — the work order page alone would dead-end', async () => {
    const alert = await nobodyClockedInAlert({
      work_order_status: 'draft',
      start_blocked_reason: WORK_ORDER_NOT_RELEASED_REASON,
    });
    expect(alert).toHaveTextContent(
      "No one is clocked in to this job, and it can't be joined while it is in draft. Ask a supervisor to release the work order first — then the 6 good can be recorded here or from the work order page."
    );
    expect(alert).not.toHaveTextContent(/resume/i);
    expect(alert).not.toHaveTextContent(/on hold/);
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  it('scrap entered with the good on a HELD job is named too, and the lead is told nothing was saved', async () => {
    const alert = await nobodyClockedInAlert(
      { work_order_status: 'on_hold', start_blocked_reason: WORK_ORDER_ON_HOLD_REASON },
      enterCoveredShort
    );
    expect(alert).toHaveTextContent(
      "No one is clocked in to this job, and it can't be joined while it is on hold. Ask a supervisor to record the 4 good and 2 scrap from the work order page. Nothing was saved, including the scrap reason."
    );
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  it('scrap entered with the good on a DRAFT job is named too, and the lead is told nothing was saved', async () => {
    const alert = await nobodyClockedInAlert(
      { work_order_status: 'draft', start_blocked_reason: WORK_ORDER_NOT_RELEASED_REASON },
      enterCoveredShort
    );
    expect(alert).toHaveTextContent(
      "No one is clocked in to this job, and it can't be joined while it is in draft. Ask a supervisor to release the work order first — then the 4 good and 2 scrap can be recorded here or from the work order page. Nothing was saved, including the scrap reason."
    );
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
  });

  it('with no scrap entered, the advice says nothing about scrap', async () => {
    const alert = await nobodyClockedInAlert({
      work_order_status: 'on_hold',
      start_blocked_reason: WORK_ORDER_ON_HOLD_REASON,
    });
    expect(alert).not.toHaveTextContent(/scrap|Nothing was saved/);
  });

  it('a row without work_order_status still tells draft from held by the server reason text', async () => {
    const alert = await nobodyClockedInAlert({
      work_order_status: null,
      start_blocked_reason: WORK_ORDER_NOT_RELEASED_REASON,
    });
    expect(alert).toHaveTextContent(/while it is in draft\. Ask a supervisor to release the work order first/);
  });

  it('a refusal it cannot place (terminal) falls back to generic advice — no draft/hold claim, no door named', async () => {
    const alert = await nobodyClockedInAlert({
      work_order_status: 'complete',
      start_blocked_reason: 'cannot start operation: work order is complete',
    });
    expect(alert).toHaveTextContent(
      "No one is clocked in to this job, and it can't be joined right now. Ask a supervisor how to record the 6 good."
    );
    expect(alert).not.toHaveTextContent(/on hold|in draft|work order page|resume|release/i);
  });

  it('a READY job the work order refuses to start: COMPLETE is disabled with the reason (completing would start it)', async () => {
    const reason = 'Work order is on hold — a supervisor must Resume Work Order first';
    mocked.getQueue.mockResolvedValue(
      queueWith({ ...ITEM, status: 'ready', roster: [], work_order_status: 'on_hold', start_blocked_reason: reason })
    );
    renderKiosk();
    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
    await screen.findByRole('region', { name: /job detail/i });
    const verb = screen.getByTestId('crew-complete-verb');
    expect(verb).toBeDisabled();
    expect(verb).toHaveAttribute('aria-describedby', 'crew-complete-blocked');
    expect(screen.getByText(reason, { selector: '#crew-complete-blocked' })).toBeInTheDocument();
  });

  it('a badge with no entry and only scrap: the scrap rides /complete itself, with its reason', async () => {
    // 8 good recorded by the crew; the last 2 were lost and are reported as scrap.
    mocked.getQueue.mockResolvedValue(queueWith({ ...ITEM, quantity_complete: 8, quantity_unaccounted: 2 }));
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.completeOperation.mockResolvedValue({
      closed_time_entries: [
        { time_entry_id: 501, user_id: 11, operator_name: 'Bob T' },
        { time_entry_id: 502, user_id: 12, operator_name: 'Charlie M' },
      ],
    });
    renderKiosk();
    await openComplete();
    fireEvent.click(screen.getByTestId('kiosk-key-clear'));
    fireEvent.click(screen.getByTestId('kiosk-qty-report-scrap'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    await waitFor(() =>
      expect(mocked.completeOperation).toHaveBeenCalledWith('op-token-alice', 31, {
        source: 'kiosk',
        acknowledge_open_entries: true,
        quantity_scrapped: 2,
        scrap_reason: 'Material defect',
        scrap_reason_code_id: undefined,
      })
    );
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    expect(await screen.findByText(/completed WO-2026-0142 — clocked out Bob T, Charlie M/i)).toBeInTheDocument();
  });

  it('a refused /complete that carried the scrap says "Nothing was saved"', async () => {
    mocked.getQueue.mockResolvedValue(queueWith({ ...ITEM, quantity_complete: 8, quantity_unaccounted: 2 }));
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    const detail =
      'Op 20 has only 1 unaccounted; declaring 2 scrap would over-state it. Refresh and re-enter; extra scrap belongs on clock-out or Report production.';
    mocked.completeOperation.mockRejectedValue(new KioskApiError(409, detail, detail));
    renderKiosk();
    await openComplete();
    fireEvent.click(screen.getByTestId('kiosk-key-clear'));
    fireEvent.click(screen.getByTestId('kiosk-qty-report-scrap'));
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    const nothingSaved = await screen.findByText(`Nothing was saved — ${detail}`);
    expect(nothingSaved.closest('[role="alert"]')).toHaveClass('text-fd-red');
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('a refused close after a rostered report landed says the production was saved', async () => {
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.reportProduction.mockResolvedValue({});
    const detail =
      'Charlie M is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.';
    mocked.completeOperation.mockRejectedValue(new KioskApiError(409, detail, detail));
    renderKiosk();
    await openComplete();
    enterCoveredShort();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E011');

    const savedButFailed = await screen.findByText(
      `Saved production, but completing failed (the 2 scrap was not saved): ${detail}`
    );
    expect(savedButFailed.closest('[role="alert"]')).toHaveClass('text-fd-red');
  });
  it('a POOLED job whose earlier operation already declared the loss: no one-tap scrap fill, and why', async () => {
    // 8 good recorded; the 2 still "unaccounted" were closed short at Op 10, but the job is
    // pooled, so they are not credited here — they never reached this operation.
    mocked.getQueue.mockResolvedValue(
      queueWith({ ...ITEM, quantity_complete: 8, quantity_unaccounted: 2, upstream_short_uncredited: 2 })
    );
    renderKiosk();
    await openComplete();
    fireEvent.click(screen.getByTestId('kiosk-key-clear'));

    expect(screen.getByTestId('kiosk-qty-short')).toHaveTextContent('Short 2 pcs');
    expect(screen.queryByTestId('kiosk-qty-report-scrap')).not.toBeInTheDocument();
    expect(screen.getByTestId('kiosk-qty-uncredited')).toHaveTextContent(
      'Do not report pieces you never received as scrap — ask a supervisor to turn on Sequential operations.'
    );
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
  });
});

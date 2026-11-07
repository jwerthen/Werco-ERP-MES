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
 *    start, because JOIN is refused there too), and its scrap rides `/complete` itself;
 *  - a refusal of a `/complete` that carried the scrap says "Nothing was saved".
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';
import type { KioskCrewQueueItem } from '../components/kiosk/kioskConstants';

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

  it('a rostered signer: good AND scrap ride /production, and /complete carries only the acknowledgement', async () => {
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
      })
    );
    expect(mocked.reportProduction).toHaveBeenCalledWith('op-token-bob', 31, {
      request_id: expect.any(String),
      quantity_complete_delta: 4,
      quantity_scrapped_delta: 2,
      scrap_reason: 'Material defect',
      scrap_reason_code_id: undefined,
      source: 'kiosk',
    });
    expect(mocked.reportProduction.mock.invocationCallOrder[0]).toBeLessThan(
      mocked.completeOperation.mock.invocationCallOrder[0]
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

    const savedButFailed = await screen.findByText(`Saved production, but completing failed: ${detail}`);
    expect(savedButFailed.closest('[role="alert"]')).toHaveClass('text-fd-red');
  });
});

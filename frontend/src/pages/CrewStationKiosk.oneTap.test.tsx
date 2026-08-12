/**
 * CrewStationKiosk — REPORT PRODUCTION is BADGE-FIRST, and the one-tap `+1 PIECE`
 * lane it makes possible.
 *
 * THE FLIP. The screen used to take quantities and THEN ask for a badge to sign
 * them. That order cannot deliver one tap per finished part — a signature after
 * the fact is a second action per piece by construction. Now the scan gates
 * ENTRY, and every report made behind it posts under that operator's token. The
 * precedent is already in this file (`stepsSign`→`steps`, `docsSign`→`docs`, both
 * of which write quality records); attribution is unchanged, what changes is that
 * the operator learns who they are recording as BEFORE they enter numbers.
 *
 * THE PROPERTY THAT MATTERS MOST is the last describe block: LEAVING THE SCREEN
 * BANKS A PENDING DELTA. The tap is the commit and the 5s window is only a way
 * out of it — walking away is not one. Cancel, the station lock, the 90s idle
 * flow-reset and the ghost-guard all funnel through a view change, and every one
 * of them must POST rather than discard. A delta dropped here is production an
 * operator watched themselves record, which is worse than never offering the
 * control: the piece exists, the ledger does not know, and nothing on screen
 * ever said so.
 *
 * Harness note: mocked exactly like the sibling CrewStationKiosk suites — the
 * `kioskStationClient` module is stubbed wholesale and the badge arrives as
 * window keydown, the way a wedge scanner sends it.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';

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

const BOB_ROSTER = {
  time_entry_id: 501,
  user_id: 11,
  operator_name: 'Bob T',
  employee_id: 'E011',
  entry_type: 'run',
  clock_in: '2026-07-02T15:00:00Z',
};

/** 50-piece operation with 37 recorded → the server will take 13 more. */
const ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 37,
  quantity_scrapped: 2,
  priority: 5,
  due_date: null,
  roster: [BOB_ROSTER],
};

const BOB_MINT = { access_token: 'op-token-bob', user: { id: 11, full_name: 'Bob T', employee_id: 'E011' } };

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

/** Type a badge on the window (wedge scanner) and hit Enter. */
function scanBadge(id: string) {
  id.split('').forEach((key) => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

async function openJobDetail() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
  await screen.findByRole('region', { name: /job detail/i });
}

/** Board → job detail → REPORT PRODUCTION → badge scan → the quantity screen. */
async function openReportScreenAsBob() {
  await openJobDetail();
  fireEvent.click(screen.getByRole('button', { name: /report production/i }));
  await screen.findByRole('region', { name: /scan badge to report production/i });
  scanBadge('E011');
  await screen.findByTestId('kiosk-onetap');
}

const lane = () => screen.getByTestId('kiosk-onetap');
const tapAdd = () => fireEvent.click(screen.getByTestId('kiosk-onetap-add'));
const goodWell = () => screen.getByTestId('kiosk-qty-good');

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue({ queue: [ITEM], server_time: new Date().toISOString(), station: STATION });
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
  mocked.reportProduction.mockResolvedValue({});
});

describe('CrewStationKiosk — REPORT PRODUCTION is badge-first', () => {
  it('lands on the BADGE screen, not the quantity screen', async () => {
    renderKiosk();
    await openJobDetail();

    fireEvent.click(screen.getByRole('button', { name: /report production/i }));

    expect(await screen.findByRole('region', { name: /scan badge to report production/i })).toBeInTheDocument();
    // Nothing to key yet: no wells, no tally, no lane, no quick adds.
    expect(screen.queryByTestId('kiosk-qty-good')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-tally-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-onetap')).not.toBeInTheDocument();
    // …and the screen says what the scan buys, in the operator's terms.
    expect(screen.getByText(/every piece you report is recorded in your name/i)).toBeInTheDocument();
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('opens the quantity screen in the scanned operator\'s name, carrying the lane', async () => {
    renderKiosk();
    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /report production/i }));
    await screen.findByRole('region', { name: /scan badge to report production/i });

    scanBadge('E011');

    // Titled with who is recording — learned BEFORE any number is entered.
    expect(await screen.findByRole('heading', { name: /report production — bob t/i })).toBeInTheDocument();
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E011');

    // The lane is the primary control on the screen.
    expect(lane()).toBeInTheDocument();
    expect(lane()).toHaveAttribute('data-phase', 'idle');
    expect(screen.getByTestId('kiosk-onetap-add')).toBeEnabled();
    // Fixed geometry holds through the real wiring too.
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeDisabled();

    // The double-count guard is still there…
    expect(screen.getByTestId('kiosk-tally-banner')).toHaveTextContent(
      'CREW TOTAL SO FAR: 37 of 50 · 2 scrap — enter only NEW pieces'
    );
    // …and `+1` has left the quick-add row, so `+1` means one thing on it.
    expect(screen.queryByRole('button', { name: 'Add +1 to good' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add +5 to good' })).toBeInTheDocument();

    // Nothing has been written by opening the screen.
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('posts a keyed entry directly — the badge that opened the screen IS the signature', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    fireEvent.click(screen.getByTestId('kiosk-key-3'));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mocked.reportProduction).toHaveBeenCalledWith('op-token-bob', 31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 0,
        scrap_reason: undefined,
        scrap_reason_code_id: undefined,
        source: 'kiosk',
      })
    );

    // Exactly one report, and NO second signature screen on the way out.
    expect(mocked.reportProduction).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/scan badge to save/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /scan badge to report production/i })).not.toBeInTheDocument();
    // One mint for the whole flow: the entry scan. No re-scan to sign.
    expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(1);

    expect(await screen.findByText(/saved by bob t — crew total now 40 of 50 · 2 scrap/i)).toBeInTheDocument();
  });
});

describe('CrewStationKiosk — the one-tap lane on the report screen', () => {
  it('holds a tapped piece in the undo window instead of posting it straight away', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    tapAdd();

    expect(lane()).toHaveAttribute('data-phase', 'pending');
    expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(/not yet recorded/i);
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeEnabled();
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('takes an un-banked tap out of the keyed row\'s ceiling, so the two cannot together key a 400', async () => {
    // The server refuses `quantity_complete + delta > target` before any
    // mutation. 13 remain; two tapped pieces are already promised, so the keyed
    // row may only offer 11 more.
    renderKiosk();
    await openReportScreenAsBob();

    expect(screen.getByTestId('kiosk-qty-quickadd-label')).toHaveTextContent('Quick add to good · max 13');

    tapAdd();
    tapAdd();

    expect(screen.getByTestId('kiosk-qty-quickadd-label')).toHaveTextContent('Quick add to good · max 11');
    fireEvent.click(screen.getByRole('button', { name: 'Add +25 to good' }));
    expect(within(goodWell()).getByText('11')).toBeInTheDocument();
  });

  it('locks RECORD while a tapped delta is un-banked, so one mechanism owns the count', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    tapAdd();

    const confirm = screen.getByTestId('kiosk-qty-confirm');
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent('Recording 1 pcs…');
  });

  it('undoing inside the window reaches the server never', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    tapAdd();
    tapAdd();
    fireEvent.click(screen.getByTestId('kiosk-onetap-undo'));
    fireEvent.click(screen.getByTestId('kiosk-onetap-undo'));

    expect(lane()).toHaveAttribute('data-phase', 'idle');
    // Nothing pending ⇒ RECORD is the operator's again.
    expect(screen.getByTestId('kiosk-qty-confirm')).toHaveTextContent('Record');
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });
});

describe('CrewStationKiosk — leaving the report screen BANKS the pending delta', () => {
  // The single most important property in the feature. The tap was the commit;
  // the window is only a way out of it, and walking away is not one.

  it('CANCEL posts the tapped pieces rather than discarding them', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    tapAdd();
    tapAdd();
    tapAdd();
    expect(mocked.reportProduction).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() =>
      expect(mocked.reportProduction).toHaveBeenCalledWith(
        'op-token-bob',
        31,
        { quantity_complete_delta: 3, quantity_scrapped_delta: 0, source: 'kiosk' },
        { keepalive: false }
      )
    );
    expect(mocked.reportProduction).toHaveBeenCalledTimes(1);
    // The operator is told, by name, what was saved on their way out.
    expect(await screen.findByText(/3 pcs recorded by bob t/i)).toBeInTheDocument();
  });

  it('LOCK STATION posts them too — locking the tablet is not a way to drop production', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    tapAdd();
    tapAdd();

    fireEvent.click(screen.getByRole('button', { name: /lock station/i }));

    // The post rides the badge-minted OPERATOR token, which the station lock
    // does not touch — so it lands even as the station credential is cleared.
    await waitFor(() =>
      expect(mocked.reportProduction).toHaveBeenCalledWith(
        'op-token-bob',
        31,
        { quantity_complete_delta: 2, quantity_scrapped_delta: 0, source: 'kiosk' },
        { keepalive: false }
      )
    );
    expect(mocked.clearStationToken).toHaveBeenCalled();
  });

  it('banks nothing when there was nothing tapped — an untouched screen writes no row', async () => {
    renderKiosk();
    await openReportScreenAsBob();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await screen.findByRole('region', { name: /job detail/i });
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });
});

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
import { KioskApiError } from '../services/kioskStationClient';
import { addStranded, readStranded } from '../components/kiosk/oneTapStrandedStore';

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

const ANN_ROSTER = {
  time_entry_id: 502,
  user_id: 12,
  operator_name: 'Ann R',
  employee_id: 'E022',
  entry_type: 'run',
  clock_in: '2026-07-02T15:10:00Z',
};

/** A DIFFERENT operation, on a DIFFERENT work order — job "Y" in the tests below. */
const ITEM_Y = {
  operation_id: 32,
  work_order_id: 14,
  work_order_number: 'WO-2026-0199',
  part_number: 'PN-8802',
  part_name: 'Bracket, hinge',
  operation_number: '30',
  operation_name: 'Grind',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 40,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [ANN_ROSTER],
};

const BOB_MINT = { access_token: 'op-token-bob', user: { id: 11, full_name: 'Bob T', employee_id: 'E011' } };
const ANN_MINT = { access_token: 'op-token-ann', user: { id: 12, full_name: 'Ann R', employee_id: 'E022' } };

/** How the lane labels Bob's held pieces: who, AND which job. */
const BOB_ON_X_LABEL = 'Bob T · WO-2026-0142 · Op 20 Weld';

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

/** Board → the named job → REPORT PRODUCTION → badge scan → the quantity screen. */
async function openReportScreenOn(workOrderNumber: string, badgeId: string) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(`work order ${workOrderNumber}`, 'i') }));
  await screen.findByRole('region', { name: /job detail/i });
  fireEvent.click(screen.getByRole('button', { name: /report production/i }));
  await screen.findByRole('region', { name: /scan badge to report production/i });
  scanBadge(badgeId);
  await screen.findByTestId('kiosk-onetap');
}

/** Quantity screen (or job detail) → the crew board, the way an operator walks away. */
async function backToBoard() {
  if (screen.queryByRole('region', { name: /job detail/i }) == null) {
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    await screen.findByRole('region', { name: /job detail/i });
  }
  fireEvent.click(screen.getByRole('button', { name: /back to jobs/i }));
  await screen.findByRole('button', { name: /work order WO-2026-0142/i });
}

const lane = () => screen.getByTestId('kiosk-onetap');
const tapAdd = () => fireEvent.click(screen.getByTestId('kiosk-onetap-add'));
const goodWell = () => screen.getByTestId('kiosk-qty-good');

/** Every (token, operationId) pair the page has actually reported production under. */
const productionCallTargets = () => mocked.reportProduction.mock.calls.map(([token, operationId]) => [token, operationId]);

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
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

describe('CrewStationKiosk — a parked delta may NEVER post under the next operator', () => {
  /**
   * THE MIS-ATTRIBUTION SEQUENCE, end to end on the real page. Every step of it
   * is ordinary shop behaviour, which is exactly why it had to be closed:
   *
   *   Bob scans onto WO-2026-0142 Op 20 and taps two finished pieces. His
   *   5-minute badge token has already expired, so the post 401s and the count
   *   parks. He walks off. The 90-second idle reset (or the Cancel he taps on
   *   his way past) returns the station to the crew board, where nothing on
   *   screen mentions the two pieces. Ann walks up, scans onto a DIFFERENT job —
   *   WO-2026-0199 Op 30 — and the station now has a live, valid credential and
   *   a live binding for the first time since the failure.
   *
   * Without the stamp, that is the moment Bob's two pieces go out: under ANN's
   * token, against ANN's operation. The row is permanent and indistinguishable
   * from a real report — it credits her TimeEntry, lands on another work order's
   * part and lot, and moves stock on the wrong operation (invariant 6).
   *
   * So the assertions below are not "the lane shows an amber box". They are:
   * `reportProduction` is never invoked with Ann's token, and never with Ann's
   * operation id, for Bob's pieces — and the only thing that ever banks them is
   * Bob returning to the same job.
   */
  beforeEach(() => {
    mocked.getQueue.mockResolvedValue({
      queue: [ITEM, ITEM_Y],
      server_time: new Date().toISOString(),
      station: STATION,
    });
    // The badge decides who is minted, the way the server would.
    mocked.mintBadgeToken.mockImplementation((employeeId: string) =>
      Promise.resolve(employeeId === 'E022' ? ANN_MINT : BOB_MINT)
    );
  });

  /** Bob taps 2 pieces on X; the post 401s; the station returns to the board. */
  async function parkBobsTwoPiecesOnX() {
    await openReportScreenOn('WO-2026-0142', 'E011');
    tapAdd();
    tapAdd();
    expect(mocked.reportProduction).not.toHaveBeenCalled();

    // The badge token died during the undo window. Leaving the screen banks —
    // and the bank is refused.
    mocked.reportProduction.mockRejectedValueOnce(
      new KioskApiError(401, null, 'Badge session ended — scan your badge to save these pieces.')
    );
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    // A 401 on an EXIT flush deliberately does NOT hijack the screen — that
    // would drop a scan prompt in front of whoever came next. The count is held
    // and surfaced on the BOARD instead, naming whose it is.
    await backToBoard();
    expect(await screen.findByTestId('crew-held-delta')).toHaveTextContent(BOB_ON_X_LABEL);
    expect(screen.getByTestId('crew-held-delta')).toHaveTextContent('2 pcs tapped but not saved');
  }

  it('holds Bob\'s pieces when ANN scans onto another job, and posts nothing in her name', async () => {
    renderKiosk();
    await parkBobsTwoPiecesOnX();

    // The next person to walk up. Valid credential, live binding, wrong pair.
    await openReportScreenOn('WO-2026-0199', 'E022');

    // THE ASSERTION. Not one report has gone out under Ann's token, and not one
    // has named her operation — the only call on record is Bob's own 401.
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'orphaned'));
    expect(productionCallTargets()).toEqual([['op-token-bob', 31]]);
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E022');

    // The lane says whose pieces are being held, and on which job — and says it
    // beside the name it is currently recording as, so the two cannot be read as
    // one person.
    expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(BOB_ON_X_LABEL);
    expect(screen.getByTestId('kiosk-onetap-operator')).toHaveTextContent(/recording as\s*Ann R/i);

    // Nothing on this screen can SEND them: no tap, no undo, and no third way.
    expect(screen.getByTestId('kiosk-onetap-add')).toBeDisabled();
    expect(screen.getByTestId('kiosk-onetap-undo')).toBeDisabled();
    expect(screen.queryByTestId('kiosk-onetap-retry')).not.toBeInTheDocument();
    // The ONLY enabled control is the write-off — an exit that records the loss
    // rather than one that launders the pieces into Ann's name. Without it the
    // sole way off this state is reloading the tablet, which is the very
    // teardown that used to destroy the count.
    const enabled = within(lane())
      .getAllByRole('button')
      .filter((b) => !(b as HTMLButtonElement).disabled);
    expect(enabled).toHaveLength(1);
    expect(enabled[0]).toHaveAttribute('data-testid', 'kiosk-onetap-writeoff');
    // …and it cannot fire on one tap: writing production off is confirmed,
    // restating the count and whose it is, and it never posts.
    const postsBefore = mocked.reportProduction.mock.calls.length;
    fireEvent.click(enabled[0]);
    expect(await screen.findByTestId('crew-writeoff-confirm')).toBeInTheDocument();
    expect(mocked.reportProduction).toHaveBeenCalledTimes(postsBefore);
  });

  it('still posts nothing in her name when she leaves the screen — the exit flush is not a loophole', async () => {
    // Leaving the report screen BANKS, and that seam runs on every teardown the
    // screen has. It must not become the thing that launders the held delta.
    renderKiosk();
    await parkBobsTwoPiecesOnX();
    await openReportScreenOn('WO-2026-0199', 'E022');

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    await screen.findByRole('region', { name: /job detail/i });

    expect(productionCallTargets()).toEqual([['op-token-bob', 31]]);
  });

  it('BANKS them when BOB scans back onto the same job — held, not lost', async () => {
    // The other half of the property. A guard that only ever refuses would have
    // turned a mis-attribution into silent lost production; the pieces are
    // recoverable, by exactly one pair.
    renderKiosk();
    await parkBobsTwoPiecesOnX();

    // Ann comes and goes without touching them.
    await openReportScreenOn('WO-2026-0199', 'E022');
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'orphaned'));
    await backToBoard();

    // Bob returns to HIS job.
    await openReportScreenOn('WO-2026-0142', 'E011');
    await waitFor(() => expect(lane()).toHaveAttribute('data-phase', 'pending'));
    expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent('2');

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() =>
      expect(mocked.reportProduction).toHaveBeenCalledWith(
        'op-token-bob',
        31,
        { quantity_complete_delta: 2, quantity_scrapped_delta: 0, source: 'kiosk' },
        { keepalive: false }
      )
    );
    // Two calls in the whole run: Bob's 401 and Bob's successful re-bank. Ann's
    // token and her operation appear in neither.
    expect(productionCallTargets()).toEqual([
      ['op-token-bob', 31],
      ['op-token-bob', 31],
    ]);
    expect(await screen.findByText(new RegExp(`2 pcs recorded by ${BOB_ON_X_LABEL}`, 'i'))).toBeInTheDocument();
  });
});

describe('CrewStationKiosk — the stranded-pieces notice on the board', () => {
  /**
   * Pieces a previous session tapped and could never send. This is a NOTICE and
   * not a retry queue: the operator who made them is gone, the endpoint is
   * additive with no idempotency key, and the request that carried them may
   * already have landed. So it names them, names who made them, and offers
   * exactly one action — a human deciding to write them off.
   *
   * It lives on the BOARD rather than inside the report flow because the 90s
   * idle reset used to hide the very thing somebody needed to act on.
   */
  it('names the pieces and the operator, posts nothing, and clears on Dismiss', async () => {
    addStranded('crew', { pieces: 3, key: 'user:11|op:31', label: BOB_ON_X_LABEL });

    renderKiosk();
    // On the board — the screen an unattended station is sitting on.
    await screen.findByRole('button', { name: /work order WO-2026-0142/i });

    const dismiss = await screen.findByRole('button', { name: /dismiss/i });
    const notice = dismiss.parentElement as HTMLElement;
    expect(notice).toHaveTextContent('3 pcs were never saved');
    expect(notice).toHaveTextContent(BOB_ON_X_LABEL);
    // …and it is explicit that these are NOT on the job.
    expect(notice).toHaveTextContent(/they are not on the job/i);
    expect(mocked.reportProduction).not.toHaveBeenCalled();

    fireEvent.click(dismiss);

    // Dismissing destroys the only remaining record that these pieces exist, so
    // it is NOT a one-tap action: the confirmation restates the count and who
    // made them before anything is cleared.
    const confirm = await screen.findByTestId('crew-dismiss-confirm');
    const dialog = confirm.closest('div[class*="p-6"]') as HTMLElement;
    expect(dialog).toHaveTextContent('3 pcs');
    expect(dialog).toHaveTextContent(BOB_ON_X_LABEL);
    expect(readStranded('crew')).toHaveLength(1);

    fireEvent.click(confirm);

    await waitFor(() => expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument());
    // Only then does it clear the record — and it still posts nothing.
    expect(readStranded('crew')).toEqual([]);
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('does not read the single-operator kiosk\'s notices — the two surfaces are separate', async () => {
    addStranded('operator', { pieces: 4, key: 'user:3|op:77', label: 'Rosa Vega · WO-2026-0300 Op 10' });

    renderKiosk();
    await screen.findByRole('button', { name: /work order WO-2026-0142/i });

    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Rosa Vega/)).not.toBeInTheDocument();
  });
});

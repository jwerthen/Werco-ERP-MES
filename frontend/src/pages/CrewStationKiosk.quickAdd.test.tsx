/**
 * CrewStationKiosk — WHICH quantity screens carry the quick-add row.
 *
 * The crew station renders `KioskQuantityScreen` three times (LEAVE, REPORT
 * PRODUCTION, COMPLETE) and the row is opt-in per call site, so "the owner still
 * sees a bare number pad" is a wiring bug that no component test can catch. This
 * file pins the wiring:
 *
 *  - all three GOOD-quantity screens offer it, because all three write the same
 *    additive good quantity against the same operation target;
 *  - each is bounded by that target less what is already recorded — the number
 *    both writers measure their 400 against (`/production`: "Quantity (N) cannot
 *    exceed quantity ordered (T)"; clock-out: "Quantity produced exceeds
 *    quantity ordered");
 *  - LEAVE for a job OUTSIDE this station's queue has no row, because there is
 *    no queue row to derive a ceiling from — no ceiling, no row;
 *  - the over-count CORRECTION screen has none: it removes pieces, and a
 *    walk-back is a deliberate, reasoned entry.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
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

const quickAdd = (label: string) => screen.getByRole('button', { name: `Add ${label} to good` });
const goodWell = () => screen.getByTestId('kiosk-qty-good');

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue({ queue: [ITEM], server_time: new Date().toISOString(), station: STATION });
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
});

describe('CrewStationKiosk quantity quick adds', () => {
  it('REPORT PRODUCTION offers the row, bounded by what is left of the operation target', async () => {
    // The owner's screen: an iPad at the machine, reporting pieces as they come
    // off. Good counts up from zero, so the row does real work here.
    renderKiosk();
    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /report production/i }));

    expect(await screen.findByTestId('kiosk-qty-quickadds')).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-qty-quickadd-label')).toHaveTextContent('Quick add to good · max 13');

    fireEvent.click(quickAdd('+5'));
    expect(within(goodWell()).getByText('5')).toBeInTheDocument();

    // …and it stops where the server would: 5 + 25 clamps to the 13 remaining.
    fireEvent.click(quickAdd('+25'));
    expect(within(goodWell()).getByText('13')).toBeInTheDocument();
    expect(quickAdd('+1')).toBeDisabled();
  });

  it('COMPLETE offers the row at the same ceiling its good field pre-fills to', async () => {
    renderKiosk();
    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));

    // Final-entry good pre-fills at the ceiling, so the row arrives disabled —
    // it is there for rebuilding a cleared count, not for passing the target.
    expect(await screen.findByTestId('kiosk-qty-quickadd-label')).toHaveTextContent('Quick add to good · max 13');
    expect(within(goodWell()).getByText('13')).toBeInTheDocument();
    expect(quickAdd('+1')).toBeDisabled();

    // Clear the field and the row comes alive, still bounded at 13.
    fireEvent.click(goodWell());
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    fireEvent.click(quickAdd('+25'));
    expect(within(goodWell()).getByText('13')).toBeInTheDocument();
  });

  it('LEAVE offers the row for a job on this station queue', async () => {
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    renderKiosk();
    await openJobDetail();

    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    await screen.findByText(/scan badge to join or leave/i);
    scanBadge('E011'); // rostered → LEAVE, not JOIN

    expect(await screen.findByText(/clock out — bob t/i)).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-qty-quickadd-label')).toHaveTextContent('Quick add to good · max 13');

    fireEvent.click(quickAdd('+1'));
    expect(within(goodWell()).getByText('1')).toBeInTheDocument();
  });

  it('LEAVE for a job outside this station queue offers NO row — there is no ceiling to bound it', async () => {
    // Badge-first from the board: the operator sheet lists an open job at
    // another work center, and clock-out from there has no queue row (and so no
    // tally banner either). An unbounded row is worse than no row.
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.getMyActiveJob.mockResolvedValue({
      active_jobs: [
        {
          time_entry_id: 777,
          operation_id: 999, // not in this station's queue
          work_order_number: 'WO-ELSEWHERE',
          operation_name: 'Deburr',
          work_center_name: 'Bench 2',
          clock_in: '2026-07-02T15:00:00Z',
        },
      ],
    });
    renderKiosk();
    await screen.findByRole('button', { name: /WO-2026-0142/i });

    scanBadge('E011');
    fireEvent.click(await screen.findByRole('button', { name: /WO-ELSEWHERE/i }));

    expect(await screen.findByText(/clock out — bob t/i)).toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-qty-quickadds')).not.toBeInTheDocument();
    // The keypad still takes any figure the operator needs.
    expect(screen.getByTestId('kiosk-key-9')).toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-tally-banner')).not.toBeInTheDocument();
  });

  it('the over-count CORRECTION screen has no quick adds — it removes pieces', async () => {
    renderKiosk();
    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-correct-verb'));

    await screen.findByTestId('kiosk-correct-remove');
    expect(screen.queryByTestId('kiosk-qty-quickadds')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Add \+\d+ to good$/ })).not.toBeInTheDocument();
  });
});

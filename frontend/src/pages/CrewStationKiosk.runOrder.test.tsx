/**
 * CrewStationKiosk — the run-order rank survives the trip from the station
 * queue payload to the crew job card.
 *
 * The crew station reads the SAME endpoint as the single-operator kiosk, with a
 * station token, so the rank has to arrive on the crew payload and reach
 * KioskCrewJobCard. The server owns the sort (advisory rank first, then the
 * priority/due-date fallback) — the station renders that order verbatim.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
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

const RANKED = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-RANK-0001',
  part_number: 'PN-1',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [],
  run_order: 1,
};

const SECOND = { ...RANKED, operation_id: 32, work_order_id: 10, work_order_number: 'WO-RANK-0002', run_order: 2 };
const UNRANKED = { ...RANKED, operation_id: 33, work_order_id: 11, work_order_number: 'WO-RANK-0003', run_order: null };

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

describe('CrewStationKiosk run order', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Station already unlocked, so the crew board renders straight away.
    mocked.getStationToken.mockReturnValue('station-token');
    mocked.getStoredStation.mockReturnValue(STATION);
    mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  });

  it('passes the station queue rank through to each crew job card, in server order', async () => {
    mocked.getQueue.mockResolvedValue({
      queue: [RANKED, SECOND, UNRANKED],
      server_time: new Date().toISOString(),
      station: STATION,
    });
    renderKiosk();

    const cards = await screen.findAllByRole('button', { name: /WO-RANK-000/ });
    expect(cards).toHaveLength(3);
    expect(within(cards[0]).getByTestId('kiosk-run-order-chip')).toHaveTextContent('1');
    expect(within(cards[1]).getByTestId('kiosk-run-order-chip')).toHaveTextContent('2');
    expect(within(cards[2]).queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });

  it('renders the server order verbatim rather than sorting by rank', async () => {
    mocked.getQueue.mockResolvedValue({
      queue: [SECOND, RANKED],
      server_time: new Date().toISOString(),
      station: STATION,
    });
    renderKiosk();

    const cards = await screen.findAllByRole('button', { name: /WO-RANK-000/ });
    expect(cards[0]).toHaveTextContent('WO-RANK-0002');
    expect(cards[1]).toHaveTextContent('WO-RANK-0001');
  });
});

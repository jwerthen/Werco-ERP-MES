/**
 * CrewStationKiosk — written guidance on the Job detail screen.
 *
 * This is the screen in the owner's photo. WO-20260807-006 (a 4-op weld
 * assembly) carries its "Unit #" in the WORK ORDER Notes field, and the welder
 * at the crew station could not see it: the job detail rendered the WO number,
 * the part, the op and the crew, and none of the five written-guidance fields.
 *
 * The same screen also read "Op Op 10 · Skid Fit", because the UI hard-coded a
 * literal `Op ` prefix around an operation_number the office had stored as
 * "Op 10". Both are pinned here.
 */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
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

/** WO-20260807-006 as the office actually stored it: operation_number = "Op 10". */
const ITEM: KioskCrewQueueItem = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-20260807-006',
  part_number: 'PN-7731',
  part_name: 'Weldment, skid',
  operation_number: 'Op 10',
  operation_name: 'Skid Fit',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 12,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [],
  work_order_notes: 'Unit #4 — stamp the unit number before it leaves the bay',
  work_order_special_instructions: 'Customer witness required at final',
  operation_description: 'Fit and tack the skid rails',
  operation_setup_instructions: 'Fixture B, 3/16 spacers',
  operation_run_instructions: 'Stitch weld 2 in on 6 in centers',
};

function queueRes(item: KioskCrewQueueItem) {
  return { queue: [item], server_time: new Date().toISOString(), station: STATION };
}

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

async function openJobDetail() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-20260807-006/i }));
  return screen.findByRole('region', { name: /job detail/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue(queueRes(ITEM));
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
});

describe('CrewStationKiosk — job instructions on the job detail screen', () => {
  it('shows all five written-guidance fields, labeled, on the screen the welder opens', async () => {
    mocked.getQueue.mockResolvedValue(queueRes(ITEM));
    renderKiosk();
    const detail = await openJobDetail();

    const notes = within(detail).getByTestId('kiosk-job-notes');
    const expected: [string, string][] = [
      ['Job Notes', 'Unit #4 — stamp the unit number before it leaves the bay'],
      ['Special Instructions', 'Customer witness required at final'],
      ['Operation Detail', 'Fit and tack the skid rails'],
      ['Setup', 'Fixture B, 3/16 spacers'],
      ['Run', 'Stitch weld 2 in on 6 in centers'],
    ];
    expected.forEach(([label, value]) => {
      expect(within(notes).getByText(label)).toBeInTheDocument();
      expect(within(notes).getByText(value)).toBeInTheDocument();
    });
  });

  it('keeps the action verbs reachable — the notes block is what scrolls, not the screen', async () => {
    mocked.getQueue.mockResolvedValue(
      queueRes({
        ...ITEM,
        work_order_notes: Array.from({ length: 80 }, (_, i) => `Unit #${i + 1} — check tag`).join('\n'),
      })
    );
    renderKiosk();
    const detail = await openJobDetail();

    const body = within(detail).getByTestId('kiosk-job-notes-body');
    expect(body.className).toMatch(/overflow-y-auto/);
    expect(body.className).toMatch(/max-h-/);
    // Every verb is still rendered alongside it.
    ['Join / Leave', 'Report production', 'Complete', 'Hold'].forEach((verb) => {
      expect(within(detail).getByRole('button', { name: new RegExp(`^${verb}$`, 'i') })).toBeInTheDocument();
    });
  });

  it('renders no notes container at all for a job with no written guidance', async () => {
    mocked.getQueue.mockResolvedValue(
      queueRes({
        ...ITEM,
        work_order_notes: null,
        work_order_special_instructions: null,
        operation_description: null,
        operation_setup_instructions: null,
        operation_run_instructions: null,
      })
    );
    renderKiosk();
    const detail = await openJobDetail();

    expect(within(detail).queryByTestId('kiosk-job-notes')).not.toBeInTheDocument();
    expect(screen.queryByText('Job Notes')).not.toBeInTheDocument();
    // The screen is otherwise unchanged.
    expect(within(detail).getByTestId('kiosk-job-tally')).toBeInTheDocument();
  });

  it('reads "Op 10 · Skid Fit", not "Op Op 10 · Skid Fit"', async () => {
    renderKiosk();
    const detail = await openJobDetail();

    expect(within(detail).getByText(/Op 10 · Skid Fit/)).toBeInTheDocument();
    expect(within(detail).queryByText(/Op Op/i)).not.toBeInTheDocument();
    expect(detail.textContent).not.toMatch(/Op\s+Op/i);
  });
});

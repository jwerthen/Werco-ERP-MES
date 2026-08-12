/**
 * CrewStationKiosk — a held operation stays on the crew board and can be
 * resumed there, under a badge signature.
 *
 * This is the surface the owner actually uses (an iPad on the floor), and it had
 * the sharper version of the defect: the station could PLACE a hold but had no
 * resume at all — `kioskStationClient` carried `holdOperation` and no twin — so
 * a mis-tap here could only be undone from a desktop.
 *
 * Payloads are shaped like the server's: held rows arrive on their OWN `held`
 * list (never mixed into `queue`), each carrying a NESTED `hold` block.
 *
 * The badge is not ceremony. The station token is honored only by the queue read
 * and the badge mint, so the resuming OPERATOR must be the credential behind the
 * mutation; that is who the audit row names.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import type { KioskCrewQueueItem } from '../components/kiosk/kioskConstants';
import {
  BARE_HOLD,
  CREW_HELD_ROW,
  CREW_QUEUE_ROW,
  UNRECORDED_HOLD,
  crewHeldRowWith,
} from '../components/kiosk/heldOperationFixtures';

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
    resumeOperation: jest.fn(),
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

const BADGE = {
  access_token: 'operator-token',
  user: { id: 12, full_name: 'Rosa Vega', employee_id: 'EMP-4217' },
};

function queuePayload(queue: KioskCrewQueueItem[], held: KioskCrewQueueItem[] = [], heldTruncated = false) {
  return {
    queue,
    held,
    held_truncated: heldTruncated,
    server_time: new Date().toISOString(),
    station: STATION,
  };
}

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

/** Resume → confirm overlay → badge sign screen → scan. */
async function resumeWithBadge() {
  await userEvent.click(await screen.findByTestId('kiosk-held-resume'));
  await screen.findByRole('dialog');
  await userEvent.click(screen.getByTestId('kiosk-resume-confirm'));
  await screen.findByTestId('crew-resume-badge-display');
  await userEvent.keyboard('4217{Enter}');
}

describe('CrewStationKiosk — held operations', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mocked.getStationToken.mockReturnValue('station-token');
    mocked.getStoredStation.mockReturnValue(STATION);
    mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mocked.mintBadgeToken.mockResolvedValue(BADGE);
  });

  it('SHOWS a held operation from the `held` list instead of dropping it', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([CREW_QUEUE_ROW], [CREW_HELD_ROW]));
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(held).toHaveTextContent('WO-HELD-0001');
    expect(within(held).getByTestId('kiosk-held-badge')).toHaveTextContent(/on hold/i);
    expect(screen.getByTestId('crew-held-section')).toBeInTheDocument();
  });

  it('shows why it was held, reading the NESTED hold block', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW]));
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(held).toHaveTextContent('Machine down');
    expect(within(held).getByTestId('kiosk-held-note')).toHaveTextContent('Z-axis alarm 4012');
    expect(within(held).getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
  });

  it('names who stopped a BARE hold — the accidental case, which files no blocker', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [crewHeldRowWith(BARE_HOLD)]));
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(within(held).getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
    expect(within(held).getByTestId('kiosk-held-no-blocker')).toBeInTheDocument();
  });

  it('keeps held work OUT of the joinable board and its job count', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([CREW_QUEUE_ROW], [CREW_HELD_ROW]));
    renderKiosk();

    await screen.findByTestId('kiosk-held-card');
    expect(screen.getByRole('heading', { name: /queue ·/i })).toHaveTextContent('1 job');
    // The job-card label STARTS with "Work order"; anchoring keeps the held
    // card's own "Resume work order …" button from satisfying the query.
    expect(screen.queryByRole('button', { name: /^Work order WO-HELD-0001/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Work order WO-READY-0001/i })).toBeInTheDocument();
  });

  it('says so when the server truncated the held list', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW], true));
    renderKiosk();

    expect(await screen.findByTestId('crew-held-truncated')).toHaveTextContent(/most recent holds only/i);
  });

  it('confirms, then requires a badge signature before resuming', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW]));
    renderKiosk();

    await userEvent.click(await screen.findByTestId('kiosk-held-resume'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByTestId('kiosk-resume-wo')).toHaveTextContent('WO-HELD-0001');
    expect(within(dialog).getByTestId('kiosk-resume-blocker-warning')).toHaveTextContent(/stays recorded/i);

    await userEvent.click(screen.getByTestId('kiosk-resume-confirm'));

    // Confirming opens the signature screen — it does not resume.
    expect(await screen.findByText(/scan badge to resume/i)).toBeInTheDocument();
    expect(mocked.resumeOperation).not.toHaveBeenCalled();
  });

  it('resumes with the badge-minted OPERATOR token, never the station token', async () => {
    mocked.getQueue
      .mockResolvedValueOnce(queuePayload([], [CREW_HELD_ROW]))
      .mockResolvedValue(queuePayload([{ ...CREW_HELD_ROW, status: 'ready', hold: null }], []));
    mocked.resumeOperation.mockResolvedValue({ message: 'Operation resumed', status: 'ready', open_blockers: [] });
    renderKiosk();

    await resumeWithBadge();

    await waitFor(() => expect(mocked.mintBadgeToken).toHaveBeenCalled());
    await waitFor(() => expect(mocked.resumeOperation).toHaveBeenCalledWith('operator-token', 41));
    await waitFor(() => expect(screen.queryByTestId('kiosk-held-card')).not.toBeInTheDocument());
  });

  it('does not let the ghost-guard bounce the resume flow off a held row', async () => {
    // Held rows are NOT on `queue`; a guard that only checked `queue` would
    // reset straight to the board and the resume screens would be unreachable.
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW]));
    renderKiosk();

    await userEvent.click(await screen.findByTestId('kiosk-held-resume'));
    await userEvent.click(await screen.findByTestId('kiosk-resume-confirm'));

    const display = await screen.findByTestId('crew-resume-badge-display');
    // Survive a poll cycle — the guard runs on every queue update.
    await waitFor(() => expect(mocked.getQueue).toHaveBeenCalled());
    expect(display).toBeInTheDocument();
  });

  it('surfaces the STILL-OPEN blocker on its own screen after a successful resume', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW]));
    mocked.resumeOperation.mockResolvedValue({
      message: 'Operation resumed',
      status: 'in_progress',
      open_blockers: [
        { id: 5, title: 'Machine Down: OP20 Deburr', category: 'machine_down', severity: 'high', status: 'open' },
      ],
    });
    renderKiosk();

    await resumeWithBadge();

    expect(await screen.findByText('Machine Down: OP20 Deburr')).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-blocker-open-done')).toHaveTextContent('Back to board');
  });

  it('renders a server refusal VERBATIM and inline on the sign screen', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [CREW_HELD_ROW]));
    mocked.resumeOperation.mockRejectedValue({
      response: { data: { detail: 'Operation is not on hold' } },
    });
    renderKiosk();

    await resumeWithBadge();

    // Inline, not a toast: a toast alone proved unreadable on the floor.
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Operation is not on hold');
    // Non-optimistic: the sign screen stays up so the right badge can re-scan.
    expect(screen.getByTestId('crew-resume-badge-display')).toBeInTheDocument();
  });

  it('does not offer a held job in the badge-first "join a job" list', async () => {
    // The server refuses a clock-in on a held op; offering it here would put
    // back the tap-and-get-refused hole the ON HOLD section exists to close.
    mocked.getQueue.mockResolvedValue(queuePayload([CREW_QUEUE_ROW], [CREW_HELD_ROW]));
    renderKiosk();

    await screen.findByTestId('kiosk-held-card');
    await userEvent.click(screen.getByRole('button', { name: /my jobs/i }));
    await userEvent.keyboard('4217{Enter}');

    const joinHeading = await screen.findByText(/join a job at this station/i);
    const section = joinHeading.parentElement as HTMLElement;
    expect(within(section).getByText('WO-READY-0001')).toBeInTheDocument();
    expect(within(section).queryByText('WO-HELD-0001')).not.toBeInTheDocument();
  });

  it('renders a held row whose hold recorded nothing at all, rather than hiding it', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload([], [crewHeldRowWith(UNRECORDED_HOLD)]));
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(within(held).getByTestId('kiosk-held-no-reason')).toBeInTheDocument();
    expect(within(held).getByTestId('kiosk-held-resume')).toBeEnabled();
  });

  it('tolerates a backend that sends no `held` key at all', async () => {
    mocked.getQueue.mockResolvedValue({
      queue: [CREW_QUEUE_ROW],
      server_time: new Date().toISOString(),
      station: STATION,
    });
    renderKiosk();

    await screen.findByRole('button', { name: /^Work order WO-READY-0001/i });
    expect(screen.queryByTestId('crew-held-section')).not.toBeInTheDocument();
  });
});

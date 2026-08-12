/**
 * OperatorKiosk — a held operation stays visible and can be resumed there.
 *
 * The production defect: an operator put an operation ON HOLD by accident and it
 * disappeared from every screen the kiosk offers, because the queue renders
 * READY/IN_PROGRESS work. `resumeOperation` had exactly one call site in the
 * whole app — a desktop page — so recovery meant leaving the machine.
 *
 * Payloads here are shaped like the server's: held rows arrive on their OWN
 * `held` list (never mixed into `queue`), each carrying a NESTED `hold` block.
 * An earlier version of this suite put held rows in `queue` with a flat
 * `blocker` field and passed green against a payload the server never sends.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import {
  BARE_HOLD,
  HELD_ROW,
  QUEUE_ROW,
  UNRECORDED_HOLD,
  heldRowWith,
} from '../components/kiosk/heldOperationFixtures';

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
    resumeOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

/** Tap Resume on the held card and confirm the overlay. */
async function resumeHeldJob() {
  await userEvent.click(await screen.findByTestId('kiosk-held-resume'));
  await screen.findByRole('dialog');
  await userEvent.click(screen.getByTestId('kiosk-resume-confirm'));
}

describe('OperatorKiosk — held operations', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedUseAuth.mockReturnValue({
      user: { id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' },
      isAuthenticated: true,
      isLoading: false,
      loginWithEmployeeId: jest.fn(),
      logout: jest.fn(),
    });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
  });

  it('SHOWS a held operation from the `held` list instead of dropping it', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ROW], held: [HELD_ROW] });
    renderKiosk();

    // The regression this whole change exists for.
    const held = await screen.findByTestId('kiosk-held-card');
    expect(held).toHaveTextContent('WO-HELD-0001');
    expect(within(held).getByTestId('kiosk-held-badge')).toHaveTextContent(/on hold/i);
  });

  it('shows why it was held, reading the NESTED hold block', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW] });
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(held).toHaveTextContent('Machine down');
    expect(within(held).getByTestId('kiosk-held-note')).toHaveTextContent('Z-axis alarm 4012');
    expect(within(held).getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
  });

  it('names who stopped a BARE hold — the accidental case, which files no blocker', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [heldRowWith(BARE_HOLD)] });
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(within(held).getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
    expect(within(held).getByTestId('kiosk-held-no-blocker')).toBeInTheDocument();
  });

  it('keeps held work OUT of the startable queue and out of the job count', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ROW], held: [HELD_ROW] });
    renderKiosk();

    await screen.findByTestId('kiosk-held-card');
    expect(screen.getByRole('heading', { name: /My queue/i })).toHaveTextContent('1 job');
    expect(screen.getByRole('heading', { name: /On hold/i })).toHaveTextContent('1 job');
    // The queue card's label STARTS with "Work order"; anchoring keeps the held
    // card's own "Resume work order …" button from satisfying the query.
    expect(screen.queryByRole('button', { name: /^Work order WO-HELD-0001/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Work order WO-READY-0001/i })).toBeInTheDocument();
  });

  it('says so when the server truncated the held list, rather than showing a silent subset', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW], held_truncated: true });
    renderKiosk();

    expect(await screen.findByTestId('kiosk-held-truncated')).toHaveTextContent(/most recent holds only/i);
  });

  it('confirms before resuming, restating the job and the hold that stays open', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW] });
    renderKiosk();

    await userEvent.click(await screen.findByTestId('kiosk-held-resume'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByTestId('kiosk-resume-wo')).toHaveTextContent('WO-HELD-0001');
    expect(within(dialog).getByTestId('kiosk-resume-blocker-warning')).toHaveTextContent(/stays recorded/i);
    // Nothing has been sent yet — the tap opens a confirm, it does not resume.
    expect(mockedApi.resumeOperation).not.toHaveBeenCalled();
  });

  it('resumes only after the confirm, and refetches so the row returns as startable', async () => {
    mockedApi.getWorkCenterQueue
      .mockResolvedValueOnce({ queue: [], held: [HELD_ROW] })
      .mockResolvedValue({ queue: [{ ...HELD_ROW, status: 'ready', hold: null }], held: [] });
    mockedApi.resumeOperation.mockResolvedValue({ message: 'Operation resumed', status: 'ready', open_blockers: [] });
    renderKiosk();

    await resumeHeldJob();

    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalledWith(41));
    // The held card is gone and the job is back on the startable queue.
    await waitFor(() => expect(screen.queryByTestId('kiosk-held-card')).not.toBeInTheDocument());
    expect(await screen.findByRole('button', { name: /^Work order WO-HELD-0001/i })).toBeInTheDocument();
  });

  it('surfaces the STILL-OPEN blocker on its own screen after a successful resume', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW] });
    mockedApi.resumeOperation.mockResolvedValue({
      message: 'Operation resumed',
      status: 'in_progress',
      open_blockers: [
        { id: 5, title: 'Machine Down: OP20 Deburr', category: 'machine_down', severity: 'high', status: 'open' },
      ],
    });
    renderKiosk();

    await resumeHeldJob();

    // Verbatim server text, on a screen that needs an explicit tap to leave —
    // a 3s toast would let a live stop read as cleared.
    expect(await screen.findByText('Machine Down: OP20 Deburr')).toBeInTheDocument();
    expect(screen.getByText(/hold still open/i)).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-blocker-open-done')).toBeInTheDocument();
  });

  it('goes straight back to the queue when the resume left nothing open', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW] });
    mockedApi.resumeOperation.mockResolvedValue({ message: 'Operation resumed', status: 'ready', open_blockers: [] });
    renderKiosk();

    await resumeHeldJob();

    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalled());
    expect(screen.queryByTestId('kiosk-open-blockers')).not.toBeInTheDocument();
    expect(await screen.findByText(/WO-HELD-0001 resumed/i)).toBeInTheDocument();
  });

  it('surfaces a server refusal VERBATIM and stays non-optimistic', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [HELD_ROW] });
    mockedApi.resumeOperation.mockRejectedValue({
      response: { data: { detail: 'Operation is not on hold' } },
    });
    renderKiosk();

    await resumeHeldJob();

    // The server's own words, not a reworded kiosk message.
    expect(await screen.findByText('Operation is not on hold')).toBeInTheDocument();
    // Nothing moved: the job is still shown as held, because that is all the
    // server has confirmed.
    expect(screen.getByTestId('kiosk-held-card')).toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-open-blockers')).not.toBeInTheDocument();
  });

  it('renders a held row whose hold recorded nothing at all, rather than hiding it', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [], held: [heldRowWith(UNRECORDED_HOLD)] });
    renderKiosk();

    const held = await screen.findByTestId('kiosk-held-card');
    expect(within(held).getByTestId('kiosk-held-no-reason')).toBeInTheDocument();
    expect(within(held).getByTestId('kiosk-held-resume')).toBeEnabled();
  });

  it('tolerates a backend that sends no `held` key at all', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ROW] });
    renderKiosk();

    await screen.findByRole('button', { name: /^Work order WO-READY-0001/i });
    expect(screen.queryByTestId('kiosk-held-section')).not.toBeInTheDocument();
  });
});

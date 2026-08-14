/**
 * OperatorKiosk — written guidance on both single-operation surfaces.
 *
 * Two surfaces, and the SECOND is the one that matters in practice: the
 * CLOCK IN? confirm card is read for a few seconds, the running-job hero is
 * what an operator looks at for hours afterwards. A note that vanishes at
 * clock-in is useless, so the transition is pinned explicitly.
 *
 * The queue read and the my-active-job read are different endpoints; these
 * fixtures carry the five keys under the SAME names in both, which is the
 * contract that lets one component serve both surfaces.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import type { KioskJobInstructions } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    getScrapReasonCodes: jest.fn(),
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

const NOTES: KioskJobInstructions = {
  work_order_notes: 'Unit #4 — stamp the unit number before it leaves the bay',
  work_order_special_instructions: 'Customer witness required at final',
  operation_description: 'Fit and tack the skid rails',
  operation_setup_instructions: 'Fixture B, 3/16 spacers',
  operation_run_instructions: 'Stitch weld 2 in on 6 in centers',
};

const LABELLED: [string, string][] = [
  ['Job Notes', NOTES.work_order_notes as string],
  ['Special Instructions', NOTES.work_order_special_instructions as string],
  ['Operation Detail', NOTES.operation_description as string],
  ['Setup', NOTES.operation_setup_instructions as string],
  ['Run', NOTES.operation_run_instructions as string],
];

/** Queue row, with operation_number stored the way the office typed it. */
const QUEUE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-20260807-006',
  part_number: 'PN-7731',
  part_name: 'Weldment, skid',
  operation_number: 'Op 10',
  operation_name: 'Skid Fit',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
  ...NOTES,
};

/** my-active-job shape — the SAME five keys, per the backend contract. */
const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-20260807-006',
  part_number: 'PN-7731',
  part_name: 'Weldment, skid',
  operation_name: 'Skid Fit',
  operation_number: 'Op 10',
  quantity_ordered: 50,
  quantity_complete: 0,
  ...NOTES,
};

function stripNotes<T extends KioskJobInstructions>(job: T): T {
  return {
    ...job,
    work_order_notes: null,
    work_order_special_instructions: null,
    operation_description: null,
    operation_setup_instructions: null,
    operation_run_instructions: null,
  };
}

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=WELD1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

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
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
});

describe('OperatorKiosk — job instructions on the CLOCK IN? card', () => {
  it('shows all five written-guidance fields, labeled, before the operator commits', async () => {
    renderKiosk();
    fireEvent.click(await screen.findByRole('button', { name: /WO-20260807-006/i }));

    const confirm = await screen.findByRole('region', { name: /confirm clock in/i });
    const notes = within(confirm).getByTestId('kiosk-job-notes');
    LABELLED.forEach(([label, value]) => {
      expect(within(notes).getByText(label)).toBeInTheDocument();
      expect(within(notes).getByText(value)).toBeInTheDocument();
    });
    // The commit button is still right there next to it.
    expect(within(confirm).getByRole('button', { name: /^clock in$/i })).toBeInTheDocument();
  });

  it('renders no notes container on a job with no written guidance', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [stripNotes(QUEUE_ITEM)] });
    renderKiosk();
    fireEvent.click(await screen.findByRole('button', { name: /WO-20260807-006/i }));

    const confirm = await screen.findByRole('region', { name: /confirm clock in/i });
    expect(within(confirm).queryByTestId('kiosk-job-notes')).not.toBeInTheDocument();
    expect(screen.queryByText('Job Notes')).not.toBeInTheDocument();
  });

  it('reads "Op 10 · Skid Fit", not "Op Op 10 · Skid Fit"', async () => {
    renderKiosk();
    fireEvent.click(await screen.findByRole('button', { name: /WO-20260807-006/i }));

    const confirm = await screen.findByRole('region', { name: /confirm clock in/i });
    expect(within(confirm).getByText(/Op 10 · Skid Fit/)).toBeInTheDocument();
    expect(confirm.textContent).not.toMatch(/Op\s+Op/i);
  });
});

describe('OperatorKiosk — job instructions on the running-job hero', () => {
  it('survives the clock-in transition: the note the operator read is still on screen after starting', async () => {
    mockedApi.clockIn.mockResolvedValue({ id: 501 });
    renderKiosk();

    // Confirm card first — the note is there.
    fireEvent.click(await screen.findByRole('button', { name: /WO-20260807-006/i }));
    const confirm = await screen.findByRole('region', { name: /confirm clock in/i });
    expect(within(confirm).getByText(NOTES.work_order_notes as string)).toBeInTheDocument();

    // Clock in: the next my-active-job read returns the running job.
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    fireEvent.click(within(confirm).getByRole('button', { name: /^clock in$/i }));
    await waitFor(() => expect(mockedApi.clockIn).toHaveBeenCalled());

    // ... and it is STILL there, on the screen the operator now looks at for hours.
    const hero = await screen.findByRole('region', { name: /active job/i });
    const notes = await within(hero).findByTestId('kiosk-job-notes');
    LABELLED.forEach(([label, value]) => {
      expect(within(notes).getByText(label)).toBeInTheDocument();
      expect(within(notes).getByText(value)).toBeInTheDocument();
    });
  });

  it('caps the notes so REPORT PRODUCTION / COMPLETE / HOLD are never pushed off the bottom', async () => {
    const wall = Array.from({ length: 80 }, (_, i) => `Unit #${i + 1} — check tag`).join('\n');
    mockedApi.getMyActiveJob.mockResolvedValue({
      active_jobs: [{ ...ACTIVE_JOB, work_order_notes: wall }],
      active_job: { ...ACTIVE_JOB, work_order_notes: wall },
    });
    renderKiosk();

    const hero = await screen.findByRole('region', { name: /active job/i });
    const body = await within(hero).findByTestId('kiosk-job-notes-body');
    expect(body.className).toMatch(/overflow-y-auto/);
    expect(body.className).toMatch(/max-h-/);

    ['Report production', 'Complete op', 'Hold'].forEach((verb) => {
      expect(within(hero).getByRole('button', { name: new RegExp(`^${verb}$`, 'i') })).toBeInTheDocument();
    });
  });

  it('renders no notes container on a running job with no written guidance', async () => {
    const bare = stripNotes(ACTIVE_JOB);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [bare], active_job: bare });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [stripNotes(QUEUE_ITEM)] });
    renderKiosk();

    const hero = await screen.findByRole('region', { name: /active job/i });
    await within(hero).findByTestId('kiosk-active-timer');
    expect(within(hero).queryByTestId('kiosk-job-notes')).not.toBeInTheDocument();
  });

  it('reads "Op 10 · Skid Fit" in the hero header, not "Op Op 10"', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    renderKiosk();

    const hero = await screen.findByRole('region', { name: /active job/i });
    await within(hero).findByTestId('kiosk-active-timer');
    expect(within(hero).getByText(/Op 10 · Skid Fit/)).toBeInTheDocument();
    expect(hero.textContent).not.toMatch(/Op\s+Op/i);
  });
});

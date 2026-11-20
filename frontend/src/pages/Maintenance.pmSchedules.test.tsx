/**
 * Maintenance page — PM schedules drive work orders, and a completed PM moves its schedule on.
 *
 *  - "Start PM" (Schedules tab row, Upcoming card) opens the work-order form pre-filled from the
 *    schedule and sends its `schedule_id` with NO target, so the server inherits the schedule's
 *    target and completing the work order advances the schedule's next due date.
 *  - A schedule that already has an open work order names it instead of offering Start PM.
 *  - Completing a PM work order ticks its checklist and reports the schedule's new due date. With
 *    any item left unticked the dialog asks for a reason (the server refuses 409 without one) and
 *    sends it, trimmed — only then.
 *  - The Complete dialog is built from a fresh read of the work order (not the cached list row),
 *    opens with the work order's notes (which completion REPLACES — sent only when changed, null
 *    when emptied), and re-reads the work order after a 409 so a checklist that changed underneath
 *    it can be answered instead of being a dead end; a 400 re-reads the page.
 *  - "Edit" opens the schedule dialog and PUTs only the fields that changed (never a target).
 *  - The Overdue / Due This Week tiles add the PM schedules nobody has started, or whose work order
 *    is on hold.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Maintenance from './Maintenance';
import type { MaintenanceSchedule, MaintenanceWorkOrder } from '../types/maintenance';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaintenanceDashboard: jest.fn(),
    getMaintenanceSchedules: jest.fn(),
    getMaintenanceWorkOrders: jest.fn(),
    getMaintenanceWorkOrder: jest.fn(),
    getMaintenanceAssets: jest.fn(),
    getWorkCenters: jest.fn(),
    createMaintenanceWorkOrder: jest.fn(),
    completeMaintenanceWorkOrder: jest.fn(),
    updateMaintenanceSchedule: jest.fn(),
  },
}));
let mockRole = 'supervisor';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole }, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const millPm: MaintenanceSchedule = {
  id: 7,
  name: 'Mill 1 Monthly PM',
  description: 'Monthly upkeep',
  maintenance_type: 'preventive',
  frequency: 'monthly',
  frequency_days: null,
  estimated_duration_hours: 2,
  priority: 'high',
  checklist: '["Lubricate ways","Check coolant"]',
  checklist_items: ['Lubricate ways', 'Check coolant'],
  requires_shutdown: true,
  is_active: true,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'MILL-1',
  target_name: 'Synthetic Mill',
  next_due_date: '2026-12-01',
  last_completed_date: null,
};

const compressorPm: MaintenanceSchedule = {
  ...millPm,
  id: 8,
  name: 'Compressor Annual PM',
  frequency: 'annual',
  priority: 'medium',
  requires_shutdown: false,
  checklist: null,
  checklist_items: [],
  work_center_id: null,
  asset_id: 5,
  target_type: 'asset',
  target_code: 'AC-1',
  target_name: 'Air Compressor 1',
};

const openPmWO: MaintenanceWorkOrder = {
  id: 41,
  schedule_id: 8,
  wo_number: 'MWO-2026-0041',
  title: 'Compressor Annual PM',
  maintenance_type: 'preventive',
  priority: 'medium',
  status: 'in_progress',
  requires_shutdown: false,
  work_center_id: null,
  asset_id: 5,
  target_type: 'asset',
  target_code: 'AC-1',
  target_name: 'Air Compressor 1',
  checklist_results: JSON.stringify([
    { item: 'Drain tank', done: false },
    { item: 'Check belt', done: false },
  ]),
};

const renderPage = (path: string) =>
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={[path]}>
        <Maintenance />
      </MemoryRouter>
    </ToastProvider>
  );

const scheduleRow = async (name: string) => {
  const table = await screen.findByTestId('data-table');
  await waitFor(() => expect(within(table).getByText(name)).toBeInTheDocument());
  return within(table).getByText(name).closest('tr') as HTMLElement;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'supervisor';
  mockedApi.getMaintenanceDashboard.mockResolvedValue({
    overdue_count: 1,
    due_this_week: 2,
    schedules_overdue: 30,
    schedules_due_this_week: 0,
    completed_this_month: 0,
    completion_rate: 0,
    total_cost_month: 0,
    in_progress: 1,
    work_center_metrics: [],
  });
  mockedApi.getMaintenanceSchedules.mockResolvedValue([millPm, compressorPm]);
  mockedApi.getMaintenanceWorkOrders.mockResolvedValue([openPmWO]);
  mockedApi.getMaintenanceAssets.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([] as any);
  mockedApi.createMaintenanceWorkOrder.mockResolvedValue({ id: 50, wo_number: 'MWO-2026-0050' });
  mockedApi.completeMaintenanceWorkOrder.mockResolvedValue({
    ...openPmWO,
    status: 'completed',
    schedule_next_due_date: '2027-11-16',
  });
  mockedApi.updateMaintenanceSchedule.mockResolvedValue(millPm);
  // The Complete dialog's fresh read answers with the row the list serves, unless a test says
  // otherwise. Calling the list mock's implementation directly records no list call.
  mockedApi.getMaintenanceWorkOrder.mockImplementation(async (id: number) => {
    const listRows = mockedApi.getMaintenanceWorkOrders.getMockImplementation();
    const rows: MaintenanceWorkOrder[] = listRows ? await listRows() : [];
    const row = rows.find(wo => wo.id === id);
    if (!row) throw new Error(`no work order ${id}`);
    return row;
  });
});

describe('Maintenance — Start PM from a schedule', () => {
  it('opens the work-order form pre-filled from the schedule and sends schedule_id with no target', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    fireEvent.click(within(row).getByRole('button', { name: 'Start PM work order for Mill 1 Monthly PM' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Start PM: Mill 1 Monthly PM');
    // The target is the schedule's: shown, never picked.
    const panel = within(dialog).getByRole('region', { name: 'PM schedule' });
    expect(panel).toHaveTextContent('MILL-1 · Synthetic Mill');
    expect(panel).toHaveTextContent('Monthly');
    expect(panel).toHaveTextContent('2h');
    expect(within(dialog).queryByRole('radio', { name: 'Work center' })).not.toBeInTheDocument();

    expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('Mill 1 Monthly PM');
    expect(within(dialog).getByLabelText('Type')).toHaveValue('preventive');
    expect(within(dialog).getByLabelText('Priority')).toHaveValue('high');
    expect(within(dialog).getByLabelText('Due Date')).toHaveValue('2026-12-01');
    expect(within(dialog).getByRole('checkbox', { name: 'Requires machine shutdown' })).toBeChecked();
    expect(within(dialog).getByLabelText(/^Checklist/)).toHaveValue('Lubricate ways\nCheck coolant');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(mockedApi.createMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createMaintenanceWorkOrder.mock.calls[0][0];
    expect(payload).toMatchObject({
      schedule_id: 7,
      title: 'Mill 1 Monthly PM',
      maintenance_type: 'preventive',
      priority: 'high',
      due_date: '2026-12-01',
      requires_shutdown: true,
      description: 'Monthly upkeep',
    });
    expect(payload).not.toHaveProperty('work_center_id');
    expect(payload).not.toHaveProperty('asset_id');
    expect(JSON.parse(payload.checklist_results)).toEqual([
      { item: 'Lubricate ways', done: false },
      { item: 'Check coolant', done: false },
    ]);
    expect(await screen.findByText(/MWO-2026-0050 opened for Mill 1 Monthly PM/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('names the open work order instead of offering a second Start PM', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Compressor Annual PM');
    expect(within(row).getByText('MWO-2026-0041 open')).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: /Start PM/ })).not.toBeInTheDocument();
  });

  it('starts a PM from an Upcoming card on the dashboard tab', async () => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([]);
    renderPage('/maintenance?tab=dashboard');
    fireEvent.click(await screen.findByRole('button', { name: 'Start PM work order for Compressor Annual PM' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Start PM: Compressor Annual PM');
    expect(within(dialog).getByRole('region', { name: 'PM schedule' })).toHaveTextContent('AC-1 · Air Compressor 1');
    expect(dialog).toHaveTextContent('Equipment assets never record machine downtime.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(mockedApi.createMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    expect(mockedApi.createMaintenanceWorkOrder.mock.calls[0][0]).toMatchObject({ schedule_id: 8 });
  });

  it('offers neither Start PM nor Edit to an operator (the server refuses them)', async () => {
    mockRole = 'operator';
    renderPage('/maintenance?tab=schedules');
    await scheduleRow('Mill 1 Monthly PM');
    expect(screen.queryByRole('button', { name: /Start PM work order/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit schedule/ })).not.toBeInTheDocument();
  });

  it('offers no Start PM on the Upcoming cards to an operator', async () => {
    mockRole = 'operator';
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([]);
    renderPage('/maintenance?tab=dashboard');
    expect(await screen.findByText('Mill 1 Monthly PM')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Start PM work order/ })).not.toBeInTheDocument();
  });
});

const REASON_LABEL = /^Reason for completing with/;
const NOTES_LABEL = /^Completion Notes/;

/** The Complete dialog opens on a fresh read of the work order; its fields appear once that settles. */
const settledCompleteDialog = async () => {
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByLabelText(NOTES_LABEL);
  return dialog;
};

const openCompleteDialog = async () => {
  renderPage('/maintenance?tab=work_orders');
  const table = await screen.findByTestId('data-table');
  await waitFor(() => expect(within(table).getByText('PM: Compressor Annual PM')).toBeInTheDocument());
  fireEvent.click(within(table).getByRole('button', { name: 'Complete work order MWO-2026-0041' }));
  return settledCompleteDialog();
};

describe('Maintenance — completing a PM work order', () => {
  it('ticks the checklist, sends it, and reports the schedule’s new due date', async () => {
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('moves PM schedule Compressor Annual PM forward');
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    expect(dialog).toHaveTextContent('Planned: 2h');
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    expect(dialog).toHaveTextContent('Checklist (1 of 2 done)');
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check belt' }));
    expect(dialog).toHaveTextContent('Checklist (2 of 2 done)');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const [id, body] = mockedApi.completeMaintenanceWorkOrder.mock.calls[0];
    expect(id).toBe(41);
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: true },
    ]);
    // Every item ticked: no reason is asked for, and none is sent.
    expect(body).not.toHaveProperty('incomplete_checklist_reason');
    // The date comes from the completion response, not from the cached schedule (still Dec 1, 2026).
    expect(
      await screen.findByText('MWO-2026-0041 completed — Compressor Annual PM is next due Nov 16, 2027')
    ).toBeInTheDocument();
  });

  it('sends no checklist for a work order that has none (the server keeps what it has)', async () => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([{ ...openPmWO, checklist_results: null }]);
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    fireEvent.click(await within(table).findByRole('button', { name: 'Complete work order MWO-2026-0041' }));
    const dialog = await settledCompleteDialog();
    expect(dialog).not.toHaveTextContent('Checklist (');
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    expect(mockedApi.completeMaintenanceWorkOrder.mock.calls[0][1]).not.toHaveProperty('checklist_results');
    expect(mockedApi.completeMaintenanceWorkOrder.mock.calls[0][1]).not.toHaveProperty('incomplete_checklist_reason');
  });
});

describe('Maintenance — completing with checklist items unticked', () => {
  it('asks for a reason exactly while an item is unticked, counting them', async () => {
    const dialog = await openCompleteDialog();
    // Nothing ticked yet: both items are open.
    expect(within(dialog).getByLabelText(/^Reason for completing with 2 items unticked\b/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    expect(within(dialog).getByLabelText(/^Reason for completing with 1 item unticked\b/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check belt' }));
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Complete' })).toBeEnabled();

    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check belt' }));
    const reason = within(dialog).getByLabelText(/^Reason for completing with 1 item unticked\b/);
    expect(reason).toHaveAttribute('aria-required', 'true');
    expect(reason).toHaveAttribute('maxLength', '2000');
    expect(dialog).toHaveTextContent('Recorded in the audit trail with the unticked items.');
  });

  it('keeps Complete disabled until the reason is more than whitespace', async () => {
    const dialog = await openCompleteDialog();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    const complete = within(dialog).getByRole('button', { name: 'Complete' });
    expect(complete).toBeDisabled();
    expect(complete).toHaveAttribute('title', 'Give a reason for the unticked checklist items, or tick them');

    const reason = within(dialog).getByLabelText(REASON_LABEL);
    fireEvent.change(reason, { target: { value: '   \n ' } });
    expect(complete).toBeDisabled();
    fireEvent.click(complete);
    expect(mockedApi.completeMaintenanceWorkOrder).not.toHaveBeenCalled();

    fireEvent.change(reason, { target: { value: 'Belt on order' } });
    expect(complete).toBeEnabled();
    expect(complete).not.toHaveAttribute('title');
  });

  it('sends the reason, trimmed, with the checklist it applies to', async () => {
    const dialog = await openCompleteDialog();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), {
      target: { value: '  Replacement belt on back order  ' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));

    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const [id, body] = mockedApi.completeMaintenanceWorkOrder.mock.calls[0];
    expect(id).toBe(41);
    expect(body.incomplete_checklist_reason).toBe('Replacement belt on back order');
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: false },
    ]);
    expect(await screen.findByText(/MWO-2026-0041 completed/)).toBeInTheDocument();
  });

  it('drops a typed reason once every item is ticked', async () => {
    const dialog = await openCompleteDialog();
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: 'Ran out of time' } });
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check belt' }));
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));

    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    expect(mockedApi.completeMaintenanceWorkOrder.mock.calls[0][1]).not.toHaveProperty('incomplete_checklist_reason');
  });

  it('keeps the dialog open with the server’s refusal when the completion is refused', async () => {
    const detail =
      '1 of 2 checklist items on maintenance work order MWO-2026-0041 are not ticked. Tick them, or give a reason for completing without them.';
    mockedApi.completeMaintenanceWorkOrder.mockRejectedValue({ response: { status: 409, data: { detail } } });
    const dialog = await openCompleteDialog();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: 'Belt on order' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      '1 of 2 checklist items on maintenance work order MWO-2026-0041 are not ticked.'
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // The refusal re-reads the work order; the checklist did not change, so no "changed" notice.
    await waitFor(() => expect(mockedApi.getMaintenanceWorkOrder).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Complete' })).toBeEnabled());
    expect(dialog).not.toHaveTextContent('The checklist changed while this was open');
    // What was entered survives the refusal, so the retry is one click.
    expect(within(dialog).getByLabelText(REASON_LABEL)).toHaveValue('Belt on order');
    expect(within(dialog).getByRole('checkbox', { name: 'Drain tank' })).toBeChecked();
  });

  it('counts typing a reason as an unsaved change', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      const dialog = await openCompleteDialog();
      fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: 'Belt on order' } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(within(dialog).getByLabelText(REASON_LABEL)).toHaveValue('Belt on order');
    } finally {
      confirmSpy.mockRestore();
    }
  });
});

describe('Maintenance — a PM answers for its schedule’s checklist', () => {
  it('adds a schedule step the work order no longer carries, unticked, and sends it', async () => {
    mockedApi.getMaintenanceSchedules.mockResolvedValue([
      millPm,
      { ...compressorPm, checklist_items: ['Drain tank', 'Check belt', 'Check oil'] },
    ]);
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 3 done)');
    for (const name of ['Drain tank', 'Check belt', 'Check oil']) {
      fireEvent.click(within(dialog).getByRole('checkbox', { name }));
    }
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));

    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const [, body] = mockedApi.completeMaintenanceWorkOrder.mock.calls[0];
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: true },
      { item: 'Check oil', done: true },
    ]);
    expect(body).not.toHaveProperty('incomplete_checklist_reason');
  });

  it('takes the steps from the work order (the list the server judges), even for an inactive schedule', async () => {
    // The page loads active schedules only; the work order carries its schedule's steps itself.
    mockedApi.getMaintenanceSchedules.mockResolvedValue([millPm]);
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([
      { ...openPmWO, schedule_checklist_items: ['Drain tank', 'Check belt', 'Check oil'] },
    ]);
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    fireEvent.click(await within(table).findByRole('button', { name: 'Complete work order MWO-2026-0041' }));
    const dialog = await settledCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 3 done)');
    expect(within(dialog).getByRole('checkbox', { name: 'Check oil' })).not.toBeChecked();
  });

  it('opens with nothing to save when the work order already carries every schedule step', async () => {
    mockedApi.getMaintenanceSchedules.mockResolvedValue([
      millPm,
      { ...compressorPm, checklist_items: ['Drain tank', 'Check belt'] },
    ]);
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

const clickCompleteFor41 = async () => {
  const table = await screen.findByTestId('data-table');
  fireEvent.click(await within(table).findByRole('button', { name: 'Complete work order MWO-2026-0041' }));
};

/** Ticks both of openPmWO's items, clicks Complete, and returns the body sent. */
const completeWithEveryItemTicked = async (dialog: HTMLElement) => {
  fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
  fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check belt' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
  await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
  return mockedApi.completeMaintenanceWorkOrder.mock.calls[0][1];
};

describe('Maintenance — the Complete dialog reads the work order fresh', () => {
  it('offers nothing to answer until the read settles, then builds the dialog from it, not the cached row', async () => {
    const read = deferred<MaintenanceWorkOrder>();
    mockedApi.getMaintenanceWorkOrder.mockReturnValueOnce(read.promise);
    renderPage('/maintenance?tab=work_orders');
    await clickCompleteFor41();
    const dialog = await screen.findByRole('dialog');
    expect(mockedApi.getMaintenanceWorkOrder).toHaveBeenCalledWith(41);
    expect(within(dialog).getByText('Loading the work order…')).toHaveAttribute('role', 'status');
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
    const complete = within(dialog).getByRole('button', { name: 'Complete' });
    expect(complete).toBeDisabled();
    fireEvent.click(complete);
    expect(mockedApi.completeMaintenanceWorkOrder).not.toHaveBeenCalled();

    // The record has moved on since the list was read: a step ticked, a schedule step added, notes.
    await act(async () => {
      read.resolve({
        ...openPmWO,
        checklist_results: JSON.stringify([
          { item: 'Drain tank', done: true },
          { item: 'Check belt', done: false },
        ]),
        schedule_checklist_items: ['Drain tank', 'Check belt', 'Bleed lines'],
        notes: 'Tank drained Tuesday',
      });
    });
    expect(within(dialog).queryByText('Loading the work order…')).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent('Checklist (1 of 3 done)');
    expect(within(dialog).getByRole('checkbox', { name: 'Drain tank' })).toBeChecked();
    expect(within(dialog).getByRole('checkbox', { name: 'Bleed lines' })).not.toBeChecked();
    expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('Tank drained Tuesday');
  });

  it('falls back to the cached row when the read fails', async () => {
    mockedApi.getMaintenanceWorkOrder.mockRejectedValueOnce(new Error('network down'));
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('');
    const body = await completeWithEveryItemTicked(dialog);
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: true },
    ]);
  });

  it('drops a read that answers after its dialog was closed and reopened', async () => {
    const first = deferred<MaintenanceWorkOrder>();
    const second = deferred<MaintenanceWorkOrder>();
    mockedApi.getMaintenanceWorkOrder.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    renderPage('/maintenance?tab=work_orders');
    await clickCompleteFor41();
    let dialog = await screen.findByRole('dialog');
    // Nothing to lose yet, so closing does not ask.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await clickCompleteFor41();
    dialog = await screen.findByRole('dialog');
    await act(async () => {
      second.resolve({ ...openPmWO, notes: 'From the second read' });
    });
    expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('From the second read');
    await act(async () => {
      first.resolve({ ...openPmWO, checklist_results: null, notes: 'From the first read' });
    });
    expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('From the second read');
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
  });
});

describe('Maintenance — completion notes replace the work order’s', () => {
  beforeEach(() => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([{ ...openPmWO, notes: 'Belt squeals at startup' }]);
  });

  it('opens with the work order’s notes, says they are replaced, and is not an unsaved change', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      const dialog = await openCompleteDialog();
      expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('Belt squeals at startup');
      expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveAccessibleDescription(
        'Saved as the work order’s notes, replacing the ones it has now (shown here). Clear the field to remove them.'
      );
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      expect(confirmSpy).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('sends no notes when they were left as they opened (the server keeps them)', async () => {
    const dialog = await openCompleteDialog();
    const body = await completeWithEveryItemTicked(dialog);
    expect(body).not.toHaveProperty('notes');
  });

  it('sends edited notes, trimmed', async () => {
    const dialog = await openCompleteDialog();
    fireEvent.change(within(dialog).getByLabelText(NOTES_LABEL), {
      target: { value: '  Belt replaced; squeal gone  ' },
    });
    const body = await completeWithEveryItemTicked(dialog);
    expect(body.notes).toBe('Belt replaced; squeal gone');
  });

  it('sends null for emptied notes, which clears them', async () => {
    const dialog = await openCompleteDialog();
    fireEvent.change(within(dialog).getByLabelText(NOTES_LABEL), { target: { value: '   ' } });
    const body = await completeWithEveryItemTicked(dialog);
    expect(body).toHaveProperty('notes', null);
  });
});

describe('Maintenance — a refused completion re-reads instead of dead-ending', () => {
  it('409: adds the new step unticked, keeps what was ticked and typed, warns, and re-reads the page', async () => {
    const detail =
      '1 of 3 checklist items on maintenance work order MWO-2026-0041 are not ticked. Tick them, or give a reason for completing without them.';
    mockedApi.getMaintenanceWorkOrder
      .mockResolvedValueOnce(openPmWO)
      .mockResolvedValueOnce({ ...openPmWO, schedule_checklist_items: ['Drain tank', 'Check belt', 'Check oil'] });
    mockedApi.completeMaintenanceWorkOrder.mockRejectedValueOnce({ response: { status: 409, data: { detail } } });
    const dialog = await openCompleteDialog();
    expect(mockedApi.getMaintenanceWorkOrders).toHaveBeenCalledTimes(1);
    fireEvent.change(within(dialog).getByLabelText('Findings'), { target: { value: 'Belt glazed' } });
    fireEvent.change(within(dialog).getByLabelText('Labor Cost ($)'), { target: { value: '45' } });
    fireEvent.change(within(dialog).getByLabelText(NOTES_LABEL), { target: { value: 'Swapped the belt' } });
    const firstBody = await completeWithEveryItemTicked(dialog);
    expect(firstBody).not.toHaveProperty('incomplete_checklist_reason');

    // The server's words stay, and the dialog now lists what the server judges.
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('1 of 3 checklist items');
    expect(
      await within(dialog).findByText(
        'The checklist changed while this was open: 1 new item. Tick it or give a reason, then Complete again.'
      )
    ).toBeInTheDocument();
    expect(mockedApi.getMaintenanceWorkOrder).toHaveBeenCalledTimes(2);
    expect(dialog).toHaveTextContent('Checklist (2 of 3 done)');
    expect(within(dialog).getByRole('checkbox', { name: 'Drain tank' })).toBeChecked();
    expect(within(dialog).getByRole('checkbox', { name: 'Check belt' })).toBeChecked();
    expect(within(dialog).getByRole('checkbox', { name: 'Check oil' })).not.toBeChecked();
    expect(within(dialog).getByLabelText(/^Reason for completing with 1 item unticked\b/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Complete' })).toBeDisabled();
    expect(within(dialog).getByLabelText('Findings')).toHaveValue('Belt glazed');
    expect(within(dialog).getByLabelText('Labor Cost ($)')).toHaveValue(45);
    expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('Swapped the belt');
    await waitFor(() => expect(mockedApi.getMaintenanceWorkOrders).toHaveBeenCalledTimes(2));

    // Answering the new step clears the reason; the retry carries all three.
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Check oil' }));
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(2));
    const retry = mockedApi.completeMaintenanceWorkOrder.mock.calls[1][1];
    expect(JSON.parse(retry.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: true },
      { item: 'Check oil', done: true },
    ]);
    expect(retry).toMatchObject({ findings: 'Belt glazed', labor_cost: 45, notes: 'Swapped the belt' });
    expect(retry).not.toHaveProperty('incomplete_checklist_reason');
    expect(await screen.findByText(/MWO-2026-0041 completed/)).toBeInTheDocument();
  });

  it('409: notes with only end-whitespace edits follow the re-read record, and a new item the server holds ticked is not flagged', async () => {
    const detail = 'Maintenance work order MWO-2026-0041 changed while it was open.';
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([{ ...openPmWO, notes: 'Belt squeals' }]);
    mockedApi.getMaintenanceWorkOrder
      .mockResolvedValueOnce({ ...openPmWO, notes: 'Belt squeals' })
      .mockResolvedValueOnce({
        ...openPmWO,
        checklist_results: JSON.stringify([
          { item: 'Drain tank', done: false },
          { item: 'Check belt', done: false },
          { item: 'Check oil', done: true },
        ]),
        notes: 'Belt squeals; day shift checked it',
      });
    mockedApi.completeMaintenanceWorkOrder.mockRejectedValueOnce({ response: { status: 409, data: { detail } } });
    const dialog = await openCompleteDialog();
    // Trailing whitespace is no change to the notes (the payload rule): they were left alone.
    fireEvent.change(within(dialog).getByLabelText(NOTES_LABEL), { target: { value: 'Belt squeals   ' } });
    const firstBody = await completeWithEveryItemTicked(dialog);
    expect(firstBody).not.toHaveProperty('notes');

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
    await waitFor(() =>
      expect(within(dialog).getByLabelText(NOTES_LABEL)).toHaveValue('Belt squeals; day shift checked it')
    );
    // The one new item is already ticked on the server: nothing to answer, so no "changed" notice.
    expect(dialog).toHaveTextContent('Checklist (3 of 3 done)');
    expect(within(dialog).getByRole('checkbox', { name: 'Check oil' })).toBeChecked();
    expect(dialog).not.toHaveTextContent('The checklist changed while this was open');
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();

    const complete = within(dialog).getByRole('button', { name: 'Complete' });
    await waitFor(() => expect(complete).toBeEnabled());
    fireEvent.click(complete);
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(2));
    expect(mockedApi.completeMaintenanceWorkOrder.mock.calls[1][1]).not.toHaveProperty('notes');
  });

  it('409: when the re-read fails, says so and offers no retry from a checklist that may be incomplete', async () => {
    const detail =
      '1 of 3 checklist items on maintenance work order MWO-2026-0041 are not ticked. Tick them, or give a reason for completing without them.';
    mockedApi.getMaintenanceWorkOrder.mockResolvedValueOnce(openPmWO).mockRejectedValueOnce(new Error('network down'));
    mockedApi.completeMaintenanceWorkOrder.mockRejectedValueOnce({ response: { status: 409, data: { detail } } });
    const dialog = await openCompleteDialog();
    await completeWithEveryItemTicked(dialog);

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('1 of 3 checklist items');
    expect(
      await within(dialog).findByText(
        "Couldn't reload this work order to show what changed. Close the dialog and open it again."
      )
    ).toBeInTheDocument();
    const complete = within(dialog).getByRole('button', { name: 'Complete' });
    expect(complete).toBeDisabled();
    expect(complete).toHaveAttribute(
      'title',
      "Couldn't reload this work order to show what changed. Close the dialog and open it again."
    );
    fireEvent.click(complete);
    expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1);
    expect(dialog).not.toHaveTextContent('The checklist changed while this was open');
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled();
    await waitFor(() => expect(mockedApi.getMaintenanceWorkOrders).toHaveBeenCalledTimes(2));
  });

  it('400: re-reads the page, keeps the server’s words on screen, and turns Complete off', async () => {
    const detail = 'Maintenance work order MWO-2026-0041 is on_hold; only an in-progress work order can be completed.';
    mockedApi.completeMaintenanceWorkOrder.mockRejectedValueOnce({ response: { status: 400, data: { detail } } });
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const dialog = await openCompleteDialog();
      await completeWithEveryItemTicked(dialog);

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
      await waitFor(() => expect(mockedApi.getMaintenanceWorkOrders).toHaveBeenCalledTimes(2));
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(dialog).toHaveTextContent('Close this dialog: the list now shows where this work order stands.');
      expect(dialog).not.toHaveTextContent('The checklist changed while this was open');
      // Only a 409 re-reads the work order itself.
      expect(mockedApi.getMaintenanceWorkOrder).toHaveBeenCalledTimes(1);

      // A retry would be refused the same way: Complete is off, Cancel is the way out.
      const complete = within(dialog).getByRole('button', { name: 'Complete' });
      await waitFor(() => expect(complete).toBeDisabled());
      fireEvent.click(complete);
      expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1);
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

      // Reopening starts clean: the block belonged to that refusal, not to the work order.
      await clickCompleteFor41();
      const reopened = await settledCompleteDialog();
      expect(reopened).not.toHaveTextContent('Close this dialog');
      expect(within(reopened).queryByRole('alert')).not.toBeInTheDocument();
    } finally {
      confirmSpy.mockRestore();
    }
  });
});

describe('Maintenance — checklist items match as the server matches them', () => {
  it('shows a bulleted, double-spaced work-order item and the schedule’s lower-case step as ONE row', async () => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([
      {
        ...openPmWO,
        checklist_results: JSON.stringify([
          { item: '- Grease  bearing', done: false },
          { item: '1. Check oil', done: false },
        ]),
        schedule_checklist_items: ['grease bearing', '1. check oil'],
      },
    ]);
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    // Shown once, in the work order's (normalized) spelling; numbering is kept.
    expect(within(dialog).getAllByRole('checkbox')).toHaveLength(2);
    expect(within(dialog).getByRole('checkbox', { name: 'Grease bearing' })).toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: '1. Check oil' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('checkbox', { name: 'grease bearing' })).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Grease bearing' }));
    fireEvent.click(within(dialog).getByRole('checkbox', { name: '1. Check oil' }));
    expect(within(dialog).queryByLabelText(REASON_LABEL)).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const [, body] = mockedApi.completeMaintenanceWorkOrder.mock.calls[0];
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Grease bearing', done: true },
      { item: '1. Check oil', done: true },
    ]);
    expect(body).not.toHaveProperty('incomplete_checklist_reason');
  });

  it('keeps a step named twice as two rows, whatever their spelling', async () => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([
      {
        ...openPmWO,
        checklist_results: JSON.stringify([{ item: '• Grease bearing', done: false }]),
        schedule_checklist_items: ['grease bearing', 'GREASE  BEARING'],
      },
    ]);
    const dialog = await openCompleteDialog();
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    const copies = within(dialog).getAllByRole('checkbox', { name: 'Grease bearing' });
    expect(copies).toHaveLength(2);
    fireEvent.click(copies[0]);
    expect(dialog).toHaveTextContent('Checklist (1 of 2 done)');
    expect(within(dialog).getByLabelText(/^Reason for completing with 1 item unticked\b/)).toBeInTheDocument();
  });
});

describe('Maintenance — editing a schedule', () => {
  it('PUTs only the fields that changed', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    fireEvent.click(within(row).getByRole('button', { name: 'Edit schedule Mill 1 Monthly PM' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Edit PM schedule');
    expect(dialog).toHaveTextContent('Mill 1 Monthly PM · MILL-1 · Synthetic Mill');
    const save = within(dialog).getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled(); // nothing changed yet

    fireEvent.change(within(dialog).getByLabelText(/^Next Due Date/), { target: { value: '2026-12-15' } });
    fireEvent.change(within(dialog).getByLabelText(/^Checklist/), {
      target: { value: 'Lubricate ways\nCheck coolant\nClean chip pan' },
    });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(mockedApi.updateMaintenanceSchedule).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateMaintenanceSchedule).toHaveBeenCalledWith(7, {
      next_due_date: '2026-12-15',
      checklist: JSON.stringify(['Lubricate ways', 'Check coolant', 'Clean chip pan']),
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(mockedApi.getMaintenanceSchedules).toHaveBeenCalledTimes(2)); // re-read
  });

  it('needs "every N days" for a custom cadence and sends it with the frequency', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    fireEvent.click(within(row).getByRole('button', { name: 'Edit schedule Mill 1 Monthly PM' }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.change(within(dialog).getByLabelText('Frequency'), { target: { value: 'custom' } });
    const save = within(dialog).getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    expect(dialog).toHaveTextContent('Every N days must be a whole number');
    fireEvent.change(within(dialog).getByLabelText(/^Every N days/), { target: { value: '45' } });
    fireEvent.change(within(dialog).getByLabelText('Priority'), { target: { value: 'low' } });
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Requires machine shutdown' }));
    fireEvent.change(within(dialog).getByLabelText('Est. Duration (hours)'), { target: { value: '3.5' } });
    fireEvent.click(save);

    await waitFor(() => expect(mockedApi.updateMaintenanceSchedule).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateMaintenanceSchedule).toHaveBeenCalledWith(7, {
      frequency: 'custom',
      frequency_days: 45,
      priority: 'low',
      requires_shutdown: false,
      estimated_duration_hours: 3.5,
    });
  });

  it('keeps the dialog open with the server’s reason when the save is refused', async () => {
    mockedApi.updateMaintenanceSchedule.mockRejectedValue({
      response: { data: { detail: 'frequency_days is required when frequency is custom' } },
    });
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    fireEvent.click(within(row).getByRole('button', { name: 'Edit schedule Mill 1 Monthly PM' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Priority'), { target: { value: 'low' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('frequency_days is required');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

describe('Maintenance — KPI tiles count PMs nobody has started or that are on hold', () => {
  it('adds unstarted or held PM schedules to the work-order counts and shows the split', async () => {
    mockedApi.getMaintenanceDashboard.mockResolvedValue({
      overdue_count: 1,
      due_this_week: 2,
      schedules_overdue: 30,
      schedules_due_this_week: 4,
      completed_this_month: 0,
      completion_rate: 0,
      total_cost_month: 0,
      in_progress: 1,
      work_center_metrics: [],
    });
    renderPage('/maintenance?tab=dashboard');
    expect(await screen.findByText('Overdue')).toBeInTheDocument();
    const overdue = screen.getByText('Overdue').closest('div')?.parentElement as HTMLElement;
    expect(overdue).toHaveTextContent('31');
    expect(overdue).toHaveTextContent('1 work order · 30 PMs not started or on hold');
    const due = screen.getByText('Due This Week').closest('div')?.parentElement as HTMLElement;
    expect(due).toHaveTextContent('6');
    expect(due).toHaveTextContent('2 work orders · 4 PMs not started or on hold');
  });
});

/*
 * Each of the three forms this change touched decides "dirty" against what it OPENED with, not
 * against a blank form: a Start PM form is pre-filled, a completion checklist arrives from the
 * work order, and the schedule dialog starts from the schedule. Closing an untouched one must
 * not ask; closing an edited one must ask (`useUnsavedChanges` → `window.confirm`), and saying
 * no must keep the edits on screen and send nothing.
 */
describe('Maintenance — unsaved-changes guards', () => {
  let confirmSpy: jest.SpyInstance<boolean, [message?: string]>;
  beforeEach(() => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
  });
  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it('Start PM: the pre-filled form closes without asking; an edit to it asks first', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    const startPm = () =>
      fireEvent.click(within(row).getByRole('button', { name: 'Start PM work order for Mill 1 Monthly PM' }));

    startPm();
    let dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    startPm();
    dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: 'Mill 1 Monthly PM + spindle' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toHaveTextContent('Start PM: Mill 1 Monthly PM');
    expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('Mill 1 Monthly PM + spindle');

    confirmSpy.mockReturnValue(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.createMaintenanceWorkOrder).not.toHaveBeenCalled();

    // The abandoned Start PM leaves nothing behind: New Work Order is a blank, targeted form.
    fireEvent.click(screen.getByRole('button', { name: 'New Work Order' }));
    dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('New Maintenance Work Order');
    expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('');
    expect(within(dialog).queryByRole('region', { name: 'PM schedule' })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('radio', { name: 'Work center' })).toBeInTheDocument();
  });

  it('Complete: ticking a checklist item alone is an unsaved change, and reopening starts over', async () => {
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    const openComplete = async () => {
      fireEvent.click(await within(table).findByRole('button', { name: 'Complete work order MWO-2026-0041' }));
      return settledCompleteDialog();
    };

    let dialog = await openComplete();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    dialog = await openComplete();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByRole('checkbox', { name: 'Drain tank' })).toBeChecked();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.completeMaintenanceWorkOrder).not.toHaveBeenCalled();

    dialog = await openComplete();
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    expect(within(dialog).getByRole('checkbox', { name: 'Drain tank' })).not.toBeChecked();
  });

  it('Edit schedule: an untouched dialog closes without asking; an edited one asks and keeps the edit', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    const openEdit = () => {
      fireEvent.click(within(row).getByRole('button', { name: 'Edit schedule Mill 1 Monthly PM' }));
      return screen.findByRole('dialog');
    };

    let dialog = await openEdit();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    dialog = await openEdit();
    fireEvent.change(within(dialog).getByLabelText('Priority'), { target: { value: 'low' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close dialog' }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByLabelText('Priority')).toHaveValue('low');

    confirmSpy.mockReturnValue(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.updateMaintenanceSchedule).not.toHaveBeenCalled();
  });
});

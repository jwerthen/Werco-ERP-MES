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
 *  - "Edit" opens the schedule dialog and PUTs only the fields that changed (never a target).
 *  - The Overdue / Due This Week tiles add the PM schedules nobody has started, or whose work order
 *    is on hold.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

const openCompleteDialog = async () => {
  renderPage('/maintenance?tab=work_orders');
  const table = await screen.findByTestId('data-table');
  await waitFor(() => expect(within(table).getByText('PM: Compressor Annual PM')).toBeInTheDocument());
  fireEvent.click(within(table).getByRole('button', { name: 'Complete work order MWO-2026-0041' }));
  return screen.findByRole('dialog');
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
    const dialog = await screen.findByRole('dialog');
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
    const dialog = await screen.findByRole('dialog');
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
      return screen.findByRole('dialog');
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

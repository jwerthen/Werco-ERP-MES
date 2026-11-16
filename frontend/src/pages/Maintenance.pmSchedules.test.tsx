/**
 * Maintenance page — PM schedules drive work orders, and a completed PM moves its schedule on.
 *
 *  - "Start PM" (Schedules tab row, Upcoming card) opens the work-order form pre-filled from the
 *    schedule and sends its `schedule_id` with NO target, so the server inherits the schedule's
 *    target and completing the work order advances the schedule's next due date.
 *  - A schedule that already has an open work order names it instead of offering Start PM.
 *  - Completing a PM work order ticks its checklist and reports the schedule's new due date.
 *  - "Edit" opens the schedule dialog and PUTs only the fields that changed (never a target).
 *  - The Overdue / Due This Week tiles add the PM schedules nobody has started.
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
});

describe('Maintenance — completing a PM work order', () => {
  it('ticks the checklist, sends it, and reports the schedule’s new due date', async () => {
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText('PM: Compressor Annual PM')).toBeInTheDocument());
    fireEvent.click(within(table).getByRole('button', { name: 'Complete work order MWO-2026-0041' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('moves PM schedule Compressor Annual PM forward');
    expect(dialog).toHaveTextContent('Checklist (0 of 2 done)');
    expect(dialog).toHaveTextContent('Planned: 2h');
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Drain tank' }));
    expect(dialog).toHaveTextContent('Checklist (1 of 2 done)');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const [id, body] = mockedApi.completeMaintenanceWorkOrder.mock.calls[0];
    expect(id).toBe(41);
    expect(JSON.parse(body.checklist_results)).toEqual([
      { item: 'Drain tank', done: true },
      { item: 'Check belt', done: false },
    ]);
    expect(await screen.findByText(/MWO-2026-0041 completed — Compressor Annual PM is next due/)).toBeInTheDocument();
  });

  it('sends no checklist for a work order that has none (the server keeps what it has)', async () => {
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([{ ...openPmWO, checklist_results: null }]);
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    fireEvent.click(await within(table).findByRole('button', { name: 'Complete work order MWO-2026-0041' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).not.toHaveTextContent('Checklist (');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(mockedApi.completeMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    expect(mockedApi.completeMaintenanceWorkOrder.mock.calls[0][1]).not.toHaveProperty('checklist_results');
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

describe('Maintenance — KPI tiles count PMs nobody has started', () => {
  it('adds unstarted PM schedules to the work-order counts and shows the split', async () => {
    renderPage('/maintenance?tab=dashboard');
    expect(await screen.findByText('Overdue')).toBeInTheDocument();
    const overdue = screen.getByText('Overdue').closest('div')?.parentElement as HTMLElement;
    expect(overdue).toHaveTextContent('31');
    expect(overdue).toHaveTextContent('1 work order · 30 PMs not started');
    const due = screen.getByText('Due This Week').closest('div')?.parentElement as HTMLElement;
    expect(due).toHaveTextContent('2 work orders · 0 PMs not started');
  });
});

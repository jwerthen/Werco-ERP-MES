/**
 * Maintenance page — a work order's record and log, and a machine's or asset's history.
 *
 *  - A Work Orders row click, or the row's "Details and log" action, opens the work order's record
 *    (target, dates, checklist with the schedule's steps, notes, findings, parts, costs) above its
 *    log timeline (`GET /maintenance/work-orders/{id}/logs`).
 *  - People who perform maintenance (Admin / Manager / Supervisor / Operator) can file an entry
 *    there; the body names the work order and never a target, and the timeline re-reads after it.
 *    Everyone else reads only.
 *  - A schedule's History action opens its target's history: a work center through
 *    `/maintenance/history/{id}`, an asset through `/maintenance/assets/{id}/history`.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Maintenance from './Maintenance';
import type { MaintenanceLogEntry, MaintenanceSchedule, MaintenanceWorkOrder } from '../types/maintenance';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaintenanceDashboard: jest.fn(),
    getMaintenanceSchedules: jest.fn(),
    getMaintenanceWorkOrders: jest.fn(),
    getMaintenanceWorkOrder: jest.fn(),
    getMaintenanceWorkOrderLogs: jest.fn(),
    getMaintenanceHistory: jest.fn(),
    getMaintenanceAssetHistory: jest.fn(),
    getMaintenanceAssets: jest.fn(),
    getWorkCenters: jest.fn(),
    createMaintenanceLog: jest.fn(),
  },
}));
let mockRole = 'operator';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole }, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const millPm: MaintenanceSchedule = {
  id: 7,
  name: 'Mill 1 Monthly PM',
  maintenance_type: 'preventive',
  frequency: 'monthly',
  estimated_duration_hours: 2,
  priority: 'high',
  checklist_items: ['Lubricate ways', 'Check coolant'],
  requires_shutdown: true,
  is_active: true,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'MILL-1',
  target_name: 'Synthetic Mill',
  next_due_date: '2026-12-01',
};

const compressorPm: MaintenanceSchedule = {
  ...millPm,
  id: 8,
  name: 'Compressor Annual PM',
  frequency: 'annual',
  checklist_items: [],
  work_center_id: null,
  asset_id: 5,
  target_type: 'asset',
  target_code: 'AC-1',
  target_name: 'Air Compressor 1',
};

const completedPm: MaintenanceWorkOrder = {
  id: 41,
  schedule_id: 7,
  wo_number: 'MWO-2026-0041',
  title: 'Mill 1 Monthly PM',
  maintenance_type: 'preventive',
  priority: 'high',
  status: 'completed',
  requires_shutdown: true,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'MILL-1',
  target_name: 'Synthetic Mill',
  checklist_results: JSON.stringify([{ item: 'Lubricate ways', done: true }]),
  schedule_checklist_items: ['Lubricate ways', 'Check coolant'],
  scheduled_date: '2026-11-18',
  due_date: '2026-12-01',
  started_at: '2026-11-19T14:00:00Z',
  completed_at: '2026-11-19T16:15:00Z',
  actual_duration_hours: 2.25,
  downtime_minutes: 135,
  labor_cost: 90,
  parts_cost: 42.5,
  total_cost: 132.5,
  notes: 'Ways were dry',
  findings: 'Coolant low; topped off next shift',
  parts_used: 'Way oil, 1 qt',
};

const logEntry = (overrides: Partial<MaintenanceLogEntry>): MaintenanceLogEntry => ({
  id: 1,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'MILL-1',
  target_name: 'Synthetic Mill',
  maintenance_wo_id: 41,
  event_type: 'note',
  description: null,
  performed_by: 7,
  performed_by_name: 'Pat Synthetic',
  event_date: '2026-11-19T14:00:00Z',
  cost: 0,
  created_at: '2026-11-19T14:00:00Z',
  ...overrides,
});

const woLogs: MaintenanceLogEntry[] = [
  logEntry({ id: 12, event_type: 'completed', description: 'Completed', event_date: '2026-11-19T16:15:00Z' }),
  logEntry({ id: 11, event_type: 'started', description: 'Started', event_date: '2026-11-19T14:00:00Z' }),
];

const renderPage = (path: string) =>
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={[path]}>
        <Maintenance />
      </MemoryRouter>
    </ToastProvider>
  );

const workOrderRow = async () => {
  const table = await screen.findByTestId('data-table');
  await waitFor(() => expect(within(table).getByText('MWO-2026-0041')).toBeInTheDocument());
  return within(table).getByText('MWO-2026-0041').closest('tr') as HTMLElement;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'operator';
  mockedApi.getMaintenanceDashboard.mockResolvedValue({
    overdue_count: 0,
    due_this_week: 0,
    completed_this_month: 1,
    completion_rate: 100,
    total_cost_month: 132.5,
    in_progress: 0,
    work_center_metrics: [],
  });
  mockedApi.getMaintenanceSchedules.mockResolvedValue([millPm, compressorPm]);
  mockedApi.getMaintenanceWorkOrders.mockResolvedValue([completedPm]);
  mockedApi.getMaintenanceAssets.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getMaintenanceWorkOrderLogs.mockResolvedValue({
    maintenance_wo_id: 41,
    wo_number: 'MWO-2026-0041',
    logs: woLogs,
  });
  mockedApi.getMaintenanceHistory.mockResolvedValue({
    work_center_id: 3,
    work_center_name: 'Synthetic Mill',
    logs: woLogs,
  });
  mockedApi.getMaintenanceAssetHistory.mockResolvedValue({
    asset_id: 5,
    asset_tag: 'AC-1',
    asset_name: 'Air Compressor 1',
    logs: [],
  });
  mockedApi.createMaintenanceLog.mockResolvedValue(
    logEntry({ id: 13, event_type: 'observation', description: 'Belt squeals', event_date: '2026-11-19T18:00:00Z' })
  );
});

describe('Maintenance — a work order’s record and log', () => {
  it('opens on a row click: the record, its checklist with the schedule’s steps, and its log', async () => {
    renderPage('/maintenance?tab=work_orders');
    fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));

    const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
    expect(dialog).toHaveTextContent('MWO-2026-0041 · Mill 1 Monthly PM');
    expect(dialog).toHaveTextContent('completed');
    expect(dialog).toHaveTextContent('MILL-1 · Synthetic Mill');
    expect(dialog).toHaveTextContent('PM scheduleMill 1 Monthly PM');
    expect(dialog).toHaveTextContent('StartedNov 19, 2026, 8:00 AM');
    expect(dialog).toHaveTextContent('CompletedNov 19, 2026, 10:15 AM');
    expect(dialog).toHaveTextContent('Downtime135 min');
    expect(dialog).toHaveTextContent('Labor cost$90.00');
    expect(dialog).toHaveTextContent('Parts cost$42.50');
    expect(dialog).toHaveTextContent('Total cost$132.50');
    expect(dialog).toHaveTextContent('NotesWays were dry');
    expect(dialog).toHaveTextContent('FindingsCoolant low; topped off next shift');
    expect(dialog).toHaveTextContent('Parts usedWay oil, 1 qt');

    // The work order's own tick, plus the schedule step it no longer carries.
    const checklist = within(dialog).getByRole('region', { name: 'Checklist' });
    expect(checklist).toHaveTextContent('Checklist (1 of 2 done)');
    expect(checklist).toHaveTextContent('Lubricate ways(done)');
    expect(checklist).toHaveTextContent('Check coolant(not done)');

    expect(mockedApi.getMaintenanceWorkOrderLogs).toHaveBeenCalledWith(41);
    const log = await within(dialog).findByRole('list', { name: 'Maintenance log for MWO-2026-0041' });
    const items = within(log).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Completed');
    expect(items[1]).toHaveTextContent('Started');
  });

  it('opens from the row’s Details action once, without the row click firing a second read', async () => {
    renderPage('/maintenance?tab=work_orders');
    fireEvent.click(
      within(await workOrderRow()).getByRole('button', { name: 'Details and log for work order MWO-2026-0041' })
    );
    expect(await screen.findByRole('dialog', { name: /MWO-2026-0041/ })).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.getMaintenanceWorkOrderLogs).toHaveBeenCalledTimes(1));
  });

  it('shows the read error with a Retry', async () => {
    mockedApi.getMaintenanceWorkOrderLogs.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Maintenance work order not found' } },
    });
    renderPage('/maintenance?tab=work_orders');
    fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
    const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Maintenance work order not found');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    expect(await within(dialog).findByRole('list', { name: 'Maintenance log for MWO-2026-0041' })).toBeInTheDocument();
    expect(mockedApi.getMaintenanceWorkOrderLogs).toHaveBeenCalledTimes(2);
  });

  it('lets an operator file an entry against the work order, then re-reads the log', async () => {
    renderPage('/maintenance?tab=work_orders');
    fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
    const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
    await within(dialog).findByRole('list', { name: 'Maintenance log for MWO-2026-0041' });

    mockedApi.getMaintenanceWorkOrderLogs.mockResolvedValueOnce({
      maintenance_wo_id: 41,
      wo_number: 'MWO-2026-0041',
      logs: [
        logEntry({
          id: 13,
          event_type: 'observation',
          description: 'Belt squeals',
          event_date: '2026-11-19T18:00:00Z',
        }),
        ...woLogs,
      ],
    });
    const form = within(dialog).getByRole('form', { name: 'Add log entry' });
    fireEvent.change(within(form).getByLabelText('Type'), { target: { value: 'observation' } });
    fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: ' Belt squeals ' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }));

    await waitFor(() => expect(mockedApi.createMaintenanceLog).toHaveBeenCalledTimes(1));
    const body = mockedApi.createMaintenanceLog.mock.calls[0][0];
    expect(body).toEqual({ maintenance_wo_id: 41, event_type: 'observation', description: 'Belt squeals' });
    expect(body).not.toHaveProperty('work_center_id');
    expect(body).not.toHaveProperty('asset_id');

    await waitFor(() => expect(mockedApi.getMaintenanceWorkOrderLogs).toHaveBeenCalledTimes(2));
    const log = within(dialog).getByRole('list', { name: 'Maintenance log for MWO-2026-0041' });
    await waitFor(() => expect(within(log).getAllByRole('listitem')).toHaveLength(3));
    expect(within(log).getAllByRole('listitem')[0]).toHaveTextContent('Belt squeals');
  });

  it('asks before closing over a typed entry, and keeps it when told no', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      renderPage('/maintenance?tab=work_orders');
      fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
      const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });

      // Untouched: closes without asking.
      fireEvent.click(within(dialog).getByRole('button', { name: 'Close dialog' }));
      expect(confirmSpy).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

      fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
      const reopened = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
      const form = within(reopened).getByRole('form', { name: 'Add log entry' });
      fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Half typed' } });
      fireEvent.click(within(reopened).getByRole('button', { name: 'Close dialog' }));
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(within(form).getByLabelText(/^Description/)).toHaveValue('Half typed');

      confirmSpy.mockReturnValue(true);
      fireEvent.click(within(reopened).getByRole('button', { name: 'Close dialog' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(mockedApi.createMaintenanceLog).not.toHaveBeenCalled();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('cannot be closed while an entry is being filed: X, Escape and the backdrop all hold', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    let finish: (entry: MaintenanceLogEntry) => void = () => undefined;
    mockedApi.createMaintenanceLog.mockReturnValueOnce(
      new Promise<MaintenanceLogEntry>(resolve => {
        finish = resolve;
      })
    );
    try {
      renderPage('/maintenance?tab=work_orders');
      fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
      const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
      const form = within(dialog).getByRole('form', { name: 'Add log entry' });
      fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Belt squeals' } });
      fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }));
      await waitFor(() => expect(mockedApi.createMaintenanceLog).toHaveBeenCalledTimes(1));

      const closeButton = within(dialog).getByRole('button', { name: 'Close dialog' });
      await waitFor(() => expect(closeButton).toBeDisabled());
      fireEvent.click(closeButton);
      fireEvent.keyDown(window, { key: 'Escape' });
      fireEvent.click(dialog.parentElement as HTMLElement);
      expect(screen.getByRole('dialog', { name: /MWO-2026-0041/ })).toBeInTheDocument();
      // Nothing offered to discard what the server is about to keep.
      expect(confirmSpy).not.toHaveBeenCalled();

      await act(async () => finish(logEntry({ id: 13, description: 'Belt squeals' })));
      await waitFor(() => expect(closeButton).toBeEnabled());
      // Filed and reset: nothing unsaved is left, so it closes without asking.
      fireEvent.click(closeButton);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(confirmSpy).not.toHaveBeenCalled();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('closes on a backdrop click when read-only or untouched, and not over a typed entry', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      renderPage('/maintenance?tab=work_orders');
      fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
      let dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
      fireEvent.click(dialog.parentElement as HTMLElement);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

      fireEvent.click(within(await workOrderRow()).getByText('Mill 1 Monthly PM'));
      dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
      const form = within(dialog).getByRole('form', { name: 'Add log entry' });
      fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Half typed' } });
      fireEvent.click(dialog.parentElement as HTMLElement);
      expect(screen.getByRole('dialog', { name: /MWO-2026-0041/ })).toBeInTheDocument();
      expect(within(form).getByLabelText(/^Description/)).toHaveValue('Half typed');
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it.each(['viewer', 'shipping'])('shows a %s the record and log but no way to file an entry', async role => {
    mockRole = role;
    renderPage('/maintenance?tab=work_orders');
    const row = await workOrderRow();
    expect(within(row).queryByRole('button', { name: /Start work order/ })).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Details and log for work order MWO-2026-0041' }));
    const dialog = await screen.findByRole('dialog', { name: /MWO-2026-0041/ });
    expect(await within(dialog).findByRole('list', { name: 'Maintenance log for MWO-2026-0041' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('form', { name: 'Add log entry' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Add entry' })).not.toBeInTheDocument();
  });
});

describe('Maintenance — a schedule’s target history', () => {
  const scheduleRow = async (name: string) => {
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText(name)).toBeInTheDocument());
    return within(table).getByText(name).closest('tr') as HTMLElement;
  };

  it('reads a work center’s history through /maintenance/history/{id}', async () => {
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Mill 1 Monthly PM');
    fireEvent.click(
      within(row).getByRole('button', {
        name: 'Maintenance history for MILL-1 · Synthetic Mill (schedule Mill 1 Monthly PM)',
      })
    );
    const dialog = await screen.findByRole('dialog', { name: 'Maintenance history: MILL-1 · Synthetic Mill' });
    expect(dialog).toHaveTextContent('Work center');
    expect(mockedApi.getMaintenanceHistory).toHaveBeenCalledWith(3);
    expect(mockedApi.getMaintenanceAssetHistory).not.toHaveBeenCalled();
    const log = await within(dialog).findByRole('list', { name: 'Maintenance history for MILL-1 · Synthetic Mill' });
    expect(within(log).getAllByRole('listitem')).toHaveLength(2);
  });

  it('reads an asset’s history through /maintenance/assets/{id}/history, and says when it is empty', async () => {
    mockRole = 'viewer'; // reading history is open to every role
    renderPage('/maintenance?tab=schedules');
    const row = await scheduleRow('Compressor Annual PM');
    fireEvent.click(within(row).getByRole('button', { name: /^Maintenance history for AC-1 · Air Compressor 1/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Maintenance history: AC-1 · Air Compressor 1' });
    expect(dialog).toHaveTextContent('Equipment asset');
    expect(mockedApi.getMaintenanceAssetHistory).toHaveBeenCalledWith(5);
    expect(mockedApi.getMaintenanceHistory).not.toHaveBeenCalled();
    expect(await within(dialog).findByTestId('empty-state')).toHaveTextContent('No log entries yet');
  });
});

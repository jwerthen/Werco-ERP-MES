/**
 * Maintenance page — the fixes that make it able to create a PM schedule again, and the
 * work-center-OR-asset target (migration 124).
 *
 *  - A schedule is sent with its required `name`, the server's `frequency_days` (custom
 *    cadence only — never the old ignored `frequency_value`), a typed frequency (no
 *    "usage based") and the checklist as a JSON array.
 *  - A schedule / work order targets exactly one thing: `asset_id` for an equipment asset,
 *    `work_center_id` for a machine — never both.
 *  - KPIs read the dashboard's real keys; Start is offered for scheduled / overdue / on-hold
 *    work orders; writes are gated by role.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Maintenance from './Maintenance';
import type { MaintenanceWorkOrder } from '../types/maintenance';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaintenanceDashboard: jest.fn(),
    getMaintenanceSchedules: jest.fn(),
    getMaintenanceWorkOrders: jest.fn(),
    getMaintenanceAssets: jest.fn(),
    getWorkCenters: jest.fn(),
    createMaintenanceSchedule: jest.fn(),
    createMaintenanceWorkOrder: jest.fn(),
    startMaintenanceWorkOrder: jest.fn(),
    updateMaintenanceWorkOrder: jest.fn(),
  },
}));
let mockRole = 'supervisor';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole }, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const forklift = {
  id: 4,
  asset_tag: 'FL-01',
  name: 'Forklift 1',
  asset_type: 'forklift',
  is_active: true,
  version: 1,
  active_schedule_count: 0,
  open_work_order_count: 0,
};

const scheduledWO: MaintenanceWorkOrder = {
  id: 31,
  wo_number: 'MWO-2026-0031',
  title: 'Replace way wipers',
  maintenance_type: 'corrective',
  priority: 'high',
  status: 'scheduled',
  requires_shutdown: true,
  work_center_id: 1,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'CNC-01',
  target_name: 'Mori Seiki MV-653',
};

const renderPage = (path = '/maintenance') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Maintenance />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'supervisor';
  mockedApi.getMaintenanceDashboard.mockResolvedValue({
    overdue_count: 2,
    due_this_week: 3,
    completed_this_month: 5,
    completion_rate: 80,
    total_cost_month: 0,
    in_progress: 1,
    work_center_metrics: [],
  });
  mockedApi.getMaintenanceSchedules.mockResolvedValue([]);
  mockedApi.getMaintenanceWorkOrders.mockResolvedValue([scheduledWO]);
  mockedApi.getMaintenanceAssets.mockResolvedValue([forklift] as any);
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, code: 'CNC-01', name: 'Mori Seiki MV-653' }] as any);
  mockedApi.createMaintenanceSchedule.mockResolvedValue({});
  mockedApi.createMaintenanceWorkOrder.mockResolvedValue({});
  mockedApi.startMaintenanceWorkOrder.mockResolvedValue({ ...scheduledWO, status: 'in_progress' });
  mockedApi.updateMaintenanceWorkOrder.mockResolvedValue({ ...scheduledWO, status: 'cancelled' });
});

async function openScheduleForm() {
  renderPage('/maintenance?tab=schedules');
  await screen.findAllByText('No schedules configured');
  fireEvent.click(screen.getAllByRole('button', { name: 'New Schedule' })[0]);
  return screen.findByRole('dialog');
}

describe('Maintenance — schedules and targets', () => {
  it('reads the dashboard’s real KPI keys', async () => {
    renderPage('/maintenance?tab=dashboard');
    expect(await screen.findByText('Due This Week')).toBeInTheDocument();
    const due = screen.getByText('Due This Week').closest('div')?.parentElement as HTMLElement;
    expect(due).toHaveTextContent('3');
    const overdue = screen.getByText('Overdue').closest('div')?.parentElement as HTMLElement;
    expect(overdue).toHaveTextContent('2');
  });

  it('creates an asset-targeted custom-cadence schedule with asset_id, frequency_days and a JSON checklist', async () => {
    const dialog = await openScheduleForm();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'Forklift quarterly-ish' } });
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Equipment asset' }));
    const picker = within(dialog).getByRole('combobox', { name: /^Equipment Asset/ });
    await waitFor(() => expect(picker).toBeEnabled());
    fireEvent.click(picker);
    fireEvent.click(await screen.findByRole('option', { name: /FL-01 Forklift 1/ }));

    // The old form offered "usage based", which the server cannot store.
    const frequency = within(dialog).getByLabelText('Frequency');
    expect(within(frequency).queryByRole('option', { name: /usage/i })).not.toBeInTheDocument();
    fireEvent.change(frequency, { target: { value: 'custom' } });
    const create = within(dialog).getByRole('button', { name: 'Create' });
    expect(create).toBeDisabled(); // custom needs "every N days"
    fireEvent.change(within(dialog).getByLabelText(/^Every N days/), { target: { value: '45' } });
    fireEvent.change(within(dialog).getByLabelText(/^Checklist/), {
      target: { value: 'Check tires\n\n  Check forks  \n' },
    });
    expect(create).toBeEnabled();
    fireEvent.click(create);

    await waitFor(() => expect(mockedApi.createMaintenanceSchedule).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createMaintenanceSchedule.mock.calls[0][0];
    expect(payload).toMatchObject({
      asset_id: 4,
      name: 'Forklift quarterly-ish',
      frequency: 'custom',
      frequency_days: 45,
      maintenance_type: 'preventive',
      checklist: JSON.stringify(['Check tires', 'Check forks']),
    });
    expect(payload).not.toHaveProperty('work_center_id');
    expect(payload).not.toHaveProperty('frequency_value');
  });

  it('a non-custom cadence sends no frequency_days', async () => {
    const dialog = await openScheduleForm();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'CNC-01 Monthly PM' } });
    const picker = within(dialog).getByRole('combobox', { name: /^Work Center/ });
    await waitFor(() => expect(picker).toBeEnabled());
    fireEvent.click(picker);
    fireEvent.click(await screen.findByRole('option', { name: /CNC-01 Mori Seiki/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(mockedApi.createMaintenanceSchedule).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createMaintenanceSchedule.mock.calls[0][0];
    expect(payload).toMatchObject({ work_center_id: 1, frequency: 'monthly', name: 'CNC-01 Monthly PM' });
    expect(payload.frequency_days).toBeUndefined();
    expect(payload).not.toHaveProperty('asset_id');
  });

  it('creates an asset work order with asset_id only', async () => {
    renderPage('/maintenance?tab=work_orders');
    await screen.findAllByText('Replace way wipers');
    fireEvent.click(screen.getByRole('button', { name: /New Work Order/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: 'Forklift brake check' } });
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Equipment asset' }));
    expect(within(dialog).getByText('Equipment assets never record machine downtime.')).toBeInTheDocument();
    const picker = within(dialog).getByRole('combobox', { name: /^Equipment Asset/ });
    await waitFor(() => expect(picker).toBeEnabled());
    fireEvent.click(picker);
    fireEvent.click(await screen.findByRole('option', { name: /FL-01 Forklift 1/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(mockedApi.createMaintenanceWorkOrder).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createMaintenanceWorkOrder.mock.calls[0][0];
    expect(payload).toMatchObject({ asset_id: 4, title: 'Forklift brake check', priority: 'medium' });
    expect(payload).not.toHaveProperty('work_center_id');
  });
});

describe('Maintenance — work-order actions and roles', () => {
  it('offers Start on a scheduled work order and names its target', async () => {
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText('Replace way wipers')).toBeInTheDocument());
    const row = within(table).getByText('Replace way wipers').closest('tr') as HTMLElement;
    expect(within(row).getByText('CNC-01 · Mori Seiki MV-653')).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Start work order MWO-2026-0031' }));
    await waitFor(() => expect(mockedApi.startMaintenanceWorkOrder).toHaveBeenCalledWith(31));
  });

  it('lets an operator start work but not plan it', async () => {
    mockRole = 'operator';
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText('Replace way wipers')).toBeInTheDocument());
    expect(within(table).getByRole('button', { name: /Start work order/ })).toBeInTheDocument();
    expect(within(table).queryByRole('button', { name: /Cancel work order/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New Work Order/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New Schedule/ })).not.toBeInTheDocument();
  });

  it('puts a started work order on hold and cancels one through the PUT, confirming the cancel', async () => {
    // Without these the only way to end an abandoned shutdown work order -- and the downtime it
    // keeps open on the machine -- was the raw API.
    mockedApi.getMaintenanceWorkOrders.mockResolvedValue([{ ...scheduledWO, status: 'in_progress' }]);
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText('Replace way wipers')).toBeInTheDocument());
    const row = within(table).getByText('Replace way wipers').closest('tr') as HTMLElement;

    fireEvent.click(within(row).getByRole('button', { name: 'Put work order MWO-2026-0031 on hold' }));
    await waitFor(() => expect(mockedApi.updateMaintenanceWorkOrder).toHaveBeenCalledWith(31, { status: 'on_hold' }));

    fireEvent.click(within(row).getByRole('button', { name: 'Cancel work order MWO-2026-0031' }));
    const confirm = await screen.findByRole('dialog');
    expect(confirm).toHaveTextContent('Downtime it is recording on the machine ends');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel work order' }));
    await waitFor(() =>
      expect(mockedApi.updateMaintenanceWorkOrder).toHaveBeenLastCalledWith(31, { status: 'cancelled' })
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows planning controls to a platform admin, whom the server admits', async () => {
    mockRole = 'platform_admin';
    renderPage('/maintenance?tab=work_orders');
    await screen.findByTestId('data-table');
    expect(await screen.findByRole('button', { name: /New Work Order/ })).toBeInTheDocument();
  });

  it('is read-only for a viewer', async () => {
    mockRole = 'viewer';
    renderPage('/maintenance?tab=work_orders');
    const table = await screen.findByTestId('data-table');
    await waitFor(() => expect(within(table).getByText('Replace way wipers')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Start work order/ })).not.toBeInTheDocument();
  });
});

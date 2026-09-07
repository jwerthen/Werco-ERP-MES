import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../components/ui/Toast';
import api from '../services/api';
import type { SchedulingImpactApplyResponse, SchedulingImpactResponse } from '../types/schedulingImpact';
import Scheduling from './Scheduling';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getSchedulableWorkOrders: jest.fn(),
    getCapacityHeatmap: jest.fn(),
    previewSchedulingImpact: jest.fn(),
    applySchedulingImpact: jest.fn(),
    scheduleWorkOrder: jest.fn(),
    scheduleWorkOrderEarliest: jest.fn(),
    bulkScheduleEarliest: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({ getAccessToken: () => 'test', buildWsUrl: () => 'ws://localhost/test' }));

const mockedApi = api as jest.Mocked<typeof api>;
const jobs = [1, 2, 3].map(id => ({
  id,
  work_order_id: id,
  work_order_number: `WO-${id}`,
  current_operation_id: id * 10,
  current_operation_name: 'Cut',
  current_operation_number: '10',
  current_operation_sequence: 10,
  part_number: id === 3 ? 'OTHER' : 'VISIBLE',
  part_name: 'Bracket',
  work_center_id: 7,
  status: 'released',
  operation_status: 'pending',
  quantity: 10,
  quantity_complete: 0,
  priority: 5,
  total_operations: 1,
  operations_complete: 0,
  remaining_hours: 8,
  setup_hours: 0,
  run_hours: 8,
}));

function plan(): SchedulingImpactResponse {
  return {
    action: 'shift',
    shift_days: 2,
    plan_token: 'reviewed-token-1',
    expires_at: '2099-09-07T19:00:00Z',
    summary: {
      selected_jobs: 2,
      changed_jobs: 1,
      changed_operations: 1,
      blocked_jobs: 1,
      skipped_jobs: 0,
      late_jobs: 1,
      overloaded_days: 1,
    },
    jobs: [
      {
        work_order_id: 1,
        work_order_number: 'WO-1',
        due_date: '2026-09-08',
        before_finish: '2026-09-07',
        after_finish: '2026-09-09',
        before_late_days: 0,
        late_days: 1,
        outcome: 'changed',
        reason: null,
        operations: [
          {
            operation_id: 10,
            operation_number: '10',
            operation_name: 'Cut',
            work_center_id: 7,
            work_center_code: 'LAS-1',
            before_start: '2026-09-07T00:00:00',
            before_end: '2026-09-07T00:00:00',
            after_start: '2026-09-09T00:00:00',
            after_end: '2026-09-09T00:00:00',
            before_status: 'pending',
            after_status: 'ready',
          },
        ],
      },
      {
        work_order_id: 2,
        work_order_number: 'WO-2',
        due_date: null,
        before_finish: null,
        after_finish: null,
        before_late_days: null,
        late_days: null,
        outcome: 'blocked',
        reason: 'A remaining operation has no active work center in this company.',
        operations: [],
      },
    ],
    capacity: [
      {
        work_center_id: 7,
        work_center_code: 'LAS-1',
        date: '2026-09-09',
        capacity_hours: 8,
        before_hours: 8,
        after_hours: 16,
        overload_hours: 8,
        affected_jobs: [
          { work_order_id: 1, work_order_number: 'WO-1' },
          { work_order_id: 9, work_order_number: 'WO-NEIGHBOR' },
        ],
      },
    ],
  };
}

function deferred<T>() {
  let resolve!: (result: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}

async function renderSelected() {
  render(
    <MemoryRouter>
      <ToastProvider>
        <Scheduling />
      </ToastProvider>
    </MemoryRouter>
  );
  await screen.findByText('WO-1');
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select all visible work orders' }));
  fireEvent.change(screen.getByPlaceholderText('Search WO#, part...'), { target: { value: 'VISIBLE' } });
  fireEvent.click(screen.getByRole('button', { name: 'Bulk' }));
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Shift dates by days' }), { target: { value: 2 } });
}

beforeEach(() => {
  jest.resetAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue([
    { id: 7, code: 'LAS-1', name: 'Laser 1', capacity_hours_per_day: 8 },
  ] as never);
  mockedApi.getSchedulableWorkOrders.mockResolvedValue(jobs as never);
  mockedApi.getCapacityHeatmap.mockResolvedValue({
    work_centers: [],
    overloaded_work_centers: [],
    overload_cells: 0,
  } as never);
  mockedApi.previewSchedulingImpact.mockResolvedValue(plan());
  mockedApi.applySchedulingImpact.mockResolvedValue({
    message: 'Applied reviewed schedule.',
    already_applied: false,
    applied_work_order_ids: [1],
    changed_operations: 1,
  });
});

it('previews only selected visible jobs, displays exact changes and risks, and writes only the reviewed plan', async () => {
  await renderSelected();
  fireEvent.click(screen.getByRole('button', { name: 'Preview Shift Dates' }));
  const dialog = screen.getByRole('dialog', { name: 'Review scheduling impact' });
  await within(dialog).findByText('WO-NEIGHBOR', { exact: false });
  expect(mockedApi.previewSchedulingImpact).toHaveBeenCalledWith({
    action: 'shift',
    shift_days: 2,
    work_order_ids: [1, 2],
  });
  expect(within(dialog).getByText('Sep 7, 2026 – Sep 7, 2026')).toBeInTheDocument();
  expect(within(dialog).getByText('Sep 9, 2026 – Sep 9, 2026')).toBeInTheDocument();
  expect(within(dialog).getByText(/1 day late \(was 0\)/)).toBeInTheDocument();
  expect(within(dialog).getByText('8h overload')).toBeInTheDocument();
  expect(mockedApi.applySchedulingImpact).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Apply reviewed plan' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(mockedApi.applySchedulingImpact).toHaveBeenCalledWith('reviewed-token-1');
  expect(mockedApi.scheduleWorkOrder).not.toHaveBeenCalled();
  expect(mockedApi.scheduleWorkOrderEarliest).not.toHaveBeenCalled();
  expect(mockedApi.bulkScheduleEarliest).not.toHaveBeenCalled();
  expect(screen.getByText(/WO-2: Blocked: A remaining operation/)).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: 'Select work order WO-1' })).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: 'Select work order WO-2' })).toBeChecked();
  fireEvent.change(screen.getByPlaceholderText('Search WO#, part...'), { target: { value: '' } });
  expect(screen.getByRole('checkbox', { name: 'Select work order WO-3' })).toBeChecked();
});

it('guards duplicate preview and apply attempts until reconciliation finishes', async () => {
  const previewPending = deferred<SchedulingImpactResponse>();
  const applyPending = deferred<SchedulingImpactApplyResponse>();
  mockedApi.previewSchedulingImpact.mockReturnValue(previewPending.promise);
  mockedApi.applySchedulingImpact.mockReturnValue(applyPending.promise);
  await renderSelected();
  const previewButton = screen.getByRole('button', { name: 'Preview Shift Dates' });
  fireEvent.click(previewButton);
  fireEvent.click(previewButton);
  expect(mockedApi.previewSchedulingImpact).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Close impact preview' })).toBeDisabled();
  await act(async () => {
    previewPending.resolve(plan());
  });
  const applyButton = screen.getByRole('button', { name: 'Apply reviewed plan' });
  fireEvent.click(applyButton);
  fireEvent.click(applyButton);
  expect(mockedApi.applySchedulingImpact).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Close impact preview' })).toBeDisabled();
  const reconcile = deferred<never>();
  mockedApi.getSchedulableWorkOrders.mockReturnValue(reconcile.promise);
  await act(async () => {
    applyPending.resolve({
      message: 'Applied.',
      already_applied: false,
      applied_work_order_ids: [1],
      changed_operations: 1,
    });
  });
  expect(screen.getByRole('button', { name: 'Applying reviewed plan…' })).toBeDisabled();
  await act(async () => {
    reconcile.resolve(jobs as never);
  });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});

it('rejects stale plans and requires a fresh explicit review without silently applying a replacement', async () => {
  mockedApi.applySchedulingImpact.mockRejectedValueOnce({
    response: { status: 409, data: { detail: 'Capacity changed after preview.' } },
  });
  await renderSelected();
  fireEvent.click(screen.getByRole('button', { name: 'Preview Shift Dates' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed plan' }));
  await screen.findByText('Capacity changed after preview.', { exact: false });
  expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeDisabled();
  expect(mockedApi.previewSchedulingImpact).toHaveBeenCalledTimes(1);
  mockedApi.previewSchedulingImpact.mockResolvedValue({ ...plan(), plan_token: 'reviewed-token-2' });
  fireEvent.click(screen.getByRole('button', { name: 'Regenerate preview' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeEnabled());
  expect(mockedApi.applySchedulingImpact).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed plan' }));
  await waitFor(() => expect(mockedApi.applySchedulingImpact).toHaveBeenLastCalledWith('reviewed-token-2'));
});

it('retries an ambiguous apply with the same token and preserves review until confirmed', async () => {
  mockedApi.applySchedulingImpact.mockRejectedValueOnce(new Error('Network lost'));
  await renderSelected();
  fireEvent.click(screen.getByRole('button', { name: 'Preview Shift Dates' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed plan' }));
  const retry = await screen.findByRole('button', { name: 'Retry reviewed plan' });
  expect(screen.queryByRole('button', { name: 'Regenerate preview' })).not.toBeInTheDocument();
  expect(screen.getByText(/result could not be confirmed/)).toBeInTheDocument();
  mockedApi.applySchedulingImpact.mockResolvedValue({
    message: 'Already applied.',
    already_applied: true,
    applied_work_order_ids: [1],
    changed_operations: 1,
  });
  fireEvent.click(retry);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(mockedApi.applySchedulingImpact.mock.calls).toEqual([['reviewed-token-1'], ['reviewed-token-1']]);
  expect(mockedApi.previewSchedulingImpact).toHaveBeenCalledTimes(1);
  expect(screen.getByText('WO-1: Reviewed plan already applied')).toBeInTheDocument();
});

it('keeps failed and wholly blocked previews read-only with useful retry or resolution guidance', async () => {
  mockedApi.previewSchedulingImpact.mockRejectedValueOnce(new Error('Offline'));
  await renderSelected();
  fireEvent.click(screen.getByRole('button', { name: 'Preview Selected Earliest' }));
  await screen.findByText(/No scheduling changes were requested/);
  expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeDisabled();
  const blocked = plan();
  blocked.jobs = [blocked.jobs[1]];
  blocked.plan_token = null;
  blocked.summary = {
    ...blocked.summary,
    selected_jobs: 1,
    changed_jobs: 0,
    changed_operations: 0,
    late_jobs: 0,
    overloaded_days: 0,
  };
  blocked.capacity = [];
  mockedApi.previewSchedulingImpact.mockResolvedValue(blocked);
  fireEvent.click(screen.getByRole('button', { name: 'Retry preview' }));
  await screen.findByText('A remaining operation has no active work center in this company.');
  expect(screen.getByRole('button', { name: 'Apply reviewed plan' })).toBeDisabled();
  expect(mockedApi.applySchedulingImpact).not.toHaveBeenCalled();
});

it('routes the visible-unscheduled shortcut through review instead of a bulk write', async () => {
  await renderSelected();
  fireEvent.click(screen.getByRole('button', { name: 'Preview Visible Unscheduled' }));
  await waitFor(() =>
    expect(mockedApi.previewSchedulingImpact).toHaveBeenCalledWith({ action: 'earliest', work_order_ids: [1, 2] })
  );
  expect(mockedApi.bulkScheduleEarliest).not.toHaveBeenCalled();
  expect(mockedApi.applySchedulingImpact).not.toHaveBeenCalled();
});

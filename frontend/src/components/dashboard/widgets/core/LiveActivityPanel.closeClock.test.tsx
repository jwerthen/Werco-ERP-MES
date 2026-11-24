/**
 * Live Shop Activity -> "Close clock" (labor_review/1 step 4).
 *
 * The action is offered to exactly the roles the set-clock-out verb admits (Admin / Manager /
 * Supervisor, plus a superuser), never on the viewer's own entry, and never when the heartbeat
 * carries no `time_entry_version` (an older API: the verb needs it). A save forces a heartbeat
 * refresh so the closed clock leaves the panel only after the server says so.
 */
import React from 'react';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import api from '../../../../services/api';
import { ToastProvider } from '../../../ui';
import { renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import { CoreDashboardProvider } from './CoreDashboardContext';
import { LiveActivityPanel, canCloseClocks } from './LiveActivityPanel';
import type { ActiveAssignment, DashboardData, UserRole } from '../../../../types';
import type { SetClockOutResponse } from '../../../../types/laborReview';

jest.mock('../../../../services/api', () => ({
  __esModule: true,
  default: { setTimeEntryClockOut: jest.fn() },
}));

const mockUser: { value: { id: number; role: UserRole; is_superuser: boolean } } = {
  value: { id: 1, role: 'manager', is_superuser: false },
};
jest.mock('../../../../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser.value, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const assignment = (overrides: Partial<ActiveAssignment> = {}): ActiveAssignment => ({
  time_entry_id: 77,
  time_entry_version: 5,
  clock_in: '2026-11-11T13:00:00Z',
  entry_type: 'run',
  user: { id: 9, employee_id: 'E-009', name: 'Riley Example', role: 'operator' },
  work_order: {
    id: 42,
    work_order_number: 'WO-1042',
    status: 'in_progress',
    quantity_ordered: 10,
    quantity_complete: 2,
  },
  operation: { id: 900, operation_number: '20', name: 'Weld', status: 'in_progress', quantity_ordered: 10 },
  work_center: { id: 3, name: 'Weld Cell' },
  ...overrides,
});

const dashboard = (assignments: ActiveAssignment[]): DashboardData => ({
  summary: {
    active_work_orders: 1,
    due_today: 0,
    overdue: 0,
    signed_in_users: 1,
    checked_in_users: 1,
    idle_signed_in_users: 0,
  },
  work_centers: [],
  signed_in_users: [],
  active_assignments: assignments,
  recent_completions: [],
});

const renderPanel = (assignments: ActiveAssignment[]) =>
  renderWithDashboardContext(
    <ToastProvider>
      <CoreDashboardProvider value={{ data: dashboard(assignments), shopError: '', openOperation: jest.fn() }}>
        <LiveActivityPanel />
      </CoreDashboardProvider>
    </ToastProvider>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockUser.value = { id: 1, role: 'manager', is_superuser: false };
});

describe('canCloseClocks', () => {
  it.each<[UserRole, boolean]>([
    ['platform_admin', true],
    ['admin', true],
    ['manager', true],
    ['supervisor', true],
    ['quality', false],
    ['operator', false],
    ['shipping', false],
    ['viewer', false],
  ])('%s -> %s', (role, expected) => {
    expect(canCloseClocks({ role, is_superuser: false })).toBe(expected);
  });

  it('lets a superuser through whatever the role', () => {
    expect(canCloseClocks({ role: 'viewer', is_superuser: true })).toBe(true);
  });
});

describe('LiveActivityPanel Close clock', () => {
  it('is offered to a manager on someone else’s entry', () => {
    renderPanel([assignment()]);
    expect(screen.getByRole('button', { name: 'Close clock for Riley Example' })).toBeInTheDocument();
  });

  it('is not offered to Quality', () => {
    mockUser.value = { id: 1, role: 'quality', is_superuser: false };
    renderPanel([assignment()]);
    expect(screen.queryByRole('button', { name: /Close clock/ })).toBeNull();
  });

  it('is not offered on the viewer’s own entry', () => {
    renderPanel([assignment({ user: { id: 1, employee_id: 'E-001', name: 'Me Myself', role: 'manager' } })]);
    expect(screen.queryByRole('button', { name: /Close clock/ })).toBeNull();
  });

  it('is not offered when the heartbeat carries no entry version', () => {
    renderPanel([assignment({ time_entry_version: undefined })]);
    expect(screen.queryByRole('button', { name: /Close clock/ })).toBeNull();
  });

  it('sends the heartbeat version and forces a refresh after the server accepts', async () => {
    const response: SetClockOutResponse = {
      entry: {
        id: 77,
        version: 6,
        user_id: 9,
        employee_name: 'Riley Example',
        employee_id: 'E-009',
        work_order_id: 42,
        work_order_number: 'WO-1042',
        work_order_status: 'in_progress',
        operation_id: 900,
        operation_number: '20',
        operation_name: 'Weld',
        operation_status: 'in_progress',
        work_center_id: 3,
        work_center_name: 'Weld Cell',
        entry_type: 'run',
        source: 'kiosk',
        clock_in: '2026-11-11T13:00:00Z',
        clock_out: '2026-11-11T21:00:00Z',
        duration_hours: 8,
        elapsed_hours: 8,
        is_open: false,
        quantity_produced: 0,
        quantity_scrapped: 0,
        approved: null,
        approved_by: null,
        approved_by_name: null,
        production_day: '2026-11-11',
        shift_name: null,
        shift_end: null,
        flags: [],
        can_approve: true,
        can_unapprove: false,
        can_set_clock_out: true,
      },
      changed: true,
      previous_clock_out: null,
      previous_duration_hours: null,
      hours: {
        applied: false,
        skipped_reason: 'operation_hours_manual',
        hours_delta: 8,
        operation_bucket: 'run',
        operation_hours_before: 2,
        operation_hours_after: 2,
        work_order_hours_before: 2,
        work_order_hours_after: 10,
        job_cost_recomputed: false,
        actual_cost_recomputed: false,
      },
    };
    mockedApi.setTimeEntryClockOut.mockResolvedValue(response);
    const { refresh } = renderPanel([assignment()]);

    fireEvent.click(screen.getByRole('button', { name: 'Close clock for Riley Example' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Clock-out \(Central time\)/), {
      target: { value: '2026-11-11T15:00' },
    });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Went home at shift end' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close clock' }));

    await waitFor(() =>
      expect(mockedApi.setTimeEntryClockOut).toHaveBeenCalledWith(77, {
        clock_out: '2026-11-11T21:00:00.000Z',
        reason: 'Went home at shift end',
        version: 5,
      })
    );
    await waitFor(() => expect(refresh).toHaveBeenCalledWith(true));
    // Hours deliberately left alone -> a warning, not a plain success.
    expect(await screen.findByText(/entered by hand, so they were left alone/)).toBeInTheDocument();
  });
});

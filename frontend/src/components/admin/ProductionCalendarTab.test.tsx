/**
 * Admin > Production Calendar — the editor for GET/PUT /production-calendar and the
 * working-calendar sync (dry run, then confirmed commit).
 *
 * Locks: the whole document is PUT with the version it was read at; a 409 opens a
 * reload prompt and a 422 shows the server's reason verbatim; nothing is optimistic;
 * the sync previews (dry_run=true) before a confirmed commit carrying the preview's
 * calendar version; and the controls follow the server's role gates (save = Admin,
 * sync = Admin/Manager/Supervisor). The api module is mocked at the boundary with the
 * real response shapes (backend/app/schemas/production_calendar.py).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ProductionCalendarTab from './ProductionCalendarTab';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import { unconfiguredCalendar, wercoCalendar } from '../../test-utils/productionCalendarFixtures';
import type { WorkingCalendarSyncResult } from '../../types/productionCalendar';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getProductionCalendar: jest.fn(),
    updateProductionCalendar: jest.fn(),
    syncWorkingCalendarsFromShifts: jest.fn(),
    getWorkCenters: jest.fn(),
  },
}));

const mockUser: { value: any } = { value: null };
jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: () => ({ user: mockUser.value }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const wc = (id: number, code: string, name: string, isActive = true) =>
  ({ id, code, name, is_active: isActive }) as any;

const workCenters = [
  wc(1, 'LASER-1', 'Laser cell 1'),
  wc(2, 'BRAKE-2', 'Press brake 2'),
  wc(101, 'WLD-01', 'Weld bay 1'),
  wc(102, 'WLD-02', 'Weld bay 2'),
  wc(103, 'WLD-03', 'Weld bay 3'),
  wc(104, 'WLD-04', 'Weld bay 4'),
  wc(105, 'WLD-05', 'Weld bay 5'),
  wc(106, 'WLD-06', 'Weld bay 6'),
];

const dryRun: WorkingCalendarSyncResult = {
  dry_run: true,
  configured: true,
  calendar_version: 3,
  changed_count: 1,
  rows: [
    {
      work_center_id: 101,
      work_center_code: 'WLD-01',
      work_center_name: 'Weld bay 1',
      pattern_id: 1,
      pattern_name: 'Weld shop',
      current_version: 0,
      current_weekly_hours: [8, 8, 8, 8, 8, 8, 8],
      new_weekly_hours: [20, 20, 20, 20, 20, 0, 0],
      overrides_kept: 0,
      changed: true,
    },
    {
      work_center_id: 1,
      work_center_code: 'LASER-1',
      work_center_name: 'Laser cell 1',
      pattern_id: 2,
      pattern_name: 'Rest of shop',
      current_version: 2,
      current_weekly_hours: [16, 16, 16, 16, 16, 0, 0],
      new_weekly_hours: [16, 16, 16, 16, 16, 0, 0],
      overrides_kept: 1,
      changed: false,
    },
  ],
};

function asRole(role: string) {
  mockUser.value = { id: 1, role, is_superuser: false, first_name: 'A', last_name: 'B', email: 'a@b.c' };
}

function renderTab() {
  return render(
    <ToastProvider>
      <ProductionCalendarTab />
    </ToastProvider>
  );
}

const axiosError = (status: number, detail: string) =>
  Object.assign(new Error(`Request failed with status code ${status}`), { response: { status, data: { detail } } });

beforeEach(() => {
  jest.clearAllMocks();
  asRole('admin');
  mockApi.getProductionCalendar.mockResolvedValue(wercoCalendar());
  mockApi.getWorkCenters.mockResolvedValue(workCenters);
});

async function loaded() {
  renderTab();
  await screen.findByRole('textbox', { name: /code for W1/i });
}

test('renders the saved calendar: day start, patterns, members and shifts', async () => {
  await loaded();
  expect(screen.getByLabelText(/production day starts/i)).toHaveValue('05:00');
  expect(screen.getByTestId('calendar-version')).toHaveTextContent('v3');
  const weld = screen.getByTestId('pattern-0');
  expect(within(weld).getByLabelText(/pattern name/i)).toHaveValue('Weld shop');
  expect(within(weld).getByRole('checkbox', { name: /WLD-01/ })).toBeChecked();
  expect(within(weld).getByRole('textbox', { name: /code for W2/i })).toHaveValue('W2');
  expect(within(weld).getByLabelText(/end for W2/i)).toHaveValue('02:00');
  // W2 runs past midnight; S2 ends AT midnight, which is not "next day".
  expect(within(weld).getByText('next day')).toBeInTheDocument();
  const rest = screen.getByTestId('pattern-1');
  expect(within(rest).getByRole('radio', { name: /default pattern/i })).toBeChecked();
  expect(within(rest).queryByText('next day')).toBeNull();
  // A weld bay is already in the weld pattern, so the default pattern cannot take it.
  expect(within(rest).getByRole('checkbox', { name: /WLD-01/ })).toBeDisabled();
  // Today's shifts as the server computed them.
  expect(screen.getByTestId('calendar-today')).toHaveTextContent('2026-11-10');
  // Nothing changed yet: Save is off.
  expect(screen.getByRole('button', { name: /save calendar/i })).toBeDisabled();
});

test('Save PUTs the whole document with the version it was read at', async () => {
  const saved = wercoCalendar({ version: 4 });
  saved.patterns[0].shifts[0].planned_minutes = 570;
  mockApi.updateProductionCalendar.mockResolvedValue(saved);
  await loaded();

  fireEvent.change(screen.getByRole('spinbutton', { name: /planned minutes for W1/i }), { target: { value: '570' } });
  expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /save calendar/i }));

  await waitFor(() => expect(mockApi.updateProductionCalendar).toHaveBeenCalledTimes(1));
  const body = mockApi.updateProductionCalendar.mock.calls[0][0];
  expect(body.expected_version).toBe(3);
  expect(body.production_day_start).toBe('05:00');
  expect(body.patterns).toHaveLength(2);
  expect(body.patterns[0]).toMatchObject({ id: 1, name: 'Weld shop', work_center_ids: [101, 102, 103, 104, 105, 106] });
  expect(body.patterns[0].shifts[0]).toEqual({
    id: 1,
    code: 'W1',
    name: 'Weld 1st',
    start: '05:00',
    end: '15:30',
    days: [0, 1, 2, 3, 4],
    planned_minutes: 570,
    is_active: true,
    sort_order: 0,
  });
  // Non-optimistic: the page shows what the server returned.
  expect(await screen.findByText(/Production calendar saved \(version 4\)/)).toBeInTheDocument();
  expect(screen.getByTestId('calendar-version')).toHaveTextContent('v4');
  expect(screen.queryByText('Unsaved changes')).toBeNull();
});

test('a new shift is sent without an id', async () => {
  mockApi.updateProductionCalendar.mockResolvedValue(wercoCalendar({ version: 4 }));
  await loaded();
  const weld = screen.getByTestId('pattern-0');
  fireEvent.click(within(weld).getByRole('button', { name: /add shift/i }));
  fireEvent.change(within(weld).getByRole('textbox', { name: /code for weld shop shift 3/i }), {
    target: { value: 'W3' },
  });
  fireEvent.change(within(weld).getByRole('textbox', { name: /name for W3/i }), { target: { value: 'Weld 3rd' } });
  fireEvent.click(screen.getByRole('button', { name: /save calendar/i }));
  await waitFor(() => expect(mockApi.updateProductionCalendar).toHaveBeenCalled());
  const added = mockApi.updateProductionCalendar.mock.calls[0][0].patterns[0].shifts[2];
  expect(added).not.toHaveProperty('id');
  expect(added).toMatchObject({ code: 'W3', name: 'Weld 3rd', start: '07:00', end: '15:30' });
});

test('client-side issues block Save before a round trip', async () => {
  await loaded();
  fireEvent.change(screen.getByRole('textbox', { name: /code for W2/i }), { target: { value: 'w1' } });
  expect(screen.getByText("Shift code 'w1' is used twice.")).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /save calendar/i })).toBeDisabled();
  expect(mockApi.updateProductionCalendar).not.toHaveBeenCalled();
});

test('a 409 opens a reload prompt; Reload re-reads the calendar', async () => {
  mockApi.updateProductionCalendar.mockRejectedValue(
    axiosError(409, 'This production calendar changed. Reload it before saving.')
  );
  await loaded();
  fireEvent.change(screen.getByLabelText(/production day starts/i), { target: { value: '04:30' } });
  fireEvent.click(screen.getByRole('button', { name: /save calendar/i }));

  const dialog = await screen.findByRole('dialog', { name: /production calendar changed/i });
  expect(mockApi.getProductionCalendar).toHaveBeenCalledTimes(1);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Reload' }));
  await waitFor(() => expect(mockApi.getProductionCalendar).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByLabelText(/production day starts/i)).toHaveValue('05:00'));
});

test('a 422 shows the server reason verbatim and keeps the edits', async () => {
  const detail = "Shift 'W1' is used by OEE records; retire it with is_active: false instead of removing it.";
  mockApi.updateProductionCalendar.mockRejectedValue(axiosError(422, detail));
  await loaded();
  fireEvent.click(screen.getByRole('button', { name: /remove W1/i }));
  fireEvent.click(screen.getByRole('button', { name: /save calendar/i }));
  const alerts = await screen.findAllByText(detail);
  expect(alerts.length).toBeGreaterThan(0);
  expect(screen.queryByRole('textbox', { name: /code for W1/i })).toBeNull();
  expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
});

test('sync: preview writes nothing, then a confirmed commit carries the preview version', async () => {
  mockApi.syncWorkingCalendarsFromShifts
    .mockResolvedValueOnce(dryRun)
    .mockResolvedValueOnce({ ...dryRun, dry_run: false });
  await loaded();

  fireEvent.click(screen.getByRole('button', { name: /preview sync/i }));
  const table = await screen.findByRole('table', { name: 'Sync preview' });
  expect(mockApi.syncWorkingCalendarsFromShifts).toHaveBeenLastCalledWith({ dry_run: true, expected_version: 3 });
  const weldRow = within(table).getByText('WLD-01').closest('tr') as HTMLElement;
  expect(within(weldRow).getByText('New calendar')).toBeInTheDocument();
  expect(within(weldRow).getAllByText('20')).toHaveLength(5);
  const laserRow = within(table).getByText('LASER-1').closest('tr') as HTMLElement;
  expect(within(laserRow).getByText('No change')).toBeInTheDocument();
  expect(screen.getByText(/1 of 2 work centers change/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: /commit 1 change/i }));
  const confirm = await screen.findByRole('dialog', { name: /write new working hours/i });
  expect(mockApi.syncWorkingCalendarsFromShifts).toHaveBeenCalledTimes(1);
  fireEvent.click(within(confirm).getByRole('button', { name: 'Commit' }));

  await waitFor(() =>
    expect(mockApi.syncWorkingCalendarsFromShifts).toHaveBeenLastCalledWith({ dry_run: false, expected_version: 3 })
  );
  expect(await screen.findByRole('table', { name: 'Sync result' })).toBeInTheDocument();
  expect(screen.getByText('Written')).toBeInTheDocument();
  expect(screen.getByText(/Working calendars updated for 1 work center\./)).toBeInTheDocument();
});

test('a commit refused 409 drops the stale preview', async () => {
  mockApi.syncWorkingCalendarsFromShifts
    .mockResolvedValueOnce(dryRun)
    .mockRejectedValueOnce(axiosError(409, 'This production calendar changed. Reload it before saving.'));
  await loaded();
  fireEvent.click(screen.getByRole('button', { name: /preview sync/i }));
  await screen.findByRole('table', { name: 'Sync preview' });
  fireEvent.click(screen.getByRole('button', { name: /commit 1 change/i }));
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Commit' }));
  expect(await screen.findByText(/Run the preview again\./)).toBeInTheDocument();
  expect(screen.queryByRole('table', { name: 'Sync preview' })).toBeNull();
});

test('the sync waits for unsaved edits to be saved', async () => {
  await loaded();
  fireEvent.change(screen.getByLabelText(/production day starts/i), { target: { value: '04:30' } });
  expect(screen.getByRole('button', { name: /preview sync/i })).toBeDisabled();
  expect(screen.getByText(/Save the calendar first/)).toBeInTheDocument();
});

test('an unconfigured company explains the default and cannot sync yet', async () => {
  mockApi.getProductionCalendar.mockResolvedValue(unconfiguredCalendar());
  renderTab();
  expect(await screen.findByText(/Nothing is configured yet/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /preview sync/i })).toBeDisabled();
  expect(screen.getByRole('button', { name: /add pattern/i })).toBeEnabled();
});

test('a manager may sync but not edit; an operator may do neither', async () => {
  asRole('manager');
  const { unmount } = renderTab();
  await screen.findByRole('textbox', { name: /code for W1/i });
  expect(screen.getByRole('textbox', { name: /code for W1/i })).toBeDisabled();
  expect(screen.queryByRole('button', { name: /save calendar/i })).toBeNull();
  expect(screen.getByText(/Only an Administrator can change/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /preview sync/i })).toBeEnabled();
  unmount();

  asRole('operator');
  renderTab();
  await screen.findByRole('textbox', { name: /code for W1/i });
  expect(screen.queryByRole('button', { name: /preview sync/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /save calendar/i })).toBeNull();
});

test('a failed load offers Retry', async () => {
  mockApi.getProductionCalendar.mockRejectedValueOnce(axiosError(500, 'boom'));
  renderTab();
  fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
  await screen.findByRole('textbox', { name: /code for W1/i });
});

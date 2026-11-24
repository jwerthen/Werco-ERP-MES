/**
 * SystemHealthTab — Admin > System Health (GET /admin/system-health).
 *
 * Fixtures are typed against SystemHealth, which mirrors
 * backend/app/schemas/system_health.py: a Redis outage is a 200 with
 * `redis_available: false` (rendered as "unknown"), and only a failed request
 * renders the error state with Retry.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SystemHealthTab, { armedState, formatAge, humanizeJobName } from './SystemHealthTab';
import api from '../../services/api';
import type { SystemHealth } from '../../types/systemHealth';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getSystemHealth: jest.fn() },
}));

const mockApi = api as jest.Mocked<typeof api>;

const healthy = (overrides: Partial<SystemHealth> = {}): SystemHealth => ({
  generated_at: '2026-11-23T15:00:00Z',
  redis_available: true,
  worker: {
    status: 'alive',
    heartbeat_age_seconds: 12,
    last_heartbeat_at: '2026-11-23T14:59:48Z',
    release: '0123456789abcdef0123456789abcdef01234567',
    instance_count: 1,
  },
  schedule_timezone: 'America/Chicago',
  schedule_published_at: '2026-11-23T08:00:00Z',
  cron_spec: 'none',
  cron_jobs: [
    {
      name: 'run_oee_auto_calc_job',
      armed: false,
      schedule: 'Daily 02:30',
      next_run_at: null,
      last_run: {
        started_at: '2026-11-22T08:30:00Z',
        finished_at: '2026-11-22T08:30:05Z',
        outcome: 'success',
        error_type: null,
        counts: { records_created: 4 },
      },
    },
    {
      name: 'relay_pending_notifications_job',
      armed: true,
      schedule: 'Every 5 min',
      next_run_at: '2026-11-23T15:05:00Z',
      last_run: {
        started_at: '2026-11-23T14:55:00Z',
        finished_at: '2026-11-23T14:55:01Z',
        outcome: 'failed',
        error_type: 'OperationalError',
        counts: {},
      },
    },
  ],
  backlog: {
    unnotified_events: 3,
    unnotified_events_stranded: 1,
    queued_digest_items: 7,
    failed_emails: 2,
    failed_email_window_days: 7,
  },
  ...overrides,
});

const renderTab = () =>
  render(
    <MemoryRouter>
      <SystemHealthTab />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
});

test('helpers: armed state, job name and heartbeat age', () => {
  expect(armedState(true)).toBe('on');
  expect(armedState(false)).toBe('off');
  expect(armedState(null)).toBe('unknown');
  expect(humanizeJobName('run_oee_auto_calc_job')).toBe('Run oee auto calc');
  expect(formatAge(null)).toBe('—');
  expect(formatAge(12.4)).toBe('12 s ago');
  expect(formatAge(600)).toBe('10 min ago');
});

test('renders the worker, every cron job with armed state and last result, and the backlog', async () => {
  mockApi.getSystemHealth.mockResolvedValue(healthy());
  renderTab();

  expect(await screen.findByText('Running')).toBeInTheDocument();
  expect(screen.getByText('12 s ago')).toBeInTheDocument();
  expect(screen.getByText('01234567')).toBeInTheDocument();
  expect(screen.getByText('none')).toBeInTheDocument();
  expect(screen.getByText(/Times in America\/Chicago/)).toBeInTheDocument();

  const table = screen.getByRole('table');
  const oeeRow = within(table).getByText('run_oee_auto_calc_job').closest('tr') as HTMLElement;
  expect(within(oeeRow).getByText('off')).toBeInTheDocument();
  expect(within(oeeRow).getByText('Daily 02:30')).toBeInTheDocument();
  expect(within(oeeRow).getByText('success')).toBeInTheDocument();
  expect(within(oeeRow).getByText('records created: 4')).toBeInTheDocument();

  const relayRow = within(table).getByText('relay_pending_notifications_job').closest('tr') as HTMLElement;
  expect(within(relayRow).getByText('on')).toBeInTheDocument();
  expect(within(relayRow).getByText('failed')).toBeInTheDocument();
  expect(within(relayRow).getByText('OperationalError')).toBeInTheDocument();

  expect(screen.getByText('Waiting to notify').nextSibling).toHaveTextContent('3');
  expect(screen.getByText('Queued digest items').nextSibling).toHaveTextContent('7');
  expect(screen.getByText('Failed emails').nextSibling).toHaveTextContent('2');
  expect(screen.getByRole('link', { name: 'View email activity' })).toHaveAttribute('href', '/notifications');
  // A read-only page: nothing here switches a job on.
  expect(screen.queryByRole('button', { name: /run now|^arm$|enable|switch on/i })).not.toBeInTheDocument();
});

test('Redis unavailable renders as unknown, not as an error', async () => {
  mockApi.getSystemHealth.mockResolvedValue(
    healthy({
      redis_available: false,
      worker: {
        status: 'unknown',
        heartbeat_age_seconds: null,
        last_heartbeat_at: null,
        release: null,
        instance_count: 0,
      },
      schedule_timezone: null,
      schedule_published_at: null,
      cron_spec: null,
      cron_jobs: [],
    })
  );
  renderTab();

  expect(await screen.findByRole('status')).toHaveTextContent(/Redis could not be read/);
  expect(screen.getByText('Scheduled jobs unknown')).toBeInTheDocument();
  // cron_spec null with an unknown worker is "Unknown", never "every job armed".
  expect(screen.queryByText(/every job armed/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  // The backlog still comes from the database.
  expect(screen.getByText('Failed emails').nextSibling).toHaveTextContent('2');
});

test('no published registry with Redis up explains the missing worker', async () => {
  mockApi.getSystemHealth.mockResolvedValue(
    healthy({
      worker: {
        status: 'not_seen',
        heartbeat_age_seconds: null,
        last_heartbeat_at: null,
        release: null,
        instance_count: 0,
      },
      cron_jobs: [],
    })
  );
  renderTab();
  expect(await screen.findByText('No worker seen')).toBeInTheDocument();
  expect(screen.getByText('No scheduled jobs published')).toBeInTheDocument();
});

test('a failed request shows the error state and Retry re-reads', async () => {
  mockApi.getSystemHealth.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce(healthy());
  renderTab();

  expect(await screen.findByText('System health could not be loaded.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /retry/i }));
  await waitFor(() => expect(mockApi.getSystemHealth).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('Running')).toBeInTheDocument();
});

test('a company switch (auth-token change) re-reads the active company health', async () => {
  mockApi.getSystemHealth.mockResolvedValue(healthy());
  renderTab();
  expect(await screen.findByText('Running')).toBeInTheDocument();
  expect(mockApi.getSystemHealth).toHaveBeenCalledTimes(1);

  window.dispatchEvent(new Event('werco:auth-token-changed'));
  await waitFor(() => expect(mockApi.getSystemHealth).toHaveBeenCalledTimes(2));
});

test('an unset WORKER_CRON_JOBS with a live worker reads as every job armed', async () => {
  mockApi.getSystemHealth.mockResolvedValue(healthy({ cron_spec: null }));
  renderTab();
  expect(await screen.findByText('Not set (every job armed)')).toBeInTheDocument();
});

test('a stale response never overwrites a newer one', async () => {
  let resolveFirst: (value: SystemHealth) => void = () => undefined;
  mockApi.getSystemHealth
    .mockImplementationOnce(() => new Promise<SystemHealth>(resolve => (resolveFirst = resolve)))
    .mockResolvedValueOnce(healthy({ cron_spec: 'relay_pending_notifications_job' }));
  renderTab();
  window.dispatchEvent(new Event('werco:auth-token-changed'));
  expect(await screen.findByText('relay_pending_notifications_job', { selector: 'dd span' })).toBeInTheDocument();

  await act(async () => {
    resolveFirst(healthy({ cron_spec: 'none' }));
  });
  expect(mockApi.getSystemHealth).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('none')).not.toBeInTheDocument();
  expect(screen.getByText('relay_pending_notifications_job', { selector: 'dd span' })).toBeInTheDocument();
});

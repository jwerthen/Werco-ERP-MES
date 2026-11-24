/**
 * OeeJobStatusNote — the OEE empty state's line about the nightly OEE job.
 * GET /admin/system-health is Admin-only, so only an Admin asks for it; every
 * other role, and any unknown state, keeps the static advice.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OeeJobStatusNote from './OeeJobStatusNote';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { User } from '../../types';
import type { SystemHealth } from '../../types/systemHealth';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getSystemHealth: jest.fn() },
}));

jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: jest.fn(),
}));

const mockApi = api as jest.Mocked<typeof api>;
const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

const asRole = (role: User['role']) =>
  mockUseAuth.mockReturnValue({ user: { role, is_superuser: false } as User } as ReturnType<typeof useAuth>);

const health = (overrides: Partial<SystemHealth> = {}): SystemHealth => ({
  generated_at: '2026-11-23T15:00:00Z',
  redis_available: true,
  worker: {
    status: 'alive',
    heartbeat_age_seconds: 5,
    last_heartbeat_at: '2026-11-23T14:59:55Z',
    release: null,
    instance_count: 1,
  },
  schedule_timezone: 'America/Chicago',
  schedule_published_at: null,
  cron_spec: 'none',
  cron_jobs: [
    {
      name: 'run_oee_auto_calc_job',
      armed: false,
      schedule: 'Daily 02:30',
      next_run_at: null,
      last_run: null,
    },
  ],
  backlog: {
    unnotified_events: 0,
    unnotified_events_stranded: 0,
    queued_digest_items: 0,
    failed_emails: 0,
    failed_email_window_days: 7,
  },
  ...overrides,
});

const renderNote = () =>
  render(
    <MemoryRouter>
      <OeeJobStatusNote />
    </MemoryRouter>
  );

beforeEach(() => jest.clearAllMocks());

test('a non-admin sees the static advice and never calls the admin endpoint', () => {
  asRole('manager');
  renderNote();
  expect(screen.getByText(/ask an administrator to check the nightly OEE job/)).toBeInTheDocument();
  expect(mockApi.getSystemHealth).not.toHaveBeenCalled();
});

test('an admin sees the live armed state and a link to System Health', async () => {
  asRole('admin');
  mockApi.getSystemHealth.mockResolvedValue(health());
  renderNote();
  expect(await screen.findByText('switched off')).toBeInTheDocument();
  expect(screen.getByText(/no run has been recorded/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'View System Health' })).toHaveAttribute(
    'href',
    '/admin/settings?tab=health'
  );
});

test('an admin with an unknown state keeps the static advice', async () => {
  asRole('admin');
  mockApi.getSystemHealth.mockResolvedValue(
    health({
      cron_jobs: [{ name: 'run_oee_auto_calc_job', armed: null, schedule: null, next_run_at: null, last_run: null }],
    })
  );
  renderNote();
  expect(await screen.findByText(/ask an administrator/)).toBeInTheDocument();
  expect(screen.queryByText('switched off')).not.toBeInTheDocument();
});

/**
 * Admin Settings > System Health: the ?tab=health deep link (the OEE page's
 * "View System Health" link lands here) opens the read-only tab, and a failed
 * health read is the tab's own ErrorState with Retry, not the page-level
 * "Could not load this settings tab".
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import AdminSettings from './AdminSettings';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getSystemHealth: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <AdminSettings />
      </ToastProvider>
    </MemoryRouter>
  );

beforeEach(() => jest.clearAllMocks());

it('opens the System Health tab from the deep link', async () => {
  mockedApi.getSystemHealth.mockResolvedValue({
    generated_at: '2026-11-23T15:00:00Z',
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
    backlog: {
      unnotified_events: 0,
      unnotified_events_stranded: 0,
      queued_digest_items: 0,
      failed_emails: 0,
      failed_email_window_days: 7,
    },
  });
  renderAt('/admin/settings?tab=health');
  expect(await screen.findByRole('heading', { name: 'System health' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /System Health/ })).toHaveAttribute('aria-current', 'page');
  expect(mockedApi.getSystemHealth).toHaveBeenCalledTimes(1);
});

it('a failed health read is the tab error state, not the page-level one', async () => {
  mockedApi.getSystemHealth.mockRejectedValue(new Error('boom'));
  renderAt('/admin/settings?tab=health');
  expect(await screen.findByText('System health could not be loaded.')).toBeInTheDocument();
  expect(screen.queryByText('Could not load this settings tab.')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
});

/**
 * Batch 3 — async-state standardization (AuditLog).
 *
 * Locks the new load-failure / empty-result pattern on the Audit Log page:
 *
 *   1. When the audit-log fetch rejects, the log-table section renders the shared
 *      <ErrorState> (role="alert") instead of a blank table, and clicking Retry
 *      re-runs loadData; on the retry's success the rows render and the error
 *      block clears.
 *   2. When the fetch resolves to an empty list, the page renders the shared
 *      <EmptyState> with its real title ("No audit logs found"), not a bare
 *      "No audit logs" string.
 *
 * AuditLog fires loadData() (getAuditLogs + getAuditSummary) and loadFilters()
 * (getAuditActions + getAuditResourceTypes) on mount; loadData is also the Retry
 * handler. The filter endpoints are mocked to resolve so only the targeted load
 * path drives the assertions.
 */

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import AuditLog from './AuditLog';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getAuditLogs: jest.fn(),
    getAuditSummary: jest.fn(),
    getAuditActions: jest.fn(),
    getAuditResourceTypes: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const summary = {
  period_days: 30,
  total_events: 12,
  failed_events: 0,
  by_action: { CREATE: 8, UPDATE: 4 },
  by_resource: { work_order: 12 },
  top_users: [{ name: 'Alice', count: 12 }],
};

const logEntry = {
  id: 1,
  timestamp: '2026-06-29T12:00:00Z',
  user_name: 'Alice',
  user_email: 'alice@example.com',
  action: 'CREATE',
  resource_type: 'work_order',
  resource_identifier: 'WO-1001',
  description: 'Created work order WO-1001',
  success: 'true',
};

const renderPage = () => render(
  <MemoryRouter>
    <AuditLog />
  </MemoryRouter>
);

describe('AuditLog async-state (Batch 3)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Mount-time filter lookups always resolve; they aren't the subject here.
    mockedApi.getAuditActions.mockResolvedValue(['CREATE', 'UPDATE'] as any);
    mockedApi.getAuditResourceTypes.mockResolvedValue(['work_order'] as any);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders ErrorState on load failure and recovers content on Retry', async () => {
    // First load: the logs fetch rejects → log-table section shows ErrorState.
    mockedApi.getAuditLogs.mockRejectedValueOnce(new Error('boom'));
    mockedApi.getAuditSummary.mockRejectedValueOnce(new Error('boom'));

    renderPage();

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Could not load audit logs.')).toBeInTheDocument();
    // No log row content while errored.
    expect(screen.queryByText('Created work order WO-1001')).not.toBeInTheDocument();

    // Retry: resolve both load endpoints so loadData succeeds.
    mockedApi.getAuditLogs.mockResolvedValueOnce([logEntry] as any);
    mockedApi.getAuditSummary.mockResolvedValueOnce(summary as any);
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));

    // The log row renders and the error block clears.
    expect(await screen.findByText('Created work order WO-1001')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockedApi.getAuditLogs).toHaveBeenCalledTimes(2);
  });

  it('renders EmptyState with its title when the log list is empty', async () => {
    mockedApi.getAuditLogs.mockResolvedValue([] as any);
    mockedApi.getAuditSummary.mockResolvedValue(summary as any);

    renderPage();

    const empty = await screen.findByTestId('empty-state');
    expect(within(empty).getByText('No audit logs found')).toBeInTheDocument();
    // A successful empty load is not an error.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

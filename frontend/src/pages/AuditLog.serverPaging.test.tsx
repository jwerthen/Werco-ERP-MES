/**
 * Batch 4 — AuditLog server pagination (compliance-critical).
 *
 * The audit list endpoint is offset/limit paged, ordered desc(timestamp), and
 * returns NO total count. AuditLog over-fetches one extra row (limit = PAGE_SIZE
 * + 1) purely to detect whether an older page exists, then slices that probe
 * row off before rendering. This is the "no hidden audit rows" invariant: every
 * audit record must remain reachable via Prev/Next, and the over-fetch must not
 * skip a row at the page boundary or surface the probe row to the operator.
 *
 * These tests lock the offset arithmetic:
 *   - initial load     → offset 0,            limit PAGE_SIZE + 1
 *   - Next (page 1→2)  → offset PAGE_SIZE,    limit PAGE_SIZE + 1   (NOT +1: no skip)
 *   - Prev (page 2→1)  → offset 0
 *   - filter change    → resets to offset 0
 *   - the PAGE_SIZE+1th probe row is sliced off and never rendered.
 *
 * api.getAuditLogs is the unit under test; the summary/filter lookups are mocked
 * to resolve so only the paging path drives the assertions.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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

// Must match the PAGE_SIZE constant in AuditLog.tsx. If that changes, this
// number — and the assertions below — must change with it deliberately.
const PAGE_SIZE = 50;

const summary = {
  period_days: 30,
  total_events: 120,
  failed_events: 0,
  by_action: { CREATE: 80, UPDATE: 40 },
  by_resource: { work_order: 120 },
  top_users: [{ name: 'Alice', count: 120 }],
};

// Build a list of distinct entries; identifier WO-<id> is rendered in the
// Resource column, so we can assert exactly which rows are on screen.
const makeEntries = (startId: number, count: number) =>
  Array.from({ length: count }, (_, i) => {
    const id = startId + i;
    return {
      id,
      timestamp: '2026-06-29T12:00:00Z',
      user_name: 'Alice',
      user_email: 'alice@example.com',
      action: 'CREATE',
      resource_type: 'work_order',
      resource_identifier: `WO-${id}`,
      description: `Created work order WO-${id}`,
      success: 'true',
    };
  });

// A full first page: PAGE_SIZE renderable rows + 1 over-fetched probe row.
const firstPagePlusProbe = makeEntries(1, PAGE_SIZE + 1); // WO-1 .. WO-51
// The second page starts at the row AFTER the last *rendered* first-page row.
// First page renders WO-1..WO-50, so page 2 must begin at WO-51 — the probe row
// must NOT have been consumed/skipped.
const secondPage = makeEntries(PAGE_SIZE + 1, 10); // WO-51 .. WO-60

const renderPage = () =>
  render(
    <MemoryRouter>
      <AuditLog />
    </MemoryRouter>
  );

const lastCallParams = () => {
  const calls = mockedApi.getAuditLogs.mock.calls;
  return calls[calls.length - 1][0] as { limit: number; offset: number; search?: string; action?: string; resource_type?: string };
};

describe('AuditLog server pagination (Batch 4)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getAuditSummary.mockResolvedValue(summary as any);
    mockedApi.getAuditActions.mockResolvedValue(['CREATE', 'UPDATE'] as any);
    mockedApi.getAuditResourceTypes.mockResolvedValue(['work_order'] as any);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('initial load over-fetches: offset 0, limit = PAGE_SIZE + 1', async () => {
    mockedApi.getAuditLogs.mockResolvedValue(makeEntries(1, 5) as any);
    renderPage();

    await screen.findByText('Created work order WO-1');
    expect(mockedApi.getAuditLogs).toHaveBeenCalledTimes(1);
    expect(lastCallParams()).toEqual({ limit: PAGE_SIZE + 1, offset: 0 });
  });

  it('slices off the over-fetched probe row: renders PAGE_SIZE rows, not PAGE_SIZE + 1', async () => {
    mockedApi.getAuditLogs.mockResolvedValue(firstPagePlusProbe as any);
    renderPage();

    // The last renderable row (WO-50) is shown...
    await screen.findByText('Created work order WO-50');
    // ...but the over-fetched probe row (WO-51) is sliced off and NOT rendered
    // on page 1 — it lives on page 2.
    expect(screen.queryByText('Created work order WO-51')).not.toBeInTheDocument();
  });

  it('Next refetches at offset PAGE_SIZE (no row skipped at the boundary), Prev returns to offset 0', async () => {
    // Page 1 returns a full page + probe → Next must be enabled.
    mockedApi.getAuditLogs.mockResolvedValueOnce(firstPagePlusProbe as any);
    renderPage();
    await screen.findByText('Created work order WO-50');

    const next = screen.getByRole('button', { name: 'Next page' });
    const prev = screen.getByRole('button', { name: 'Previous page' });
    expect(prev).toBeDisabled(); // page 1
    expect(next).not.toBeDisabled(); // hasNext (51 > 50)

    // Click Next → page 2. offset must be PAGE_SIZE (50), the index right after
    // the last *rendered* first-page row — proving the probe row was not skipped.
    mockedApi.getAuditLogs.mockResolvedValueOnce(secondPage as any);
    fireEvent.click(next);

    await screen.findByText('Created work order WO-51');
    expect(lastCallParams()).toEqual({ limit: PAGE_SIZE + 1, offset: PAGE_SIZE });
    // No gap: WO-51 is the first row of page 2 — it directly follows WO-50.
    expect(screen.queryByText('Created work order WO-50')).not.toBeInTheDocument();

    // Page 2 returned < PAGE_SIZE → no further page; Next disabled, Prev enabled.
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).not.toBeDisabled();

    // Click Prev → back to page 1, offset 0.
    mockedApi.getAuditLogs.mockResolvedValueOnce(firstPagePlusProbe as any);
    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }));

    await screen.findByText('Created work order WO-50');
    expect(lastCallParams()).toEqual({ limit: PAGE_SIZE + 1, offset: 0 });
  });

  it('changing a filter resets to page 0 (offset 0) and carries the filter into the query', async () => {
    // Start on page 2 so we can prove the filter change resets the offset.
    mockedApi.getAuditLogs.mockResolvedValueOnce(firstPagePlusProbe as any);
    renderPage();
    await screen.findByText('Created work order WO-50');

    mockedApi.getAuditLogs.mockResolvedValueOnce(secondPage as any);
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Created work order WO-51');
    expect(lastCallParams().offset).toBe(PAGE_SIZE); // confirm we're on page 2

    // Apply an action filter → must reset to offset 0 (newest page) and include
    // the filter param. The Action <select> is the one carrying "All Actions".
    mockedApi.getAuditLogs.mockResolvedValueOnce(makeEntries(1, 3) as any);
    const actionSelect = screen
      .getByRole('option', { name: 'All Actions' })
      .closest('select') as HTMLSelectElement;
    fireEvent.change(actionSelect, { target: { value: 'UPDATE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply Filters' }));

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ limit: PAGE_SIZE + 1, offset: 0, action: 'UPDATE' })
    );
  });

  it('Next stays disabled when the page is not full (no over-fetch probe returned)', async () => {
    // Exactly fewer than PAGE_SIZE rows → no probe → single page.
    mockedApi.getAuditLogs.mockResolvedValue(makeEntries(1, 3) as any);
    renderPage();

    await screen.findByText('Created work order WO-1');
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
  });
});

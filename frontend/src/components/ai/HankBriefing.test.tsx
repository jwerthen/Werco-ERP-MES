import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankBriefing as Briefing } from '../../types/hank';
import { HankBriefing } from './HankBriefing';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getHankBriefing: jest.fn() },
}));

const mockedApi = jest.mocked(api);
const fixture: Briefing = {
  checked_at: '2026-09-22T13:30:00Z',
  role: 'quality',
  headline: 'Your quality shift',
  summary: 'Two recorded priorities are ready for review.',
  sections: [
    {
      key: 'assigned',
      title: 'Assigned to you',
      description: 'Your assigned quality checks.',
      total: 1,
      truncated: false,
      items: [
        {
          key: 'ncr:14',
          source_kind: 'ncr',
          source_id: 14,
          title: 'Review NCR-0014',
          detail: 'Disposition is needed for the documented mismatch.',
          severity: 'high',
          href: '/quality/ncr/14',
          suggested_action: 'Review the inspection evidence.',
          owner_name: 'Taylor Inspector',
          is_mine: true,
        },
      ],
    },
    {
      key: 'blockers',
      title: 'Open blockers',
      description: 'Recorded blockers relevant to your role.',
      total: 1,
      truncated: false,
      items: [
        {
          key: 'blocker:9',
          source_kind: 'blocker',
          source_id: 9,
          title: 'WO-1007 needs a drawing',
          detail: 'The current drawing is missing.',
          severity: 'medium',
          href: '/work-orders/7',
          suggested_action: 'Open the work order and review its documents.',
          owner_name: 'Jordan Planner',
          is_mine: false,
        },
      ],
    },
  ],
  coverage_notes: ['This check covers recorded priorities; it does not infer missing inspection results.'],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderBriefing() {
  const onNavigate = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankBriefing onNavigate={onNavigate} />
      </MemoryRouter>
    ),
    onNavigate,
  };
}

function setCompany(cid: number) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.signature`);
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  mockedApi.getHankBriefing.mockResolvedValue(fixture);
});

describe('HankBriefing', () => {
  it('shows progress and prevents duplicate refreshes until the live briefing arrives', async () => {
    const pending = deferred<Briefing>();
    mockedApi.getHankBriefing.mockReturnValue(pending.promise);
    renderBriefing();
    expect(screen.getByRole('region', { name: 'My shift briefing' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('Checking the shop…');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(mockedApi.getHankBriefing).toHaveBeenCalledTimes(1);
    expect(mockedApi.getHankBriefing.mock.calls[0][0]).toBeInstanceOf(AbortSignal);
    await act(async () => pending.resolve(fixture));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'My shift briefing' })).toHaveAttribute('aria-busy', 'false');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('renders server priorities, source links, ownership and coverage notes', async () => {
    const { onNavigate } = renderBriefing();
    expect(await screen.findByRole('heading', { name: 'Your quality shift' })).toBeInTheDocument();
    expect(screen.getByText(fixture.summary)).toBeInTheDocument();
    expect(screen.getByText(/Checked/)).toHaveTextContent('8:30');
    expect(screen.getByRole('link', { name: 'Review NCR-0014' })).toHaveAttribute('href', '/quality/ncr/14');
    expect(screen.getByText('Next: Review the inspection evidence.')).toBeInTheDocument();
    expect(screen.getByText('Owner: Jordan Planner')).toBeInTheDocument();
    expect(screen.queryByText('Owner: Taylor Inspector')).not.toBeInTheDocument();
    expect(screen.getByText(fixture.coverage_notes[0])).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'WO-1007 needs a drawing' }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    const inboxLink = screen.getByRole('link', { name: 'Open Action Inbox' });
    expect(inboxLink).toHaveAttribute('href', '/action-inbox');
    fireEvent.click(inboxLink);
    expect(onNavigate).toHaveBeenCalledTimes(2);
  });

  it('distinguishes empty sections from bounded result lists', async () => {
    mockedApi.getHankBriefing.mockResolvedValue({
      ...fixture,
      sections: [
        { ...fixture.sections[0], total: 0, items: [] },
        { ...fixture.sections[1], total: 51, truncated: true },
      ],
    });
    renderBriefing();
    const empty = await screen.findByRole('region', { name: 'Assigned to you' });
    expect(within(empty).getByText('Nothing needing attention in this check.')).toBeInTheDocument();
    expect(within(empty).queryByRole('link')).not.toBeInTheDocument();
    const bounded = screen.getByRole('region', { name: 'Open blockers' });
    expect(within(bounded).getByText('51+')).toBeInTheDocument();
    expect(within(bounded).getByText(/Showing the first 1\./)).toBeInTheDocument();
    expect(within(bounded).getByRole('link', { name: 'Review Action Inbox' })).toHaveAttribute('href', '/action-inbox');
  });

  it.each([
    ['shipping', 'Review Shipping', '/shipping'],
    ['my_work', 'Review work orders', '/work-orders'],
  ])('links a truncated %s section to the correct source workspace', async (key, name, href) => {
    mockedApi.getHankBriefing.mockResolvedValue({
      ...fixture,
      sections: [{ ...fixture.sections[1], key, total: 9, truncated: true }],
    });
    renderBriefing();
    expect(await screen.findByRole('link', { name })).toHaveAttribute('href', href);
  });

  it('labels active time as clocked in rather than an assigned task', async () => {
    mockedApi.getHankBriefing.mockResolvedValue({
      ...fixture,
      sections: [
        {
          ...fixture.sections[0],
          key: 'my_work',
          title: 'Your clocked work',
          items: [{ ...fixture.sections[0].items[0], source_kind: 'active_work' }],
        },
      ],
    });
    renderBriefing();
    expect(await screen.findByText('Clocked in')).toBeInTheDocument();
    expect(screen.queryByText('Assigned to you')).not.toBeInTheDocument();
  });

  it('shows an error and allows retrying the first load', async () => {
    mockedApi.getHankBriefing.mockRejectedValueOnce(new Error('Unavailable'));
    renderBriefing();
    expect(await screen.findByRole('alert')).toHaveTextContent('Your shift briefing could not be loaded. Try again.');
    expect(screen.queryByText(fixture.summary)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(fixture.summary);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockedApi.getHankBriefing).toHaveBeenCalledTimes(2);
  });

  it('labels the previous successful briefing when refreshing fails and replaces it on recovery', async () => {
    renderBriefing();
    await screen.findByText(fixture.summary);
    mockedApi.getHankBriefing.mockRejectedValueOnce(new Error('Offline'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(fixture.summary)).toBeInTheDocument();
    expect(screen.getByText('Showing the last successful briefing.')).toBeInTheDocument();
    mockedApi.getHankBriefing.mockResolvedValueOnce({ ...fixture, summary: 'Priorities have changed.', sections: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Priorities have changed.');
    expect(screen.queryByText(fixture.summary)).not.toBeInTheDocument();
    expect(screen.queryByText('Showing the last successful briefing.')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Review NCR-0014' })).not.toBeInTheDocument();
  });

  it('does not render a pending result from the previous company', async () => {
    setCompany(4);
    const pending = deferred<Briefing>();
    mockedApi.getHankBriefing.mockReturnValue(pending.promise);
    renderBriefing();
    setCompany(5);
    await act(async () => pending.resolve(fixture));
    expect(screen.queryByText(fixture.summary)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Review NCR-0014' })).not.toBeInTheDocument();
  });

  it('aborts an unmounted request and does not let its late response overwrite a fresh briefing', async () => {
    const old = deferred<Briefing>();
    mockedApi.getHankBriefing.mockReturnValueOnce(old.promise);
    const { unmount } = renderBriefing();
    const oldSignal = mockedApi.getHankBriefing.mock.calls[0][0];
    expect(oldSignal?.aborted).toBe(false);
    unmount();
    expect(oldSignal?.aborted).toBe(true);
    mockedApi.getHankBriefing.mockResolvedValueOnce({
      ...fixture,
      headline: 'Fresh shift briefing',
      summary: 'Current priorities only.',
    });
    renderBriefing();
    await screen.findByText('Current priorities only.');
    await act(async () => old.resolve(fixture));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Fresh shift briefing' })).toBeInTheDocument());
    expect(screen.queryByText(fixture.summary)).not.toBeInTheDocument();
  });
});

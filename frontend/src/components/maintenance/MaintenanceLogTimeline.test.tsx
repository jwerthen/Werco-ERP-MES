/**
 * The maintenance log timeline: every entry's type label, Central date, performer, description and
 * (only when there was one) cost, in the order the server sent them; and the shared loading, error
 * and empty states.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MaintenanceLogTimeline } from './MaintenanceLogTimeline';
import type { MaintenanceLogEntry } from '../../types/maintenance';

/** An entry recorded the moment it happened, unless the override says otherwise. */
const entry = (overrides: Partial<MaintenanceLogEntry>): MaintenanceLogEntry => {
  const eventDate = overrides.event_date === undefined ? '2026-11-19T14:30:00Z' : overrides.event_date;
  return {
    id: 1,
    work_center_id: 3,
    asset_id: null,
    target_type: 'work_center',
    target_code: 'MILL-1',
    target_name: 'Synthetic Mill',
    maintenance_wo_id: 41,
    event_type: 'note',
    description: null,
    performed_by: 7,
    performed_by_name: 'Pat Synthetic',
    cost: 0,
    created_at: eventDate,
    ...overrides,
    event_date: eventDate,
  };
};

const NEWEST_FIRST: MaintenanceLogEntry[] = [
  entry({
    id: 3,
    event_type: 'completed',
    description: 'Completed MWO-2026-0041',
    event_date: '2026-11-19T20:05:00Z',
  }),
  entry({
    id: 2,
    event_type: 'part_replacement',
    description: 'Replaced drive belt\nOld one glazed',
    cost: 42.5,
    performed_by_name: 'Sam Example',
    event_date: '2026-07-01T13:30:00Z',
  }),
  entry({ id: 1, event_type: 'belt_check by vendor', performed_by_name: null, event_date: '2026-11-19T14:30:00Z' }),
];

const renderTimeline = (props: Partial<React.ComponentProps<typeof MaintenanceLogTimeline>> = {}) =>
  render(
    <MaintenanceLogTimeline
      logs={NEWEST_FIRST}
      loading={false}
      error={null}
      onRetry={jest.fn()}
      label="Maintenance log for MWO-2026-0041"
      {...props}
    />
  );

describe('MaintenanceLogTimeline', () => {
  it('shows each entry’s label, Central date, performer, description and cost, in the server’s order', () => {
    renderTimeline();
    const list = screen.getByRole('list', { name: 'Maintenance log for MWO-2026-0041' });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(3);

    // 20:05 UTC in November is 2:05 PM CST.
    expect(items[0]).toHaveTextContent('Completed');
    expect(items[0]).toHaveTextContent('Nov 19, 2026, 2:05 PM');
    expect(items[0]).toHaveTextContent('by Pat Synthetic');
    expect(items[0]).not.toHaveTextContent('$');

    // 13:30 UTC in July is 8:30 AM CDT.
    expect(items[1]).toHaveTextContent('Part replacement');
    expect(items[1]).toHaveTextContent('Jul 1, 2026, 8:30 AM');
    expect(items[1]).toHaveTextContent('by Sam Example');
    expect(items[1]).toHaveTextContent('Replaced drive belt Old one glazed');
    expect(items[1]).toHaveTextContent('$42.50');
    expect(within(items[1]).getByText('Jul 1, 2026, 8:30 AM')).toHaveAttribute('dateTime', '2026-07-01T13:30:00Z');

    // A legacy free-text type still reads; a performer the server cannot name is said so.
    expect(items[2]).toHaveTextContent('Belt check by vendor');
    expect(items[2]).toHaveTextContent('Nov 19, 2026, 8:30 AM');
    expect(items[2]).toHaveTextContent('by an unknown user');

    // Entered as it happened: no API-token tag, no separate "recorded" time.
    for (const item of items) {
      expect(item).not.toHaveTextContent('via API token');
      expect(item).not.toHaveTextContent('recorded');
    }
  });

  it('tags an entry a standing API token filed, in words a screen reader reads', () => {
    renderTimeline({
      logs: [entry({ id: 5, event_type: 'inspection', description: 'Forks checked', filed_via_api_token: true })],
    });
    const item = screen.getByRole('listitem');
    expect(item).toHaveTextContent('by Pat Synthetic');
    expect(within(item).getByText('via API token')).toBeVisible();
  });

  it('shows when a backdated entry was recorded, once that is more than five minutes from when it happened', () => {
    renderTimeline({
      logs: [
        // Happened 8:30 AM CST, recorded the next afternoon at 1:00 PM.
        entry({ id: 6, event_date: '2026-11-19T14:30:00Z', created_at: '2026-11-20T19:00:00Z' }),
        // Recorded four minutes after: close enough to say nothing.
        entry({ id: 7, event_date: '2026-11-19T14:30:00Z', created_at: '2026-11-19T14:34:00Z' }),
      ],
    });
    const [backdated, prompt] = screen.getAllByRole('listitem');
    expect(backdated).toHaveTextContent('Nov 19, 2026, 8:30 AM');
    expect(backdated).toHaveTextContent('recorded Nov 20, 2026, 1:00 PM');
    expect(within(backdated).getByText('Nov 20, 2026, 1:00 PM')).toHaveAttribute('dateTime', '2026-11-20T19:00:00Z');
    expect(prompt).not.toHaveTextContent('recorded');
  });

  it('shows a loading line before the first answer', () => {
    renderTimeline({ logs: [], loading: true });
    expect(screen.getByRole('status')).toHaveTextContent('Loading the maintenance log');
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('shows the empty state when there are no entries', () => {
    renderTimeline({ logs: [], emptyDescription: 'Entries filed against this work order appear here.' });
    expect(screen.getByTestId('empty-state')).toHaveTextContent('No log entries yet');
    expect(screen.getByTestId('empty-state')).toHaveTextContent('Entries filed against this work order appear here.');
  });

  it('shows the error with a Retry that re-reads', () => {
    const onRetry = jest.fn();
    renderTimeline({ logs: [], error: 'Maintenance work order not found', onRetry });
    expect(screen.getByRole('alert')).toHaveTextContent('Maintenance work order not found');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

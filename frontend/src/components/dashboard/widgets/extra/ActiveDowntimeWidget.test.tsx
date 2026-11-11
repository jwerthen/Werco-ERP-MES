import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { ActiveDowntimeEvent } from '../../../../types/dashboardWidgetData';
import { ActiveDowntimeWidget } from './ActiveDowntimeWidget';

function event(overrides: Partial<ActiveDowntimeEvent> = {}): ActiveDowntimeEvent {
  return {
    id: 5,
    work_center_id: 3,
    work_order_id: 88,
    // Zone-less on purpose: DowntimeEventResponse is not a UTCModel. toDate reads it as UTC.
    start_time: '2026-11-11T13:30:00',
    end_time: null,
    duration_minutes: null,
    category: 'mechanical',
    planned_type: 'unplanned',
    reason_code: 'SPINDLE',
    description: 'Spindle alarm',
    resolution: null,
    reported_by: 2,
    resolved_by: null,
    created_at: '2026-11-11T13:30:00',
    updated_at: null,
    work_center: { id: 3, code: 'CNC-2', name: 'Haas VF-2' },
    reporter: { id: 2, username: null, full_name: 'Lena Chen' },
    resolver: null,
    work_order: { id: 88, wo_number: 'WO-5088' },
    ...overrides,
  };
}

describe('ActiveDowntimeWidget', () => {
  it('lists open events with elapsed time from a zone-less start parsed as UTC, shown in Central', () => {
    // renderWithDashboardContext pins nowMs to 2026-11-11T15:00:00Z → 1h 30m down.
    renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: {
        downtime: readySource([
          event(),
          event({
            id: 6,
            planned_type: 'planned',
            category: 'planned_maintenance',
            reason_code: null,
            work_order: null,
          }),
        ]),
      },
    });

    expect(screen.getByText('1 down')).toBeInTheDocument();
    expect(screen.getByText('2 open events total')).toBeInTheDocument();
    expect(screen.getAllByText('Haas VF-2')).toHaveLength(2);
    expect(screen.getAllByText('1h 30m')).toHaveLength(2);
    // 13:30 UTC is 7:30 AM Central (CST in November).
    expect(screen.getAllByText('since 7:30 AM')).toHaveLength(2);
    expect(screen.getByText(/Mechanical · SPINDLE/)).toBeInTheDocument();
    expect(screen.getByText('Planned maintenance')).toBeInTheDocument();
    expect(screen.getByText('unplanned')).toBeInTheDocument();
    expect(screen.getByText('planned')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'WO-5088' })).toHaveAttribute('href', '/work-orders/88');
    expect(screen.getByRole('link', { name: 'Open downtime' })).toHaveAttribute('href', '/downtime');
  });

  it('renders the empty state when no machine is down', () => {
    renderWithDashboardContext(<ActiveDowntimeWidget />, { sources: { downtime: readySource([]) } });
    expect(screen.getByText('No machines down')).toBeInTheDocument();
    expect(screen.getByText('0 down')).toBeInTheDocument();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: { downtime: readySource([event()], { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('Haas VF-2')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: { downtime: readySource(null, { error: 'Could not refresh' }) },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Active downtime is unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: { downtime: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders the work order as text when it cannot be opened', () => {
    renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: { downtime: readySource([event()]) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('WO-5088')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('falls back to the work-center id when the relation is missing', () => {
    renderWithDashboardContext(<ActiveDowntimeWidget />, {
      sources: { downtime: readySource([event({ work_center: null })]) },
    });
    expect(screen.getByText('Work center 3')).toBeInTheDocument();
  });
});

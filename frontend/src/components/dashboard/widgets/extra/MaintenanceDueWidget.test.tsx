import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { MaintenanceDashboardResponse } from '../../../../types/dashboardWidgetData';
import { MaintenanceDueWidget } from './MaintenanceDueWidget';

const DASHBOARD: MaintenanceDashboardResponse = {
  overdue_count: 2,
  due_this_week: 5,
  completed_this_month: 7,
  completion_rate: 63.6,
  total_cost_month: 18250.75,
  in_progress: 1,
  work_center_metrics: [
    {
      work_center_id: 1,
      work_center_name: 'Haas VF-2',
      work_center_code: 'CNC-2',
      wo_count: 3,
      mtbf_hours: 240,
      mttr_hours: 2.5,
      total_downtime_minutes: 300,
      total_cost: 9100.5,
    },
  ],
};

describe('MaintenanceDueWidget', () => {
  it('shows the three workload counts and never the cost fields', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />, { sources: { maintenance: readySource(DASHBOARD) } });
    expect(screen.getByText('Overdue').nextSibling).toHaveTextContent('2');
    expect(screen.getByText('Due in 7 days').nextSibling).toHaveTextContent('5');
    expect(screen.getByText('In progress').nextSibling).toHaveTextContent('1');
    expect(screen.queryByText(/18,?250/)).not.toBeInTheDocument();
    expect(screen.queryByText(/9,?100/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open maintenance' })).toHaveAttribute('href', '/maintenance');
  });

  it('renders the empty state when nothing is due', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />, {
      sources: { maintenance: readySource({ ...DASHBOARD, overdue_count: 0, due_this_week: 0, in_progress: 0 }) },
    });
    expect(screen.getByText('No maintenance due')).toBeInTheDocument();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />, {
      sources: { maintenance: readySource(DASHBOARD, { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('Overdue')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<MaintenanceDueWidget />, {
      sources: { maintenance: readySource(null, { error: 'Could not refresh' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />, {
      sources: { maintenance: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('omits the Maintenance link when it cannot be opened', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />, {
      sources: { maintenance: readySource(DASHBOARD) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('Overdue')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('reads as loading before the source arrives', () => {
    renderWithDashboardContext(<MaintenanceDueWidget />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading maintenance…');
  });
});

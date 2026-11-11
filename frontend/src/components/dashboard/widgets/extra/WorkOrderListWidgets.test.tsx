import React from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { WorkOrderBrowseResponse } from '../../../../types/workOrderBrowse';
import type { WorkOrderSummary } from '../../../../types';
import { WorkOrdersDueTodayWidget, WorkOrdersOverdueWidget } from './WorkOrderListWidgets';

function workOrder(overrides: Partial<WorkOrderSummary> = {}): WorkOrderSummary {
  return {
    id: 41,
    version: 3,
    work_order_number: 'WO-1041',
    part_id: 7,
    work_order_type: 'production',
    part_number: 'BRK-220',
    part_name: 'Bracket, mounting',
    status: 'in_progress',
    priority: 3,
    quantity_ordered: 10,
    quantity_complete: 2,
    due_date: '2026-11-04',
    customer_name: 'Acme Aero',
    customer_po: 'PO-99812',
    ...overrides,
  };
}

function browse(items: WorkOrderSummary[], total = items.length): WorkOrderBrowseResponse {
  return {
    items,
    total,
    skip: 0,
    limit: 8,
    has_next: total > items.length,
    stats: { overdue: total, in_progress: 1, due_today: 0 },
    customers: ['Acme Aero'],
    customers_truncated: false,
    group_totals: {},
    type_counts: { production: total, laser_cutting: 0 },
  };
}

describe('WorkOrdersOverdueWidget', () => {
  it('lists work order, part, due date and status with the uncapped total — never customer data', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: {
        workOrdersOverdue: readySource(
          browse(
            [workOrder(), workOrder({ id: 42, work_order_number: 'WO-1042', status: 'on_hold', part_number: null })],
            12
          )
        ),
      },
    });

    expect(screen.getByRole('heading', { name: 'Overdue Work Orders' })).toBeInTheDocument();
    expect(screen.getByText('12 overdue')).toBeInTheDocument();
    expect(screen.getByText('12 work orders total')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'WO-1041' })).toHaveAttribute('href', '/work-orders/41');
    expect(screen.getByText('BRK-220')).toBeInTheDocument();
    // Falls back to the part name when a row has no part number.
    expect(screen.getByText('Bracket, mounting', { selector: 'span' })).toBeInTheDocument();
    expect(screen.getAllByText('Nov 4')).toHaveLength(2);
    expect(screen.getByText('in progress')).toBeInTheDocument();
    expect(screen.getByText('on hold')).toBeInTheDocument();
    expect(screen.queryByText(/Acme Aero/)).not.toBeInTheDocument();
    expect(screen.queryByText(/PO-99812/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View all overdue work orders' })).toHaveAttribute(
      'href',
      '/work-orders?scope=overdue&cots=1'
    );
  });

  it('renders the empty state when nothing is overdue', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: { workOrdersOverdue: readySource(browse([])) },
    });
    expect(screen.getByText('No overdue work orders')).toBeInTheDocument();
    expect(screen.getByText('0 overdue')).toBeInTheDocument();
  });

  it('keeps the last verified rows and says the refresh failed', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: { workOrdersOverdue: readySource(browse([workOrder()]), { error: 'Could not refresh' }) },
    });
    expect(screen.getByRole('link', { name: 'WO-1041' })).toBeInTheDocument();
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows an error with a Retry that forces a refresh when there is no data', () => {
    const { refresh } = renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: { workOrdersOverdue: readySource(null, { error: 'Could not refresh' }) },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Overdue work orders are unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('says the panel is not available for the role on a 403', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: { workOrdersOverdue: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reads as loading until the source has a result', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading overdue work orders…');
  });

  it('renders rows as plain text and drops View all when the viewer cannot open them', () => {
    renderWithDashboardContext(<WorkOrdersOverdueWidget />, {
      sources: { workOrdersOverdue: readySource(browse([workOrder()])) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('WO-1041')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

describe('WorkOrdersDueTodayWidget', () => {
  it('reads its own source and links to the due-today scope', () => {
    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: {
        workOrdersDueToday: readySource(browse([workOrder({ due_date: '2026-11-11', status: 'released' })], 3)),
      },
    });
    const panel = screen.getByRole('heading', { name: 'Work Orders Due Today' }).closest('.card') as HTMLElement;
    expect(within(panel).getByText('3 due')).toBeInTheDocument();
    expect(within(panel).getByText('Nov 11')).toBeInTheDocument();
    expect(within(panel).getByRole('link', { name: 'View all work orders due today' })).toHaveAttribute(
      'href',
      '/work-orders?scope=due_today&cots=1'
    );
  });

  it('renders its own empty state', () => {
    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersDueToday: readySource(browse([])) },
    });
    expect(screen.getByText('Nothing due today')).toBeInTheDocument();
  });

  it('does not read the overdue source', () => {
    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersOverdue: readySource(browse([workOrder()])) },
    });
    expect(screen.getByRole('status')).toHaveTextContent('Loading work orders due today…');
    expect(screen.queryByText('WO-1041')).not.toBeInTheDocument();
  });

  it('handles stale, failed and forbidden states', () => {
    const { refresh, unmount } = renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersDueToday: readySource(null, { error: 'Could not refresh' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
    unmount();

    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersDueToday: readySource(browse([workOrder()]), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('WO-1041')).toBeInTheDocument();
  });

  it('omits links when the viewer cannot open them', () => {
    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersDueToday: readySource(browse([workOrder()])) },
      env: { canOpen: () => false },
    });
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('is forbidden-safe', () => {
    renderWithDashboardContext(<WorkOrdersDueTodayWidget />, {
      sources: { workOrdersDueToday: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
  });
});

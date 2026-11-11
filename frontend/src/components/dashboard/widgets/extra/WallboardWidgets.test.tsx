import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { WallboardResponse } from '../../../../types/wallboard';
import { BlockedJobsWidget, LateJobsWidget, ShippingOutlookWidget, TodaysOutputWidget } from './WallboardWidgets';

function wallboard(overrides: Partial<WallboardResponse> = {}): WallboardResponse {
  return {
    work_centers: [],
    late_wos: [
      { wo_number: 'WO-2001', part_number: 'SHAFT-9', due_date: '2026-11-02', days_late: 9, status: 'in_progress' },
      { wo_number: 'WO 2002/A', part_number: null, due_date: null, days_late: 1, status: null },
    ],
    blocked_wos: [
      { wo_number: 'WO-3001', category: 'material_missing', age_hours: 30.5 },
      { wo_number: 'WO-3001', category: 'quality_hold', age_hours: 2 },
    ],
    late_total: 14,
    blocked_total: 5,
    down_total: 1,
    ship: {
      due_today: 4,
      shipped_today: 1,
      due_this_week: 9,
      due_today_rows: [
        { wo_number: 'WO-4001', part_number: 'PLATE-3', promise_date: '2026-11-11', qty_remaining: 12.5 },
      ],
      next_due_date: null,
      next_due_count: 0,
    },
    today: {
      ops_completed: 17,
      pieces_completed: 340,
      wos_completed: 3,
      operators_on_clock: 6,
      hours_logged: 41.25,
      receipts: 2,
      scrap_events: 1,
    },
    quality: { open_ncr_count: 2, newest_ncr_age_days: 1, wos_on_hold: 1 },
    jobs: [],
    jobs_total: 0,
    generated_at: '2026-11-11T15:00:00Z',
    ...overrides,
  };
}

const FAILED = readySource(null, { error: 'Could not refresh' });
const FORBIDDEN = readySource(null, { forbidden: true });

describe('BlockedJobsWidget', () => {
  it('lists blockers oldest first with the uncapped blocker total and search links', () => {
    renderWithDashboardContext(<BlockedJobsWidget />, { sources: { wallboard: readySource(wallboard()) } });
    // The total counts blockers, not distinct work orders (WO-3001 carries two of them).
    expect(screen.getByText('5 blockers')).toBeInTheDocument();
    expect(screen.queryByText(/\d+ blocked$/)).not.toBeInTheDocument();
    expect(screen.getByText('5 open blockers total')).toBeInTheDocument();
    const links = screen.getAllByRole('link', { name: 'WO-3001' });
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute('href', '/work-orders?search=WO-3001&cots=1');
    expect(screen.getByText('Material missing')).toBeInTheDocument();
    expect(screen.getByText('Quality hold')).toBeInTheDocument();
    expect(screen.getByText('1d 6h')).toBeInTheDocument();
    expect(screen.getByText('2h 0m')).toBeInTheDocument();
  });

  it('falls back to the list length when an older backend sends no total', () => {
    renderWithDashboardContext(<BlockedJobsWidget />, {
      sources: { wallboard: readySource(wallboard({ blocked_total: undefined })) },
    });
    expect(screen.getByText('2 blockers')).toBeInTheDocument();
  });

  it('renders the empty state', () => {
    renderWithDashboardContext(<BlockedJobsWidget />, {
      sources: { wallboard: readySource(wallboard({ blocked_wos: [], blocked_total: 0 })) },
    });
    expect(screen.getByText('No blocked work orders')).toBeInTheDocument();
  });

  it('keeps stale data, retries by force without data, and respects a 403', () => {
    const first = renderWithDashboardContext(<BlockedJobsWidget />, {
      sources: { wallboard: readySource(wallboard(), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getAllByText('WO-3001')).toHaveLength(2);
    first.unmount();

    const second = renderWithDashboardContext(<BlockedJobsWidget />, { sources: { wallboard: FAILED } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(second.refresh).toHaveBeenCalledWith(true);
    second.unmount();

    renderWithDashboardContext(<BlockedJobsWidget />, { sources: { wallboard: FORBIDDEN } });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByText(/\d+ blockers?$/)).not.toBeInTheDocument();
  });

  it('renders work-order numbers as text when the list cannot be opened', () => {
    renderWithDashboardContext(<BlockedJobsWidget />, {
      sources: { wallboard: readySource(wallboard()) },
      env: { canOpen: () => false },
    });
    expect(screen.getAllByText('WO-3001')).toHaveLength(2);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

describe('ShippingOutlookWidget', () => {
  it('shows today and this week by promise date with the worst rows', () => {
    renderWithDashboardContext(<ShippingOutlookWidget />, { sources: { wallboard: readySource(wallboard()) } });
    expect(screen.getByText('Due today')).toBeInTheDocument();
    expect(screen.getByText('1/4')).toBeInTheDocument();
    expect(screen.getByText('9')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'WO-4001' })).toHaveAttribute('href', '/work-orders?search=WO-4001&cots=1');
    expect(screen.getByText('12.5 left')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open shipping' })).toHaveAttribute('href', '/warehouse?tab=shipping');
  });

  it('names the next due date when nothing is due today', () => {
    renderWithDashboardContext(<ShippingOutlookWidget />, {
      sources: {
        wallboard: readySource(
          wallboard({
            ship: {
              due_today: 0,
              shipped_today: 0,
              due_this_week: 2,
              due_today_rows: [],
              next_due_date: '2026-11-13',
              next_due_count: 2,
            },
          })
        ),
      },
    });
    expect(screen.getByText(/Next due Nov 13/)).toHaveTextContent('Next due Nov 13 · 2 work orders');
  });

  it('renders the empty state when nothing ships this week', () => {
    renderWithDashboardContext(<ShippingOutlookWidget />, {
      sources: {
        wallboard: readySource(
          wallboard({
            ship: {
              due_today: 0,
              shipped_today: 0,
              due_this_week: 0,
              due_today_rows: [],
              next_due_date: null,
              next_due_count: 0,
            },
          })
        ),
      },
    });
    expect(screen.getByText('Nothing promised to ship this week')).toBeInTheDocument();
  });

  it('says the figures are unavailable when the server dropped the ship block', () => {
    renderWithDashboardContext(<ShippingOutlookWidget />, {
      sources: { wallboard: readySource(wallboard({ ship: null })) },
    });
    expect(screen.getByText('Shipping figures are unavailable right now.')).toBeInTheDocument();
  });

  it('handles stale, failed and forbidden states, and omits links it cannot open', () => {
    const first = renderWithDashboardContext(<ShippingOutlookWidget />, {
      sources: { wallboard: readySource(wallboard(), { error: 'Could not refresh' }) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('WO-4001')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    first.unmount();

    const second = renderWithDashboardContext(<ShippingOutlookWidget />, { sources: { wallboard: FAILED } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(second.refresh).toHaveBeenCalledWith(true);
    second.unmount();

    renderWithDashboardContext(<ShippingOutlookWidget />, { sources: { wallboard: FORBIDDEN } });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

describe('TodaysOutputWidget', () => {
  it('shows the Central-day totals', () => {
    renderWithDashboardContext(<TodaysOutputWidget />, { sources: { wallboard: readySource(wallboard()) } });
    expect(screen.getByText('Operations done').nextSibling).toHaveTextContent('17');
    expect(screen.getByText('Pieces done').nextSibling).toHaveTextContent('340');
    expect(screen.getByText('WOs completed').nextSibling).toHaveTextContent('3');
    expect(screen.getByText('On the clock').nextSibling).toHaveTextContent('6');
    expect(screen.getByText('Hours logged').nextSibling).toHaveTextContent('41.3h');
    expect(screen.getByText('Receipts').nextSibling).toHaveTextContent('2');
    expect(screen.getByText('Scrap events').nextSibling).toHaveTextContent('1');
  });

  it('renders an em dash for hours when the server could not total them', () => {
    renderWithDashboardContext(<TodaysOutputWidget />, {
      sources: { wallboard: readySource(wallboard({ today: { ...wallboard().today!, hours_logged: null } })) },
    });
    expect(screen.getByText('Hours logged').nextSibling).toHaveTextContent('—');
  });

  it('renders empty states for no activity yet and for a dropped block', () => {
    const first = renderWithDashboardContext(<TodaysOutputWidget />, {
      sources: {
        wallboard: readySource(
          wallboard({
            today: {
              ops_completed: 0,
              pieces_completed: 0,
              wos_completed: 0,
              operators_on_clock: 0,
              hours_logged: 0,
              receipts: 0,
              scrap_events: 0,
            },
          })
        ),
      },
    });
    expect(screen.getByText('No activity recorded yet today')).toBeInTheDocument();
    first.unmount();

    renderWithDashboardContext(<TodaysOutputWidget />, {
      sources: { wallboard: readySource(wallboard({ today: null })) },
    });
    expect(screen.getByText("Today's totals are unavailable")).toBeInTheDocument();
  });

  it('handles stale, failed and forbidden states and renders no links', () => {
    const first = renderWithDashboardContext(<TodaysOutputWidget />, {
      sources: { wallboard: readySource(wallboard(), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    first.unmount();

    const second = renderWithDashboardContext(<TodaysOutputWidget />, { sources: { wallboard: FAILED } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(second.refresh).toHaveBeenCalledWith(true);
    second.unmount();

    renderWithDashboardContext(<TodaysOutputWidget />, { sources: { wallboard: FORBIDDEN } });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
  });
});

describe('LateJobsWidget', () => {
  it('lists late work orders by promise date with the uncapped late total', () => {
    renderWithDashboardContext(<LateJobsWidget />, { sources: { wallboard: readySource(wallboard()) } });
    expect(screen.getByText('By promise date (must-ship-by, else due date)')).toBeInTheDocument();
    expect(screen.getByText('14 late')).toBeInTheDocument();
    expect(screen.getByText('14 late work orders total')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'WO-2001' })).toHaveAttribute('href', '/work-orders?search=WO-2001&cots=1');
    // Work-order numbers are URL-encoded into the search link.
    expect(screen.getByRole('link', { name: 'WO 2002/A' })).toHaveAttribute(
      'href',
      '/work-orders?search=WO%202002%2FA&cots=1'
    );
    expect(screen.getByText('Nov 2')).toBeInTheDocument();
    expect(screen.getByText('9d late')).toBeInTheDocument();
    expect(screen.getByText('in progress')).toBeInTheDocument();
  });

  it('renders the empty state', () => {
    renderWithDashboardContext(<LateJobsWidget />, {
      sources: { wallboard: readySource(wallboard({ late_wos: [], late_total: 0 })) },
    });
    expect(screen.getByText('No late jobs')).toBeInTheDocument();
  });

  it('handles stale, failed and forbidden states, and omits links it cannot open', () => {
    const first = renderWithDashboardContext(<LateJobsWidget />, {
      sources: { wallboard: readySource(wallboard(), { error: 'Could not refresh' }) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('WO-2001')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    first.unmount();

    const second = renderWithDashboardContext(<LateJobsWidget />, { sources: { wallboard: FAILED } });
    expect(screen.getByRole('alert')).toHaveTextContent('Shop status is unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(second.refresh).toHaveBeenCalledWith(true);
    second.unmount();

    renderWithDashboardContext(<LateJobsWidget />, { sources: { wallboard: FORBIDDEN } });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
  });

  it('reads as loading before the shared wallboard source arrives', () => {
    renderWithDashboardContext(<LateJobsWidget />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading shop status…');
  });
});

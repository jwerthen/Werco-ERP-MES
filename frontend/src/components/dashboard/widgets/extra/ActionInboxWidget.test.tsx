import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { OperationalInboxItem, OperationalInboxResponse } from '../../../../types/operationsInbox';
import { ActionInboxWidget } from './ActionInboxWidget';

let nextId = 1;
function item(overrides: Partial<OperationalInboxItem> = {}): OperationalInboxItem {
  const id = nextId++;
  return {
    key: `late_work_order:${id}`,
    source_kind: 'late_work_order',
    source_id: id,
    occurrence: '2026-11-10',
    title: `WO-${id} is late`,
    detail: 'Due Nov 9',
    severity: 'high',
    href: `/work-orders/${id}`,
    suggested_action: 'Review schedule',
    owner_id: null,
    owner_name: null,
    next_action: '',
    acknowledged: false,
    snoozed_until: null,
    version: 1,
    can_manage: true,
    ...overrides,
  };
}

function inbox(items: OperationalInboxItem[], truncated: string[] = []): OperationalInboxResponse {
  return { items, assignees: [], checked_at: '2026-11-11T15:00:00Z', truncated_sources: truncated };
}

beforeEach(() => {
  nextId = 1;
});

describe('ActionInboxWidget', () => {
  it('counts by severity and lists the top five high items, excluding snoozed items', () => {
    const highs = Array.from({ length: 6 }, () => item());
    const items = [
      ...highs,
      item({ severity: 'medium', title: 'Low stock: bar' }),
      item({ severity: 'low', title: 'Supplier follow-up' }),
      item({ severity: 'high', title: 'Snoozed late WO', snoozed_until: '2026-11-12T15:00:00Z' }),
      item({ severity: 'medium', title: 'Snoozed medium', snoozed_until: '2026-11-12T15:00:00Z' }),
      item({ severity: 'high', title: 'Acknowledged NCR', acknowledged: true, owner_name: 'Lena Chen' }),
    ];
    renderWithDashboardContext(<ActionInboxWidget />, { sources: { actionInbox: readySource(inbox(items)) } });

    expect(screen.getByText('9 open')).toBeInTheDocument();
    expect(screen.getByText('High').nextSibling).toHaveTextContent('7');
    expect(screen.getByText('Medium').nextSibling).toHaveTextContent('1');
    expect(screen.getByText('Low').nextSibling).toHaveTextContent('1');
    expect(screen.queryByText('Snoozed late WO')).not.toBeInTheDocument();
    const listed = screen.getAllByRole('link', { name: /is late/ });
    expect(listed).toHaveLength(5);
    expect(listed[0]).toHaveAttribute('href', '/work-orders/1');
    expect(screen.getByRole('link', { name: 'Open action inbox' })).toHaveAttribute('href', '/action-inbox');
  });

  it('marks counts "N+" when the server truncated a source', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item(), item({ severity: 'low' })], ['low_stock'])) },
    });
    expect(screen.getByText('2+ open')).toBeInTheDocument();
    expect(screen.getByText('High').nextSibling).toHaveTextContent('1+');
    expect(screen.getByText('Medium').nextSibling).toHaveTextContent(/^0$/);
  });

  it('says so when nothing is high severity', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item({ severity: 'medium' })])) },
    });
    expect(screen.getByText('No high-severity issues.')).toBeInTheDocument();
  });

  it('renders the empty state when only snoozed items remain', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item({ snoozed_until: '2026-11-12T15:00:00Z' })])) },
    });
    expect(screen.getByText('Nothing needs action')).toBeInTheDocument();
    expect(screen.getByText('0 open')).toBeInTheDocument();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item()]), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('WO-1 is late')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(null, { error: 'Could not refresh' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders item titles as text when their destination cannot be opened', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item()])) },
      env: { canOpen: href => href === '/action-inbox' },
    });
    expect(screen.getByText('WO-1 is late')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'WO-1 is late' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open action inbox' })).toBeInTheDocument();
  });

  it('omits every link when nothing can be opened', () => {
    renderWithDashboardContext(<ActionInboxWidget />, {
      sources: { actionInbox: readySource(inbox([item()])) },
      env: { canOpen: () => false },
    });
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

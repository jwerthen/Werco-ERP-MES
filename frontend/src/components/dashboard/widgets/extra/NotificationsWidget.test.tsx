import React from 'react';
import { act, fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { NotificationItem, NotificationListResponse } from '../../../../types/notification';
import { useNotificationState } from '../../../../hooks/useNotificationState';
import { invalidateNotificationList } from './notificationListCache';
import { NotificationsWidget } from './NotificationsWidget';

jest.mock('../../../../hooks/useNotificationState', () => ({ useNotificationState: jest.fn() }));
jest.mock('./notificationListCache', () => ({ invalidateNotificationList: jest.fn() }));

const mockUseNotificationState = useNotificationState as jest.MockedFunction<typeof useNotificationState>;
const mockInvalidate = invalidateNotificationList as jest.MockedFunction<typeof invalidateNotificationList>;

function storeState(unreadCount: number | null, revision: number): ReturnType<typeof useNotificationState> {
  return {
    unreadCount,
    revision,
    pending: false,
    refreshUnread: jest.fn(async () => undefined),
    markRead: jest.fn(),
    markAllRead: jest.fn(),
  };
}

function note(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 1,
    event_key: 'ncr.created',
    severity: 'warning',
    title: 'NCR-0042 opened',
    body: null,
    link: '/quality?tab=ncr&ncr=42',
    related_type: 'ncr',
    related_id: 42,
    is_read: false,
    read_at: null,
    created_at: '2026-11-11T14:20:00Z',
    ...overrides,
  };
}

function page(items: NotificationItem[], total = items.length): NotificationListResponse {
  return {
    items,
    pagination: {
      page: 1,
      page_size: 5,
      total_count: total,
      total_pages: Math.ceil(total / 5),
      has_next: total > 5,
      has_previous: false,
    },
  };
}

/** Re-renders the widget in place (same providers, no remount) after the store mock changes. */
let rerenderWidget: () => void = () => undefined;
function Harness() {
  const [, setTick] = React.useState(0);
  rerenderWidget = () => setTick(tick => tick + 1);
  return <NotificationsWidget />;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUseNotificationState.mockReturnValue(storeState(3, 1));
});

describe('NotificationsWidget', () => {
  it('shows the store count and the newest unread rows with deep links and Central times', () => {
    renderWithDashboardContext(<NotificationsWidget />, {
      sources: {
        notifications: readySource(
          page([note(), note({ id: 2, severity: 'critical', title: 'Machine down: Haas VF-2', link: null })], 9)
        ),
      },
    });
    // The header count is the shared store's, not the list page's total.
    expect(screen.getByText('3 unread')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'NCR-0042 opened' })).toHaveAttribute('href', '/quality?tab=ncr&ncr=42');
    expect(screen.getByText('Machine down: Haas VF-2')).toBeInTheDocument();
    expect(screen.getByText('Warning')).toBeInTheDocument();
    expect(screen.getByText('Critical')).toBeInTheDocument();
    // 14:20 UTC → 8:20 AM Central.
    expect(screen.getAllByText('Nov 11, 8:20 AM')).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'Open all unread notifications' })).toHaveAttribute(
      'href',
      '/notifications?show=unread'
    );
  });

  it('falls back to the list total before the store knows the count', () => {
    mockUseNotificationState.mockReturnValue(storeState(null, 0));
    renderWithDashboardContext(<NotificationsWidget />, { sources: { notifications: readySource(page([note()], 9)) } });
    expect(screen.getByText('9 unread')).toBeInTheDocument();
  });

  it('renders the empty state when nothing is unread', () => {
    mockUseNotificationState.mockReturnValue(storeState(0, 1));
    renderWithDashboardContext(<NotificationsWidget />, { sources: { notifications: readySource(page([])) } });
    expect(screen.getByText("You're all caught up")).toBeInTheDocument();
  });

  it('treats a stale list as caught up once the store says nothing is unread', () => {
    mockUseNotificationState.mockReturnValue(storeState(0, 4));
    renderWithDashboardContext(<NotificationsWidget />, { sources: { notifications: readySource(page([note()])) } });
    expect(screen.getByText("You're all caught up")).toBeInTheDocument();
  });

  it('invalidates the list and asks for a non-forced refresh when the unread count changes', () => {
    const view = renderWithDashboardContext(<Harness />, {
      sources: { notifications: readySource(page([note()])) },
    });
    expect(mockInvalidate).not.toHaveBeenCalled();
    expect(view.refresh).not.toHaveBeenCalled();

    // A re-render with the same count is not a change.
    act(() => rerenderWidget());
    expect(mockInvalidate).not.toHaveBeenCalled();

    mockUseNotificationState.mockReturnValue(storeState(4, 2));
    act(() => rerenderWidget());
    expect(mockInvalidate).toHaveBeenCalledTimes(1);
    expect(view.refresh).toHaveBeenCalledTimes(1);
    expect(view.refresh).toHaveBeenCalledWith(false);
    expect(screen.getByText('4 unread')).toBeInTheDocument();
  });

  it('does not refetch for the first count after mount or a session reset', () => {
    mockUseNotificationState.mockReturnValue(storeState(null, 0));
    const view = renderWithDashboardContext(<Harness />, {
      sources: { notifications: readySource(page([note()])) },
    });
    mockUseNotificationState.mockReturnValue(storeState(3, 1));
    act(() => rerenderWidget());
    expect(mockInvalidate).not.toHaveBeenCalled();
    expect(view.refresh).not.toHaveBeenCalled();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<NotificationsWidget />, {
      sources: { notifications: readySource(page([note()]), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('NCR-0042 opened')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<NotificationsWidget />, {
      sources: { notifications: readySource(null, { error: 'Could not refresh' }) },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Notifications are unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<NotificationsWidget />, {
      sources: { notifications: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByText(/\d+ unread/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders titles as text when their deep link cannot be opened', () => {
    renderWithDashboardContext(<NotificationsWidget />, {
      sources: { notifications: readySource(page([note()])) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('NCR-0042 opened')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

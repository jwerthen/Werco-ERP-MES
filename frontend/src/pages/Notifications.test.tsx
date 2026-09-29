/**
 * Notifications page — the full in-app inbox (bell "View all" target).
 *
 * Covers: the server-paged list loads and renders rows; the empty state shows
 * when there are none; changing the severity filter re-queries the server with
 * the filter (and resets to page 1); "Mark all read" calls the API and reloads.
 * services/api is mocked at the module boundary.
 */

import React from 'react';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Notifications from './Notifications';
import NotificationBell from '../components/NotificationBell';
import api from '../services/api';
import { NotificationItem, PaginationMeta } from '../types/notification';

jest.mock('../components/BackgroundEmailActivity', () => ({ __esModule: true, default: () => null }));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUnreadCount: jest.fn(),
    getNotificationCatalog: jest.fn(),
    getNotifications: jest.fn(),
    markNotificationRead: jest.fn(),
    markAllNotificationsRead: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const meta = (over: Partial<PaginationMeta> = {}): PaginationMeta => ({
  page: 1,
  page_size: 25,
  total_count: 1,
  total_pages: 1,
  has_next: false,
  has_previous: false,
  ...over,
});

const makeItem = (over: Partial<NotificationItem>): NotificationItem => ({
  id: 1,
  event_key: 'wo.blocker_created',
  severity: 'critical',
  title: 'Work order on hold',
  body: 'WO-1042',
  link: '/work-orders/1042',
  related_type: 'work_order',
  related_id: 1042,
  is_read: false,
  read_at: null,
  created_at: '2026-07-24T12:00:00Z',
  ...over,
});

const renderPage = (initialEntry = '/notifications') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Notifications />
    </MemoryRouter>
  );

describe('Notifications page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getUnreadCount.mockResolvedValue(1);
    mockApi.getNotificationCatalog.mockResolvedValue([
      {
        event_key: 'wo.blocker_created',
        label: 'WO blocker created',
        description: '',
        category: 'Production',
        severity: 'critical',
        default_channels: ['in_app'],
        mandatory_channel: 'in_app',
        sms_eligible: true,
      },
    ]);
  });

  it('loads and renders the server-paged notifications', async () => {
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 3, title: 'Receipt recorded' })],
      pagination: meta(),
    });

    renderPage();

    expect(await screen.findByText('Receipt recorded')).toBeInTheDocument();
    expect(mockApi.getNotifications).toHaveBeenCalledWith({ page: 1, pageSize: 25, unread: true });
    expect(screen.getByLabelText('Show')).toHaveValue('unread');
  });

  it('renders the empty state when there are no notifications', async () => {
    mockApi.getNotifications.mockResolvedValue({
      items: [],
      pagination: meta({ total_count: 0 }),
    });

    renderPage();

    expect(await screen.findByText('No notifications')).toBeInTheDocument();
  });

  it('re-queries with the severity filter and resets to page 1', async () => {
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 4, title: 'Something happened' })],
      pagination: meta(),
    });

    renderPage();
    await screen.findByText('Something happened');

    fireEvent.change(screen.getByLabelText('Severity'), { target: { value: 'critical' } });

    await waitFor(() =>
      expect(mockApi.getNotifications).toHaveBeenLastCalledWith(
        expect.objectContaining({ page: 1, severity: 'critical' })
      )
    );
  });

  it('marks all read through the API and reloads', async () => {
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 6, title: 'Unread thing', is_read: false })],
      pagination: meta(),
    });
    mockApi.markAllNotificationsRead.mockResolvedValue({ updated: 1 });

    renderPage();
    await screen.findByText('Unread thing');

    fireEvent.click(screen.getByRole('button', { name: /Mark all read/i }));

    await waitFor(() => expect(mockApi.markAllNotificationsRead).toHaveBeenCalled());
  });

  it.each([
    ['/notifications?show=all', 'all', undefined],
    ['/notifications?unread=false', 'read', false],
    ['/notifications?show=unread', 'unread', true],
  ])('respects explicit notification filters in %s', async (url, show, unread) => {
    mockApi.getNotifications.mockResolvedValue({ items: [makeItem({})], pagination: meta() });
    renderPage(url as string);
    await screen.findByText('Work order on hold');
    expect(screen.getByLabelText('Show')).toHaveValue(show);
    expect(mockApi.getNotifications).toHaveBeenLastCalledWith(unread === undefined ? { page: 1, pageSize: 25 } : { page: 1, pageSize: 25, unread });
  });

  it('shows all on a fresh visit with no unread notifications and leaves email activity collapsed', async () => {
    mockApi.getUnreadCount.mockResolvedValue(0);
    mockApi.getNotifications.mockResolvedValue({ items: [makeItem({ is_read: true })], pagination: meta() });
    renderPage();
    await screen.findByText('Work order on hold');
    await waitFor(() => expect(screen.getByLabelText('Show')).toHaveValue('all'));
    expect(screen.getByText('Background email activity').closest('details')).not.toHaveAttribute('open');
  });

  it('reaches the remaining unread notifications through visible pagination', async () => {
    mockApi.getUnreadCount.mockResolvedValue(42);
    mockApi.getNotifications.mockImplementation(async params => ({
      items: Array.from({ length: params?.page === 2 ? 17 : 25 }, (_, i) => makeItem({ id: i + (params?.page === 2 ? 26 : 1), title: `Unread notice ${i + (params?.page === 2 ? 26 : 1)}` })),
      pagination: meta({ page: params?.page ?? 1, total_count: 42, total_pages: 2, has_next: params?.page !== 2, has_previous: params?.page === 2 }),
    }));
    renderPage();
    expect(await screen.findByText(/17 remaining after this page/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    expect(await screen.findByText('Unread notice 42')).toBeInTheDocument();
    expect(screen.getByText(/0 remaining after this page/)).toBeInTheDocument();
    expect(mockApi.getNotifications).toHaveBeenLastCalledWith({ page: 2, pageSize: 25, unread: true });
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled();
  });

  it('shows the shared unread count rather than total notifications and clears the bell when the inbox marks the last unread row', async () => {
    let unread = makeItem({ id: 8, title: 'Last unread notification' });
    const read = makeItem({ id: 9, title: 'Already read notification', is_read: true });
    mockApi.getNotifications.mockImplementation(async () => ({
      items: [unread, read],
      pagination: meta({ total_count: 42, total_pages: 2, has_next: true }),
    }));
    mockApi.markNotificationRead.mockImplementation(async () => {
      unread = { ...unread, is_read: true };
      return unread;
    });
    render(<MemoryRouter><NotificationBell /><Notifications /></MemoryRouter>);

    expect(await screen.findByRole('button', { name: 'Notifications, 1 unread' })).toBeInTheDocument();
    await screen.findByText('Last unread notification');
    expect(screen.getByText('42')).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('table')).getByRole('button', { name: 'Mark read' }));

    await waitFor(() => expect(mockApi.markNotificationRead).toHaveBeenCalledWith(8));
    const bell = await screen.findByRole('button', { name: 'Notifications' });
    expect(within(bell).queryByText('1')).not.toBeInTheDocument();
    expect(within(bell).queryByText('0')).not.toBeInTheDocument();
    await waitFor(() => expect(within(screen.getByRole('table')).queryByRole('button', { name: 'Mark read' })).not.toBeInTheDocument());
    expect(mockApi.getUnreadCount).toHaveBeenCalledTimes(1);
    expect(mockApi.getNotificationCatalog).toHaveBeenCalledTimes(1);
  });
});

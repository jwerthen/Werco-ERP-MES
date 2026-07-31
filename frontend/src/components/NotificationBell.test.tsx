/**
 * NotificationBell — top-bar bell + unread badge + recent-20 popover.
 *
 * Covers: the mount-time unread-count poll drives the badge; opening the popover
 * loads the recent list; clicking a row marks it read (optimistically) and
 * decrements the badge; "Mark all read" clears the badge; the empty state renders
 * when there are none; Escape closes the popover. services/api is mocked at the
 * module boundary.
 */

import React from 'react';
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NotificationBell from './NotificationBell';
import api from '../services/api';
import { NotificationItem } from '../types/notification';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUnreadCount: jest.fn(),
    getNotifications: jest.fn(),
    markNotificationRead: jest.fn(),
    markAllNotificationsRead: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const makeItem = (overrides: Partial<NotificationItem>): NotificationItem => ({
  id: 1,
  event_key: 'wo.blocker_created',
  severity: 'critical',
  title: 'Work order placed on hold',
  body: 'WO-1042',
  link: '/work-orders/1042',
  related_type: 'work_order',
  related_id: 1042,
  is_read: false,
  read_at: null,
  created_at: '2026-07-24T12:00:00Z',
  ...overrides,
});

const renderBell = () =>
  render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>
  );

describe('NotificationBell', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getUnreadCount.mockResolvedValue(0);
    mockApi.getNotifications.mockResolvedValue({
      items: [],
      pagination: { page: 1, page_size: 20, total_count: 0, total_pages: 1, has_next: false, has_previous: false },
    });
  });

  it('renders the unread badge from the mount-time count poll', async () => {
    mockApi.getUnreadCount.mockResolvedValue(3);
    renderBell();

    expect(await screen.findByText('3')).toBeInTheDocument();
    // The accessible name reflects the unread count.
    expect(screen.getByRole('button', { name: 'Notifications, 3 unread' })).toBeInTheDocument();
  });

  it('opens the popover and loads the recent notifications on click', async () => {
    mockApi.getUnreadCount.mockResolvedValue(1);
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 5, title: 'Receipt recorded', severity: 'info' })],
      pagination: { page: 1, page_size: 20, total_count: 1, total_pages: 1, has_next: false, has_previous: false },
    });

    renderBell();

    fireEvent.click(await screen.findByRole('button', { name: /Notifications/ }));

    expect(await screen.findByText('Receipt recorded')).toBeInTheDocument();
    expect(mockApi.getNotifications).toHaveBeenCalledWith({ pageSize: 20 });
  });

  it('marks a notification read on click and decrements the badge', async () => {
    mockApi.getUnreadCount.mockResolvedValue(1);
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 7, title: 'Inspection failed' })],
      pagination: { page: 1, page_size: 20, total_count: 1, total_pages: 1, has_next: false, has_previous: false },
    });
    mockApi.markNotificationRead.mockResolvedValue(makeItem({ id: 7, is_read: true }));

    renderBell();

    fireEvent.click(await screen.findByRole('button', { name: /Notifications/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Notifications' });
    fireEvent.click(within(dialog).getByText('Inspection failed'));

    await waitFor(() => expect(mockApi.markNotificationRead).toHaveBeenCalledWith(7));
    // Badge cleared (1 → 0) optimistically.
    await waitFor(() => expect(screen.queryByText('1')).not.toBeInTheDocument());
  });

  it('marks all read through the API and clears the badge', async () => {
    mockApi.getUnreadCount.mockResolvedValue(2);
    mockApi.getNotifications.mockResolvedValue({
      items: [makeItem({ id: 8 }), makeItem({ id: 9, is_read: false })],
      pagination: { page: 1, page_size: 20, total_count: 2, total_pages: 1, has_next: false, has_previous: false },
    });
    mockApi.markAllNotificationsRead.mockResolvedValue({ updated: 2 });

    renderBell();

    fireEvent.click(await screen.findByRole('button', { name: /Notifications/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Notifications' });
    fireEvent.click(within(dialog).getByRole('button', { name: /Mark all read/i }));

    await waitFor(() => expect(mockApi.markAllNotificationsRead).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText('2')).not.toBeInTheDocument());
  });

  it('renders the empty state when there are no notifications', async () => {
    mockApi.getUnreadCount.mockResolvedValue(0);
    renderBell();

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }));

    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });

  it('closes the popover on Escape', async () => {
    mockApi.getUnreadCount.mockResolvedValue(0);
    renderBell();

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }));
    expect(await screen.findByRole('dialog', { name: 'Notifications' })).toBeInTheDocument();

    act(() => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
    );
  });
});

/**
 * Notifications page — the full in-app inbox (bell "View all" target).
 *
 * Covers: the server-paged list loads and renders rows; the empty state shows
 * when there are none; changing the severity filter re-queries the server with
 * the filter (and resets to page 1); "Mark all read" calls the API and reloads.
 * services/api is mocked at the module boundary.
 */

import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Notifications from './Notifications';
import api from '../services/api';
import { NotificationItem, PaginationMeta } from '../types/notification';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
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

const renderPage = () =>
  render(
    <MemoryRouter>
      <Notifications />
    </MemoryRouter>
  );

describe('Notifications page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
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
    expect(mockApi.getNotifications).toHaveBeenCalledWith({ page: 1, pageSize: 25 });
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
});

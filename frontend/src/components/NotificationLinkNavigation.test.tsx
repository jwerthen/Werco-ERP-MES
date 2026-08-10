/**
 * The two notification surfaces must NAVIGATE to `notification.link` — and only
 * to routes that resolve.
 *
 * This is the consumer end of the 2026-08-07 deep-link fix. The backend now
 * emits only shapes declared in `app/services/notification_links.py`, but that
 * is worth nothing if the UI drops the value or sends it somewhere else. Both
 * surfaces are covered here in one file because they are the same contract:
 *
 * - `NotificationBell` renders a row with a link as a `<Link to={item.link}>`
 *   and a row WITHOUT one as a plain `<button>` (a non-navigating row is the
 *   correct rendering when there is no honest destination — the third rule in
 *   notification_links.py);
 * - `Notifications` (the full inbox) navigates on row click.
 *
 * The end-to-end assertion is the important one: feed a row the exact link
 * strings the backend now emits, click it, and require the resulting location
 * to be a REAL route rather than the catch-all 404 — the failure the user
 * actually reported.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import NotificationBell from './NotificationBell';
import NotificationsPage from '../pages/Notifications';
import api from '../services/api';
import { ToastProvider } from './ui/Toast';
import { NotificationItem } from '../types/notification';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUnreadCount: jest.fn(),
    getNotifications: jest.fn(),
    markNotificationRead: jest.fn(),
    markAllNotificationsRead: jest.fn(),
    getNotificationCatalog: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const makeItem = (overrides: Partial<NotificationItem>): NotificationItem => ({
  id: 1,
  event_key: 'receipt.created',
  severity: 'info',
  title: 'Material received: RCV-20260807-008',
  body: 'RCV-20260807-008',
  link: '/purchasing?po=12',
  related_type: 'po_receipt',
  related_id: 8,
  is_read: false,
  read_at: null,
  created_at: '2026-08-07T12:00:00Z',
  ...overrides,
});

const page = (items: NotificationItem[]) => ({
  items,
  pagination: { page: 1, page_size: 20, total_count: items.length, total_pages: 1, has_next: false, has_previous: false },
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

/**
 * Every route family the backend's ALL_LINK_TEMPLATES can land on, plus a
 * catch-all standing in for App.tsx's `NotFound`. A link that reaches the
 * catch-all is the exact production bug.
 */
const REAL_ROUTES = [
  '/work-orders/:id',
  '/parts/:id',
  '/purchasing',
  '/quotes',
  '/quality',
  '/calibration',
  '/downtime',
  '/inventory',
  '/mrp',
  '/scheduling',
  '/visitor-log',
];

const renderInRouter = (ui: React.ReactNode) =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <ToastProvider>
        <LocationProbe />
        {ui}
        <Routes>
          {REAL_ROUTES.map(path => (
            <Route key={path} path={path} element={<div data-testid="real-route">ok</div>} />
          ))}
          <Route path="/" element={<div />} />
          <Route path="*" element={<div data-testid="not-found">Page not found</div>} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );

const landedAt = () => screen.getByTestId('location').textContent;

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getUnreadCount.mockResolvedValue(1);
  mockApi.markNotificationRead.mockResolvedValue({} as any);
  mockApi.getNotificationCatalog.mockResolvedValue([] as any);
});

// Exactly the values ALL_LINK_TEMPLATES produces, with ids filled in. Keep this
// list in step with notification_links.py -- the backend guard test proves each
// one resolves against App.tsx; this file proves the UI actually GOES there.
const EMITTABLE_LINKS = [
  '/work-orders/1042',
  '/parts/55',
  '/purchasing?po=12',
  '/quotes?id=9',
  '/quality?tab=fai&fai=8',
  '/quality?tab=ncr',
  '/quality?tab=car',
  '/calibration',
  '/downtime',
  '/inventory',
  '/mrp',
  '/scheduling',
  '/visitor-log',
];

describe('NotificationBell', () => {
  const openPopover = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /Notifications/ }));
    await waitFor(() => expect(mockApi.getNotifications).toHaveBeenCalled());
  };

  test.each(EMITTABLE_LINKS)('a row linking to %s navigates there, not to the 404', async link => {
    mockApi.getNotifications.mockResolvedValue(page([makeItem({ link })]) as any);
    renderInRouter(<NotificationBell />);
    await openPopover();

    const row = await screen.findByRole('link', { name: /Material received/ });
    expect(row).toHaveAttribute('href', link);

    fireEvent.click(row);

    await waitFor(() => expect(landedAt()).toBe(link));
    expect(screen.getByTestId('real-route')).toBeInTheDocument();
    expect(screen.queryByTestId('not-found')).not.toBeInTheDocument();
  });

  test('clicking a linked row also marks it read', async () => {
    mockApi.getNotifications.mockResolvedValue(page([makeItem({ id: 5 })]) as any);
    renderInRouter(<NotificationBell />);
    await openPopover();

    fireEvent.click(await screen.findByRole('link', { name: /Material received/ }));
    await waitFor(() => expect(mockApi.markNotificationRead).toHaveBeenCalledWith(5));
  });

  test('a row with link=null renders as a non-navigating button, not a dead link', async () => {
    // The correct rendering when the builder had no honest destination. It must
    // NOT become an <a> to nowhere.
    mockApi.getNotifications.mockResolvedValue(page([makeItem({ id: 6, link: null })]) as any);
    renderInRouter(<NotificationBell />);
    await openPopover();

    expect(screen.queryByRole('link', { name: /Material received/ })).not.toBeInTheDocument();
    const row = await screen.findByRole('button', { name: /Material received/ });

    fireEvent.click(row);
    await waitFor(() => expect(mockApi.markNotificationRead).toHaveBeenCalledWith(6));
    // Still on the page it started on.
    expect(landedAt()).toBe('/');
  });

  test('the legacy shape still in production rows resolves through the redirect, not the 404', async () => {
    // Rows written BEFORE the fix carry /purchasing/12. App.tsx's legacy
    // redirect is what saves them; here we only assert the bell hands the value
    // to the router verbatim rather than mangling or dropping it.
    mockApi.getNotifications.mockResolvedValue(page([makeItem({ link: '/purchasing/12' })]) as any);
    renderInRouter(<NotificationBell />);
    await openPopover();

    expect(await screen.findByRole('link', { name: /Material received/ })).toHaveAttribute('href', '/purchasing/12');
  });
});

describe('Notifications inbox page', () => {
  /**
   * The inbox row's title cell, scoped to the desktop table — DataTable also
   * renders responsive mobile cards into the same jsdom tree, so the title
   * appears twice.
   */
  const inboxRow = async (): Promise<HTMLElement> => {
    const table = await screen.findByTestId('data-table');
    return within(table).findByText('Material received: RCV-20260807-008');
  };

  test.each(['/purchasing?po=12', '/quality?tab=ncr', '/calibration', '/work-orders/1042'])(
    'a row click navigates to %s',
    async link => {
      mockApi.getNotifications.mockResolvedValue(page([makeItem({ link })]) as any);
      renderInRouter(<NotificationsPage />);

      // Wait for the ROW, not just the table shell — DataTable mounts its
      // <table> before the async load resolves.
      fireEvent.click(await inboxRow());

      await waitFor(() => expect(landedAt()).toBe(link));
      expect(screen.queryByTestId('not-found')).not.toBeInTheDocument();
    },
  );

  test('a row with no link marks read but does not navigate', async () => {
    mockApi.getNotifications.mockResolvedValue(page([makeItem({ id: 9, link: null })]) as any);
    renderInRouter(<NotificationsPage />);

    fireEvent.click(await inboxRow());

    await waitFor(() => expect(mockApi.markNotificationRead).toHaveBeenCalledWith(9));
    expect(landedAt()).toBe('/');
  });
});

import { test, expect, Locator } from '@playwright/test';

const user = {
  id: 9003, company_id: 9001, email: 'notifications-layout@example.test', employee_id: 'NOTIFICATIONS',
  first_name: 'Notification', last_name: 'Check', role: 'admin', is_active: true,
};
const notification = {
  id: 1, event_key: 'wo.blocker_created', severity: 'critical', is_read: false, read_at: null,
  title: 'Work order WO-20260921-00131 needs resolution before dispatch',
  body: 'WO-20260921-00131 | In progress | Qty complete: 1.0 | Qty required: 16.0 | FINISHED-GOODS-AISLE-12-LOCATION-001 has -16 pieces on hand.',
  link: '/work-orders/131', related_type: 'work_order', related_id: 131, created_at: '2026-09-28T12:00:00Z',
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(profile => {
    sessionStorage.setItem('token', 'synthetic-notifications-layout-session');
    sessionStorage.setItem('user', JSON.stringify(profile));
    localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
  }, user);
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path.endsWith('/users/me')) body = user;
    else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'Layout fixture' };
    else if (path.endsWith('/notifications/unread-count')) body = { count: 1 };
    else if (path.endsWith('/notifications')) body = {
      items: [notification],
      pagination: { page: 1, page_size: 25, total_count: 42, total_pages: 2, has_next: true, has_previous: false },
    };
    else if (path.endsWith('/pending-approval-summary')) body = { count: 0 };
    else if (path.includes('/runtime-metrics/')) body = { enabled: false };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
});

async function expectFullText(locator: Locator) {
  await expect(locator).toBeVisible();
  const layout = await locator.evaluate(node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const box = node.getBoundingClientRect();
    const textRects = Array.from(range.getClientRects());
    return {
      fits: textRects.every(rect => rect.left >= box.left - 1 && rect.right <= box.right + 1
        && rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1),
      noScroll: node.scrollWidth <= node.clientWidth + 1 && node.scrollHeight <= node.clientHeight + 1,
      textOverflow: getComputedStyle(node).textOverflow,
      lineClamp: getComputedStyle(node).webkitLineClamp,
    };
  });
  expect(layout.fits).toBe(true);
  expect(layout.noScroll).toBe(true);
  expect(layout.textOverflow).not.toBe('ellipsis');
  expect(layout.lineClamp).toBe('none');
}

for (const width of [1280, 1440]) {
  test(`notification identifiers and quantities are fully readable in inbox and bell at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/notifications');
    const table = page.getByTestId('data-table');
    await expectFullText(table.getByText(notification.title, { exact: true }));
    await expectFullText(table.getByText(notification.body, { exact: true }));
    expect((await table.getByText(notification.title, { exact: true }).boundingBox())!.y).toBeLessThan(900);
    await expect(page.getByRole('region', { name: 'Background email activity' })).not.toBeVisible();
    await page.getByRole('button', { name: 'Notifications, 1 unread' }).click();
    const popover = page.getByRole('dialog', { name: 'Notifications' });
    await expectFullText(popover.getByText(notification.title, { exact: true }));
    await expectFullText(popover.getByText(notification.body, { exact: true }));
    await page.locator('details > summary').filter({ hasText: 'Background email activity' }).click();
    await expect(page.getByRole('region', { name: 'Background email activity' })).toBeVisible();
  });
}

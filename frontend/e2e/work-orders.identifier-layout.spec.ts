import { test, expect } from '@playwright/test';

// Layout regression uses synthetic read-only API responses, so it needs no
// seeded account and cannot modify an inventory or production record.
const user = {
  id: 9001, company_id: 9001, email: 'layout@example.test', employee_id: 'LAYOUT',
  first_name: 'Layout', last_name: 'Check', role: 'admin', is_active: true,
};
const workOrderNumber = 'WO-20260921-00131';
const order = {
  id: 131, work_order_number: workOrderNumber, part_id: 10, work_order_type: 'production',
  part_number: 'PN-20260921-131', part_name: 'Bracket assembly', part_type: 'manufactured',
  status: 'in_progress', priority: 2, quantity_ordered: 131, quantity_complete: 130,
  customer_name: 'Example Aerospace Manufacturing', due_date: '2026-09-21',
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(profile => {
    sessionStorage.setItem('token', 'synthetic-layout-session');
    sessionStorage.setItem('user', JSON.stringify(profile));
    localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
  }, user);
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path.endsWith('/users/me')) body = user;
    else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'Layout fixture' };
    else if (path.endsWith('/work-orders/browse')) body = {
      items: [order], total: 1, skip: 0, limit: 50, has_next: false,
      customers: [order.customer_name], customers_truncated: false, group_totals: {},
      stats: { overdue: 1, due_today: 0, in_progress: 1 },
    };
    else if (path.includes('/user-workspaces/team/')) body = { items: [], can_manage: true };
    else if (path.endsWith('/unread-count') || path.endsWith('/pending-approval-summary')) body = { count: 0 };
    else if (path.includes('/runtime-metrics/')) body = { enabled: false };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
});

for (const width of [1280, 1440]) {
  test(`full work order identifier fits its cell at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/work-orders');
    const identifier = page.locator('table').getByRole('link', { name: workOrderNumber, exact: true });
    await expect(identifier).toBeVisible();
    const firstRow = await identifier.locator('xpath=ancestor::tr').boundingBox();
    expect(firstRow).not.toBeNull();
    expect(firstRow!.y + firstRow!.height).toBeLessThanOrEqual(900);
    await identifier.scrollIntoViewIfNeeded();
    const layout = await identifier.evaluate(link => {
      const range = document.createRange();
      range.selectNodeContents(link);
      const textRects = Array.from(range.getClientRects());
      const cell = link.closest('td')!.getBoundingClientRect();
      const container = link.closest('.table-container')!.getBoundingClientRect();
      return {
        textFits: textRects.every(rect => rect.left >= cell.left && rect.right <= cell.right + 1),
        cellVisible: cell.left >= container.left && cell.right <= container.right + 1,
        textOverflow: getComputedStyle(link).textOverflow,
        pageOverflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    expect(layout.textFits).toBe(true);
    expect(layout.cellVisible).toBe(true);
    expect(layout.textOverflow).not.toBe('ellipsis');
    expect(layout.pageOverflow).toBe(false);
    await expect(identifier).toHaveText(workOrderNumber);
  });
}

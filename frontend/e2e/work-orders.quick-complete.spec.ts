import { test, expect } from '@playwright/test';

const user = {
  id: 9001, company_id: 9001, email: 'completion@example.test', employee_id: 'COMPLETE',
  first_name: 'Completion', last_name: 'Check', role: 'manager', is_active: true,
};
const order = {
  id: 131, work_order_number: 'WO-20260921-00131', part_id: 10, work_order_type: 'production',
  part_number: 'PN-20260921-131', part_name: 'Bracket assembly', part_type: 'manufactured',
  status: 'in_progress', priority: 2, quantity_ordered: 10, quantity_complete: 8,
  operation_count: 4, operations_complete: 3, operation_progress_percent: 75,
  customer_name: 'Example Aerospace', due_date: '2026-09-21',
};

for (const width of [1280, 390]) {
  test(`quick completion stays on the list at ${width}px`, async ({ page }, testInfo) => {
    let completed = false;
    let completionCalls = 0;
    let detailCalls = 0;
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(profile => {
      sessionStorage.setItem('token', 'synthetic-completion-session');
      sessionStorage.setItem('user', JSON.stringify(profile));
      localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
    }, user);
    await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
    // Every API response is synthetic, including the completion write.
    await page.route('**/api/v1/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      let body: unknown = [];
      if (path.endsWith('/users/me')) body = user;
      else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'Completion fixture' };
      else if (path.endsWith('/work-orders/131/complete')) {
        expect(route.request().method()).toBe('POST');
        expect(url.searchParams.get('quantity_complete')).toBe('10');
        expect(url.searchParams.has('quantity_scrapped')).toBe(false);
        completionCalls += 1;
        completed = true;
        body = { message: 'Work order completed', status: 'complete' };
      } else if (path.endsWith('/work-orders/131')) detailCalls += 1;
      else if (path.endsWith('/work-orders/browse')) body = {
        items: completed ? [] : [order], total: completed ? 0 : 1, skip: 0, limit: 50, has_next: false,
        customers: [order.customer_name], customers_truncated: false, group_totals: {},
        stats: { overdue: completed ? 0 : 1, due_today: 0, in_progress: completed ? 0 : 1 },
      };
      else if (path.includes('/user-workspaces/team/')) body = { items: [], can_manage: true };
      else if (path.endsWith('/unread-count') || path.endsWith('/pending-approval-summary')) body = { count: 0 };
      else if (path.includes('/runtime-metrics/')) body = { enabled: false };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });

    await page.goto(width === 390 ? '/work-orders?group=customer' : '/work-orders');
    const quickComplete = page.getByRole('button', { name: `Quick complete ${order.work_order_number}` });
    await expect(quickComplete).toBeVisible();
    await quickComplete.scrollIntoViewIfNeeded();
    const bounds = await quickComplete.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`quick-complete-${width}.png`), animations: 'disabled' });
    await quickComplete.click();
    const dialog = page.getByRole('dialog', { name: `Complete work order ${order.work_order_number}` });
    await expect(dialog.getByRole('spinbutton', { name: 'Quantity completed' })).toHaveValue('10');
    await expect(dialog.getByText('Completes all remaining operations.', { exact: false })).toBeVisible();
    expect(detailCalls).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`quick-complete-dialog-${width}.png`), animations: 'disabled' });
    await dialog.getByRole('button', { name: 'Complete', exact: true }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByText(`${order.work_order_number} completed`, { exact: true })).toBeVisible();
    await expect(quickComplete).toHaveCount(0);
    expect(completionCalls).toBe(1);
    expect(detailCalls).toBe(0);
    expect(new URL(page.url()).pathname).toBe('/work-orders');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}

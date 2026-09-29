import { test, expect } from '@playwright/test';

// Synthetic, read-only report payloads reproduce the crowded KPI strip without
// depending on a seeded account or changing production records.
const user = {
  id: 9002, company_id: 9001, email: 'reports-layout@example.test', employee_id: 'REPORTS',
  first_name: 'Reports', last_name: 'Check', role: 'admin', is_active: true,
};
const production = {
  period_days: 30, work_orders_by_status: { in_progress: 4, completed: 48 },
  total_completed: 48, on_time_delivery_count: 4, on_time_delivery_pct: 8.3,
  total_hours_worked: 320, total_produced: 540, total_scrapped: 12, scrap_rate_pct: 2.22,
};
const quality = {
  period_days: 30, total_ncrs: 3, open_ncrs: 1, ncr_by_status: { open: 1, closed: 2 },
  ncr_by_source: { receiving: 2, in_process: 1 }, receiving_total_qty: 1000,
  receiving_rejected_qty: 8, receiving_reject_rate_pct: 0.8,
};
const titles = ['On-Time Delivery', 'Hours Worked', 'Scrap Rate', 'Inventory Value', 'Total NCRs', 'Qty Received', 'Recv Reject Rate'];

test.beforeEach(async ({ page }) => {
  await page.addInitScript(profile => {
    sessionStorage.setItem('token', 'synthetic-reports-layout-session');
    sessionStorage.setItem('user', JSON.stringify(profile));
    localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
  }, user);
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path.endsWith('/users/me')) body = user;
    else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'Layout fixture' };
    else if (path.endsWith('/reports/production-summary')) body = production;
    else if (path.endsWith('/reports/quality-metrics')) body = quality;
    else if (path.endsWith('/reports/inventory-value')) body = { total_value: 125000, total_quantity: 4200, unique_parts: 88 };
    else if (path.endsWith('/reports/work-center-utilization')) body = [{ work_center_id: 1, work_center_code: 'LAS-01', work_center_name: 'Laser cutting', hours_worked: 338.64, available_hours: 240, utilization_pct: 141.1 }];
    else if (path.endsWith('/unread-count') || path.endsWith('/pending-approval-summary')) body = { count: 0 };
    else if (path.includes('/runtime-metrics/')) body = { enabled: false };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
});

for (const width of [1280, 1440]) {
  test(`critical Reports KPI titles and values remain fully visible at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/reports');
    for (const title of titles) {
      const label = page.getByText(title, { exact: true });
      await expect(label).toBeVisible();
      const layout = await label.evaluate(node => {
        const card = node.closest('.card')!;
        const cardRect = card.getBoundingClientRect();
        const textFits = Array.from(card.querySelectorAll('p')).every(text => {
          const range = document.createRange();
          range.selectNodeContents(text);
          const rects = Array.from(range.getClientRects());
          return rects.every(rect => rect.left >= cardRect.left && rect.right <= cardRect.right + 1)
            && text.scrollWidth <= text.clientWidth + 1;
        });
        return {
          textFits,
          textOverflow: getComputedStyle(node).textOverflow,
          cardVisible: cardRect.left >= 0 && cardRect.right <= window.innerWidth && cardRect.bottom <= window.innerHeight,
          pageOverflow: document.documentElement.scrollWidth > window.innerWidth,
        };
      });
      expect(layout.textFits, `${title} label, value and subtitle fit`).toBe(true);
      expect(layout.textOverflow).not.toBe('ellipsis');
      expect(layout.cardVisible).toBe(true);
      expect(layout.pageOverflow).toBe(false);
    }
    await expect(page.getByText('8.3%', { exact: true })).toBeVisible();
    await expect(page.getByText('4 of 48', { exact: true })).toBeVisible();
    await expect(page.getByText('$125,000.00', { exact: true })).toBeVisible();
    const formula = page.getByText('Utilization = logged hours ÷ (period days × 8 hours).', { exact: true });
    await expect(formula).toBeVisible();
    expect(await formula.evaluate(node => node.scrollWidth <= node.clientWidth + 1 && node.scrollHeight <= node.clientHeight + 1)).toBe(true);
    await expect(page.getByText('141.1% · above capacity', { exact: true })).toBeVisible();
  });
}

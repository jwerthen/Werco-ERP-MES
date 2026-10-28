import { expect, test, type Page } from '@playwright/test';

// Synthetic data keeps these navigation/layout checks independent of production.
const user = {
  id: 9012, company_id: 9001, email: 'customer-pos@example.test', employee_id: 'PO-CHECK',
  first_name: 'Customer PO', last_name: 'Check', role: 'manager', is_active: true,
};
const customerName = 'Example Aerospace & Space';
const poNumber = 'PO / 742 & A';
const order = {
  id: 131, work_order_number: 'WO-20261028-00131', version: 1,
  part_id: 10, work_order_type: 'production', parent_work_order_id: null,
  part_number: 'ASSY-101', part_name: 'Aircraft mounting assembly', part_type: 'manufactured',
  status: 'in_progress', priority: 2, quantity_ordered: 10, quantity_complete: 3, quantity_scrapped: 0,
  operation_count: 4, operations_complete: 2, operation_progress_percent: 62.5,
  customer_name: customerName, customer_po: poNumber, po_line_item: '10',
  unit_number: 'UNIT-17', po_date: '2026-10-01', due_date: '2026-10-29',
  must_ship_by: '2026-10-27', is_overdue: true,
  current_operations: [{ id: 51, operation_number: '30', name: 'Weld assembly', status: 'in_progress', work_center_name: 'Weld cell 1' }],
};
const completedOrder = {
  ...order, id: 132, work_order_number: 'WO-20261028-00132', status: 'complete',
  part_number: 'BRACKET-210', part_name: 'Support bracket', po_line_item: '20', unit_number: null,
  quantity_complete: 10, operations_complete: 4, operation_progress_percent: 100,
  current_operations: [], is_overdue: false,
};
const childOrder = {
  ...order, id: 133, work_order_number: 'WO-20261028-00133', status: 'on_hold',
  part_id: null, part_number: null, part_name: null, parent_work_order_id: 131,
  work_order_type: 'laser_cutting', unit_number: null, quantity_ordered: 4, quantity_complete: 1,
  operation_count: 2, operations_complete: 0, operation_progress_percent: 25,
  current_operations: [{ id: 52, operation_number: '10', name: 'Laser nest 1', status: 'on_hold', work_center_name: 'Laser cutting' }],
};
const stats = {
  total_work_orders: 3, active_work_orders: 2, completed_work_orders: 1, cancelled_work_orders: 0,
  in_progress_work_orders: 1, on_hold_work_orders: 1, overdue_work_orders: 2,
  due_today_work_orders: 0, completion_percent: 33.333333, next_due_date: '2026-10-27',
};

async function installFixtures(page: Page) {
  const dashboardRequests: URL[] = [];
  const detailRequests: string[] = [];
  await page.addInitScript(profile => {
    sessionStorage.setItem('token', 'synthetic-customer-po-session');
    sessionStorage.setItem('user', JSON.stringify(profile));
    localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
  }, user);
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let body: unknown = [];
    if (path.endsWith('/users/me')) body = user;
    else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'Customer PO fixture' };
    else if (path.endsWith('/work-orders/customer-pos')) body = {
      items: [
        { customer_name: customerName, customer_po: poNumber, stats },
        { customer_name: 'Another Customer', customer_po: poNumber, stats: { ...stats, total_work_orders: 1, active_work_orders: 0, completed_work_orders: 1, in_progress_work_orders: 0, on_hold_work_orders: 0, overdue_work_orders: 0, completion_percent: 100 } },
      ],
      total: 2, skip: 0, limit: 50, has_next: false, as_of_date: '2026-10-28',
    };
    else if (path.endsWith('/work-orders/customer-po-dashboard')) {
      expect(route.request().method()).toBe('GET');
      dashboardRequests.push(url);
      body = {
        customer_name: customerName, customer_po: poNumber, stats,
        items: [order, childOrder, completedOrder], total: 3, skip: 0, limit: 50,
        has_next: false, as_of_date: '2026-10-28',
      };
    } else if (/\/work-orders\/\d+$/.test(path)) {
      detailRequests.push(path);
      body = { ...order, operations: [], estimated_hours: 4, actual_hours: 2, created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-28T12:00:00Z' };
    } else if (path.endsWith('/work-orders/browse')) body = {
      items: [order], total: 1, skip: 0, limit: 50, has_next: false,
      customers: [customerName], customers_truncated: false, group_totals: {},
      stats: { overdue: 1, due_today: 0, in_progress: 1 },
    };
    else if (path.includes('/user-workspaces/team/')) body = { items: [], can_manage: true };
    else if (path.endsWith('/unread-count') || path.endsWith('/pending-approval-summary')) body = { count: 0 };
    else if (path.includes('/runtime-metrics/')) body = { enabled: false };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  return { dashboardRequests, detailRequests };
}

for (const width of [1440, 390]) {
  test(`customer PO links open the complete production dashboard at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const requests = await installFixtures(page);
    await page.goto('/work-orders');
    await expect(page.getByRole('link', { name: 'Customer POs', exact: true })).toBeVisible();
    const poLink = page.getByRole('link', { name: `Track customer PO ${poNumber}`, exact: true });
    await expect(poLink).toBeVisible();
    await poLink.click();
    await expect(page).toHaveURL(/\/work-orders\/customer-pos\?/);
    await expect(page.getByRole('heading', { name: poNumber, exact: false })).toBeVisible();
    const jobLink = page.getByRole('link', { name: order.work_order_number, exact: true }).first();
    await expect(jobLink).toHaveAttribute('href', '/work-orders/131');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(requests.detailRequests).toHaveLength(0);
    expect(requests.dashboardRequests.length).toBeGreaterThan(0);
    const query = requests.dashboardRequests.at(-1)!.searchParams;
    expect(query.get('customer_name')).toBe(customerName);
    expect(query.get('customer_po')).toBe(poNumber);
    await expect(page.getByRole('link', { name: completedOrder.work_order_number, exact: true }).first()).toBeVisible();
    await expect(page.getByText('Weld cell 1', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('Laser cutting', { exact: false }).first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`customer-po-dashboard-${width}.png`), fullPage: true, animations: 'disabled' });

    await page.goto('/work-orders/customer-pos');
    await expect(page.getByRole('heading', { name: 'Customer Purchase Orders', exact: true })).toBeVisible();
    // Select by the exact encoded pair: identical PO numbers can belong to different customers.
    const matchingPo = page.locator(`a[href="/work-orders/customer-pos?${new URLSearchParams({ customer_name: customerName, customer_po: poNumber }).toString()}"]`).first();
    await expect(matchingPo).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`customer-po-overview-${width}.png`), fullPage: true, animations: 'disabled' });
    await matchingPo.click();
    await expect(page.getByRole('heading', { name: poNumber, exact: false })).toBeVisible();
    const openJob = page.getByRole('link', { name: order.work_order_number, exact: true }).first();
    await expect(openJob).toHaveAttribute('href', '/work-orders/131');
    await openJob.click();
    await expect(page).toHaveURL(/\/work-orders\/131$/);
    await expect.poll(() => requests.detailRequests.length).toBeGreaterThan(0);
  });
}

import { expect, Page, test } from '@playwright/test';
import type consumables from '../src/services/consumables';

// Local synthetic acceptance only. All API calls are intercepted; no production
// checkout, employee or inventory data is read or written.
const manager = {
  id: 7, company_id: 1, email: 'manager@example.test', role: 'manager', employee_id: 'MGR-007',
  first_name: 'Alex', last_name: 'Rivera', is_active: true, is_superuser: false,
};
type Checkout = Awaited<ReturnType<typeof consumables.checkout>>;
type Stock = Awaited<ReturnType<typeof consumables.stock>>;
const receipt = (body: Record<string, unknown> = {}): Checkout => ({
  id: 42, inventory_item_id: 101, part_id: 51, part_number: 'CON-001', part_name: '4.5 inch grinding disc',
  unit_of_measure: 'each', location: 'CRIB-A1', lot_number: 'LOT-01', employee_id: 8,
  employee_name: 'Jordan Smith', department: 'Welding', issued_by: 7,
  issued_by_name: 'Alex Rivera', issued_at: '2026-10-27T19:15:00Z', quantity: 3, notes: 'Welding bay 2',
  request_id: '84678230-89cf-4576-b732-9b94873c5df8', replayed: false, ...body,
});

async function mockWorkspace(page: Page, baseURL: string, role = 'manager', seed = false) {
  const rows: Checkout[] = seed ? [receipt()] : [];
  const user = { ...manager, role };
  const stock: Stock = { items: [
    { inventory_item_id: 101, part_id: 51, part_number: 'CON-001', part_name: '4.5 inch grinding disc',
      unit_of_measure: 'each', location: 'CRIB-A1', lot_number: 'LOT-01', quantity_on_hand: 20,
      quantity_available: 15, part_quantity_on_hand: 20, reorder_point: 10, is_low_stock: false,
      can_checkout: true, unavailable_reason: null },
    { inventory_item_id: 102, part_id: 52, part_number: 'CON-002', part_name: 'Welding gloves',
      unit_of_measure: 'each', location: 'CRIB-A2', lot_number: null, quantity_on_hand: 0,
      quantity_available: 0, part_quantity_on_hand: 0, reorder_point: 5, is_low_stock: true,
      can_checkout: false, unavailable_reason: 'No available stock' },
  ], total: 2 };
  const mutations: Record<string, unknown>[] = [];
  const unexpected: string[] = [];
  const historyQueries: URLSearchParams[] = [];
  const metadata: Awaited<ReturnType<typeof consumables.metadata>> = {
    can_manage: role === 'manager',
    can_export: role === 'manager',
    people: [
      { id: 8, name: 'Jordan Smith', department: 'Welding', is_active: true },
      { id: 9, name: 'Former Employee', department: 'Welding', is_active: false },
    ],
  };
  await page.addInitScript(savedUser => {
    sessionStorage.setItem('token', 'synthetic-local-test-token');
    sessionStorage.setItem('user', JSON.stringify(savedUser));
    localStorage.setItem(`werco-completed-tours:${savedUser.company_id}:${savedUser.id}`, '["getting-started"]');
  }, user);
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (!path.startsWith('/api/v1/')) {
      if (url.origin !== new URL(baseURL).origin) return route.abort();
      return route.continue();
    }
    if (path === '/api/v1/users/me') return route.fulfill({ json: user });
    if (path === '/api/v1/companies/me') return route.fulfill({ json: { id: 1, name: 'Werco Manufacturing', slug: 'werco' } });
    if (path.endsWith('/unread-count') || path.endsWith('/pending-approvals/summary'))
      return route.fulfill({ json: { count: 0 } });
    const prefix = '/api/v1/consumables';
    if (!path.startsWith(prefix)) return route.fulfill({ json: {} });
    if (path === `${prefix}/metadata`) return route.fulfill({ json: metadata });
    if (path === `${prefix}/stock`) {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const items = stock.items.filter(item => !q || `${item.part_name} ${item.part_number}`.toLowerCase().includes(q));
      return route.fulfill({ json: { items, total: items.length } });
    }
    if (path === `${prefix}/checkouts` && request.method() === 'POST') {
      const body = request.postDataJSON();
      mutations.push(body);
      if (!metadata.can_manage) return route.fulfill({ status: 403, json: { detail: 'Issuing role required' } });
      const existing = rows.find(row => row.request_id === body.request_id);
      if (existing) return route.fulfill({ json: { ...existing, replayed: true } });
      const saved = receipt(body);
      rows.push(saved);
      stock.items[0].quantity_on_hand -= saved.quantity;
      stock.items[0].quantity_available -= saved.quantity;
      stock.items[0].part_quantity_on_hand -= saved.quantity;
      return route.fulfill({ status: 201, json: saved });
    }
    if (path === `${prefix}/checkouts` && request.method() === 'GET') {
      historyQueries.push(url.searchParams);
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const items = rows.filter(row =>
        (!q || `${row.part_name} ${row.employee_name} ${row.notes}`.toLowerCase().includes(q)) &&
        (!url.searchParams.get('employee_id') || row.employee_id === Number(url.searchParams.get('employee_id'))));
      return route.fulfill({ json: { items, total: items.length } });
    }
    if (path === `${prefix}/checkouts/export.csv`) {
      return route.fulfill({ contentType: 'text/csv', body: 'Employee,Quantity\nJordan Smith,3\n' });
    }
    unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unexpected feature request' } });
  });
  return { rows, stock, mutations, unexpected, historyQueries };
}

test.use({ timezoneId: 'Asia/Tokyo' });
test.beforeEach(async ({ baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname),
    'Synthetic acceptance runs only against a local frontend.');
});

for (const viewport of [{ name: 'desktop', width: 1440, height: 1100 }, { name: 'mobile', width: 390, height: 844 }]) {
  test(`${viewport.name}: issue supplies to an employee and review stock and Central-time history`, async ({ page, baseURL }, testInfo) => {
    const state = await mockWorkspace(page, baseURL!);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto('/consumables');
    await expect(page.getByRole('heading', { level: 1, name: 'Consumables' })).toBeVisible();
    await page.getByRole('combobox', { name: /^Employee receiving supplies/ }).fill('Jordan');
    await page.getByRole('option', { name: /Jordan Smith/ }).click();
    await page.getByRole('button', { name: 'Select 4.5 inch grinding disc at CRIB-A1' }).click();
    await page.getByRole('spinbutton', { name: /^Quantity/ }).fill('3');
    await page.getByLabel('Notes (optional)', { exact: true }).fill('Welding bay 2');
    await expect(page.getByRole('button', { name: 'Select Welding gloves at CRIB-A2' })).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath(`consumables-${viewport.name}-form.png`), fullPage: true, animations: 'disabled' });
    await page.getByRole('button', { name: 'Record checkout', exact: true }).click();
    await expect.poll(() => state.mutations.length).toBe(1);
    expect(state.mutations[0]).toMatchObject({ inventory_item_id: 101, employee_id: 8, quantity: 3, notes: 'Welding bay 2' });
    expect(state.mutations[0]).not.toHaveProperty('issued_at');
    expect(state.mutations[0]).not.toHaveProperty('issued_by');
    await expect(page.getByText('Jordan Smith', { exact: true }).filter({ visible: true }).first()).toBeVisible();
    await expect(page.getByText('Alex Rivera', { exact: true }).filter({ visible: true }).first()).toBeVisible();
    await expect(page.locator('time:visible').filter({ hasText: /2:15(?::00)? PM CDT/ }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Select 4.5 inch grinding disc at CRIB-A1' })).toContainText('12');
    expect(state.stock.items[0].quantity_on_hand).toBe(17);
    const layout = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    expect(layout.scrollWidth, JSON.stringify(layout)).toBeLessThanOrEqual(layout.width);
    await page.getByRole('heading', { level: 1, name: 'Consumables' }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `/tmp/consumables-${viewport.name}.png`, fullPage: true, animations: 'disabled' });
    await page.getByLabel('Search checkout history', { exact: true }).fill('not found');
    await expect.poll(() => state.historyQueries.at(-1)?.get('q')).toBe('not found');
    await expect(page.getByText('Jordan Smith', { exact: true })).toHaveCount(0);
    await page.getByLabel('Search checkout history', { exact: true }).fill('grinding');
    await expect(page.getByText('Jordan Smith', { exact: true }).filter({ visible: true }).first()).toBeVisible();
    expect(state.unexpected).toEqual([]);
    expect(errors).toEqual([]);
  });
}

test('viewer sees who received supplies and when without issuing controls', async ({ page, baseURL }) => {
  const state = await mockWorkspace(page, baseURL!, 'viewer', true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/consumables');
  await expect(page.getByText('Jordan Smith', { exact: true }).filter({ visible: true }).first()).toBeVisible();
  await expect(page.locator('time:visible').filter({ hasText: /2:15(?::00)? PM CDT/ }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Record checkout', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Retry checkout', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Export CSV', exact: true })).toHaveCount(0);
  await page.screenshot({ path: '/tmp/consumables-viewer-mobile.png', fullPage: true, animations: 'disabled' });
  expect(state.mutations).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

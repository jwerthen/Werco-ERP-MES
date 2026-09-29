import { test, expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const user = { id: 9001, company_id: 9001, email: 'fixture@example.test', employee_id: '9001', first_name: 'UI', last_name: 'Review', role: 'admin', is_active: true };
const part = { id: 10, part_number: 'BRACKET-001', name: 'Mounting bracket', part_type: 'manufactured', revision: 'A', unit_of_measure: 'EA', is_active: true, status: 'active' };
const center = { id: 1, code: 'LAS-01', name: 'Fiber laser', work_center_type: 'laser', hourly_rate: 120, capacity_hours_per_day: 8, efficiency_factor: 1, current_status: 'available', is_active: true };
const routing = { id: 1, part_id: 10, part, revision: 'A', status: 'released', operations: [], total_setup_hours: 0, total_run_hours_per_unit: 0, total_labor_cost: 0, is_active: true };
const bom = { id: 1, part_id: 10, part, revision: 'A', status: 'draft', bom_type: 'standard', items: [] };
const quote = { id: 1, quote_number: 'Q-2026-001', revision: 'A', customer_name: 'Example Aerospace', status: 'draft', quote_date: '2026-09-28', total: 1112.598, subtotal: 1112.598, lines: [] };
const screenshots = resolve(process.cwd(), '../output/phase-c-screenshots');

// Synthetic reads only; every mutation is blocked, including accidental background writes.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(profile => {
    sessionStorage.setItem('token', 'synthetic-ui-review');
    sessionStorage.setItem('user', JSON.stringify(profile));
    localStorage.setItem(`werco-completed-tours:${profile.company_id}:${profile.id}`, '["getting-started"]');
  }, user);
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') return route.fulfill({ status: 405, json: { detail: 'Read-only fixture' } });
    const path = new URL(route.request().url()).pathname.replace(/\/$/, '');
    let body: unknown = [];
    if (path.endsWith('/users/me')) body = user;
    else if (path.endsWith('/companies/me')) body = { id: user.company_id, name: 'UI review fixture' };
    else if (path.endsWith('/admin/settings/work-center-rates')) body = [center];
    else if (path.endsWith('/admin/settings/role-permissions')) body = { roles: [{ value: 'admin', label: 'Administrator' }], role_permissions: { admin: [] }, all_permissions: [], permission_categories: {} };
    else if (path.endsWith('/work-centers/types')) body = { types: ['laser'] };
    else if (path.endsWith('/work-centers')) body = [center];
    else if (path.endsWith('/routing/1')) body = routing;
    else if (path.endsWith('/routing')) body = [{ ...routing, operations: undefined, operation_count: 0 }];
    else if (path.endsWith('/bom/1')) body = bom;
    else if (path.endsWith('/bom')) body = [bom];
    else if (path.endsWith('/parts')) body = [part];
    else if (path.endsWith('/fabrication-quotes/capabilities')) body = { can_write: true };
    else if (path.endsWith('/fabrication-quotes')) body = { items: [], total: 0 };
    else if (path.endsWith('/quotes')) body = [quote, { ...quote, id: 2, quote_number: 'REJECTED-002', status: 'rejected' }];
    else if (path.endsWith('/unread-count') || path.endsWith('/pending-approval-summary')) body = { count: 0 };
    else if (path.includes('/runtime-metrics/')) body = { enabled: false };
    await route.fulfill({ status: 200, json: body });
  });
});

for (const width of [1280, 1440]) {
  test(`admin sections and editing are reachable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/admin/settings');
    await expect(page.getByRole('heading', { level: 1, name: 'Work Center Rates' })).toBeVisible();
    const nav = page.getByRole('navigation', { name: 'Settings sections' });
    expect(await nav.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    const roles = nav.getByRole('button', { name: 'Roles & Permissions' });
    const box = await roles.boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await expect(page.getByRole('button', { name: 'Edit rate for LAS-01' })).toBeVisible();
    if (width === 1440) { mkdirSync(screenshots, { recursive: true }); await page.screenshot({ path: resolve(screenshots, 'admin-settings.png') }); }
    await page.getByRole('button', { name: 'Edit rate for LAS-01' }).click();
    await expect(page.getByLabel('Hourly Rate ($)')).toHaveValue('120');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await roles.click();
    await expect(page.getByRole('heading', { level: 1, name: 'Roles & Permissions' })).toBeVisible();
  });

  test(`quote inputs show readable defaults at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/quote-calculator');
    await expect(page.getByLabel('Quote title')).toBeEnabled();
    await expect(page.getByLabel('Quote currency')).toHaveValue('USD');
    await expect(page.getByLabel('Target gross margin (%)')).toHaveValue('25');
    await expect(page.getByRole('button', { name: 'Calculate', exact: true })).toBeDisabled();
    const title = page.getByLabel('Quote title');
    expect(await title.evaluate((node: HTMLInputElement) => { const canvas = document.createElement('canvas'); const ctx = canvas.getContext('2d')!; const css = getComputedStyle(node); ctx.font = css.font; return ctx.measureText(node.placeholder).width <= node.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight); })).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width === 1440) { mkdirSync(screenshots, { recursive: true }); await page.screenshot({ path: resolve(screenshots, 'fabrication-quotes.png') }); }
  });
}

test('engineering selection and quote membership match the workflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/routing');
  await expect(page.getByRole('alert').filter({ hasText: 'Released with no operations' })).toBeVisible();
  await expect(page).toHaveURL(/id=1/);
  mkdirSync(screenshots, { recursive: true });
  await page.screenshot({ path: resolve(screenshots, 'routing.png') });
  await page.goto('/bom');
  await expect(page.getByRole('button', { name: 'Single Level' })).toBeVisible();
  await expect(page).toHaveURL(/id=1/);
  await page.goto('/quotes');
  await expect(page.getByRole('cell', { name: '$1,112.60' })).toBeVisible();
  await expect(page.getByText('REJECTED-002')).toHaveCount(0);
  await page.screenshot({ path: resolve(screenshots, 'customer-quotes.png') });
  await page.goto('/work-centers');
  await expect(page.getByRole('link', { name: 'View live work in Dispatch' })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Status for LAS-01' }).filter({ visible: true })).toHaveCount(1);
  await page.screenshot({ path: resolve(screenshots, 'work-centers.png') });
});

test('sidebar scrolls without moving the profile footer and settings use standard checkbox chrome', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/work-centers');
  const sidebar = page.getByRole('complementary', { name: 'Workspace navigation' });
  const nav = sidebar.getByRole('navigation', { name: 'Main navigation' });
  const profile = sidebar.getByText('UI Review', { exact: true });
  const before = await profile.boundingBox();
  await nav.evaluate(node => { node.scrollTop = node.scrollHeight; });
  expect(await nav.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  expect((await profile.boundingBox())!.y).toBe(before!.y);
  const purchasing = nav.getByRole('button', { name: 'Purchasing', exact: true });
  await purchasing.scrollIntoViewIfNeeded();
  await expect(purchasing).toBeInViewport();
  expect((await profile.boundingBox())!.y).toBe(before!.y);
  await page.goto('/admin/settings');
  const inactive = page.getByRole('checkbox', { name: 'Show inactive' });
  expect(await inactive.evaluate(node => parseFloat(getComputedStyle(node).borderTopWidth))).toBeLessThanOrEqual(1);
});

import { expect, Page, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import type {
  MaterialPriceHistoryDetail,
  MaterialPricePurchase,
  MaterialPriceSummary,
} from '../src/types/materialPriceHistory';

// This suite is deliberately self-contained: every API response is synthetic,
// and it only opens a local frontend. No live credentials or write endpoints.
const user = {
  id: 9001,
  company_id: 1,
  email: 'buyer@example.test',
  role: 'manager',
  employee_id: 'QA-001',
  first_name: 'Alex',
  last_name: 'Buyer',
  is_active: true,
  is_superuser: false,
};
const dates = ['2026-02-10', '2026-03-17', '2026-04-21', '2026-05-28', '2026-06-30', '2026-08-05', '2026-09-10'];
const prices = [119.5, 123, 121.75, 128, 125.5, 120, 126];
const vendors = [
  { id: 5, name: 'Acme Metals' },
  { id: 6, name: 'Bravo Supply' },
];
const inventory: MaterialPriceSummary[] = [
  {
    part_id: 7,
    part_number: 'AL-5052-090',
    part_name: '5052 aluminum sheet · .090 × 48 × 120',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    currency: null,
    latest_unit_price: 126,
    previous_unit_price: 120,
    price_change: 6,
    price_change_percent: 5,
    last_order_date: '2026-09-10',
    latest_po_id: 107,
    latest_po_number: 'PO-0107',
    latest_vendor_id: 5,
    latest_vendor_name: 'Acme Metals',
    order_count: 7,
    total_quantity: 140,
    total_spend: 17275,
    sparkline: prices.map((unit_price, index) => ({
      purchase_order_id: 101 + index,
      order_date: dates[index],
      unit_price,
    })),
  },
  {
    part_id: 8,
    part_number: 'HW-RIVET-125',
    part_name: 'Steel rivet · 1/8 inch',
    part_type: 'hardware',
    unit_of_measure: 'each',
    currency: null,
    latest_unit_price: 0.48,
    previous_unit_price: 0.5,
    price_change: -0.02,
    price_change_percent: -4,
    last_order_date: '2026-09-09',
    latest_po_id: 207,
    latest_po_number: 'PO-0207',
    latest_vendor_id: 6,
    latest_vendor_name: 'Bravo Supply',
    order_count: 7,
    total_quantity: 14000,
    total_spend: 7120,
    sparkline: [0.53, 0.52, 0.53, 0.49, 0.51, 0.5, 0.48].map((unit_price, index) => ({
      purchase_order_id: 201 + index,
      order_date: dates[index],
      unit_price,
    })),
  },
  {
    part_id: 9,
    part_number: 'SS-304-125',
    part_name: '304 stainless plate · .125 × 48 × 96',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    currency: null,
    latest_unit_price: 284,
    previous_unit_price: 284,
    price_change: 0,
    price_change_percent: 0,
    last_order_date: '2026-09-08',
    latest_po_id: 307,
    latest_po_number: 'PO-0307',
    latest_vendor_id: 5,
    latest_vendor_name: 'Acme Metals',
    order_count: 7,
    total_quantity: 70,
    total_spend: 19470,
    sparkline: [268, 271, 274, 280, 286, 284, 284].map((unit_price, index) => ({
      purchase_order_id: 301 + index,
      order_date: dates[index],
      unit_price,
    })),
  },
  {
    part_id: 10,
    part_number: 'SUP-ABRASIVE-80',
    part_name: '80 grit flap disc · 4-1/2 inch',
    part_type: 'consumable',
    unit_of_measure: 'each',
    currency: null,
    latest_unit_price: 4.85,
    previous_unit_price: 4.25,
    price_change: 0.6,
    price_change_percent: 14.1176,
    last_order_date: '2026-09-06',
    latest_po_id: 407,
    latest_po_number: 'PO-0407',
    latest_vendor_id: 6,
    latest_vendor_name: 'Bravo Supply',
    order_count: 7,
    total_quantity: 700,
    total_spend: 2975,
    sparkline: [3.95, 4.15, 4.1, 4.25, 4.2, 4.25, 4.85].map((unit_price, index) => ({
      purchase_order_id: 401 + index,
      order_date: dates[index],
      unit_price,
    })),
  },
];

function detailFor(part: MaterialPriceSummary, params: URLSearchParams): MaterialPriceHistoryDetail {
  const quantity = part.total_quantity / part.order_count;
  const chronological: MaterialPricePurchase[] = part.sparkline
    .map((point, index) => {
      const previous = index > 0 ? part.sparkline[index - 1].unit_price : null;
      return {
        ...point,
        po_number: `PO-${String(point.purchase_order_id).padStart(4, '0')}`,
        status: index === 6 ? 'sent' : 'closed',
        vendor_id: index % 2 ? 6 : 5,
        vendor_name: index % 2 ? 'Bravo Supply' : 'Acme Metals',
        quantity_ordered: quantity,
        extended_price: quantity * point.unit_price,
        line_count: index === 6 ? 2 : 1,
        previous_unit_price: previous,
        price_change: previous == null ? null : point.unit_price - previous,
        price_change_percent: previous ? ((point.unit_price - previous) / previous) * 100 : null,
        unit_of_measure: part.unit_of_measure,
        currency: null,
      };
    })
    .filter(
      row =>
        (!params.get('vendor_id') || row.vendor_id === Number(params.get('vendor_id'))) &&
        (!params.get('start_date') || row.order_date >= params.get('start_date')!) &&
        (!params.get('end_date') || row.order_date <= params.get('end_date')!)
    );
  // Match the endpoint: comparison follows consecutive POs in the chosen scope.
  chronological.forEach((row, index) => {
    const previous = chronological[index - 1]?.unit_price ?? null;
    row.previous_unit_price = previous;
    row.price_change = previous == null ? null : row.unit_price - previous;
    row.price_change_percent = previous ? ((row.unit_price - previous) / previous) * 100 : null;
  });
  const latest = chronological[chronological.length - 1];
  const totalQuantity = chronological.reduce((sum, row) => sum + row.quantity_ordered, 0);
  const totalSpend = chronological.reduce((sum, row) => sum + row.extended_price, 0);
  return {
    part,
    history: [...chronological].reverse(),
    total: chronological.length,
    page: 1,
    page_size: 50,
    stats: {
      latest_unit_price: latest?.unit_price ?? null,
      previous_unit_price: latest?.previous_unit_price ?? null,
      price_change: latest?.price_change ?? null,
      price_change_percent: latest?.price_change_percent ?? null,
      lowest_unit_price: chronological.length ? Math.min(...chronological.map(row => row.unit_price)) : null,
      highest_unit_price: chronological.length ? Math.max(...chronological.map(row => row.unit_price)) : null,
      weighted_average_unit_price: totalQuantity ? totalSpend / totalQuantity : null,
      total_quantity: totalQuantity,
      total_spend: totalSpend,
      order_count: chronological.length,
    },
    chart: chronological,
    chart_truncated: false,
    vendor_options: vendors,
    notes: [
      'PO unit costs exclude shipping and tax. Currency is not recorded on purchase orders.',
      'Multiple lines for the same item on a PO are combined using ordered quantities.',
    ],
  };
}

async function mockWorkspace(page: Page, baseURL: string) {
  const frontendOrigin = new URL(baseURL).origin;
  await page.addInitScript(savedUser => {
    sessionStorage.setItem('token', 'synthetic-local-test-token');
    sessionStorage.setItem('user', JSON.stringify(savedUser));
    localStorage.setItem('werco-completed-tours:1:9001', '["getting-started"]');
  }, user);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/v1/')) {
      let json: unknown = {};
      if (url.pathname === '/api/v1/users/me') json = user;
      else if (url.pathname === '/api/v1/companies/me') json = { id: 1, name: 'Werco Manufacturing', slug: 'werco' };
      else if (url.pathname.includes('unread-count') || url.pathname.includes('pending-approvals/summary'))
        json = { count: 0 };
      else if (url.pathname === '/api/v1/purchasing/price-history') {
        const search = (url.searchParams.get('search') || '').toLowerCase();
        const matching = inventory.filter(
          item =>
            (!search || `${item.part_number} ${item.part_name}`.toLowerCase().includes(search)) &&
            (!url.searchParams.get('part_type') || item.part_type === url.searchParams.get('part_type'))
        );
        const trend = url.searchParams.get('trend');
        const items = matching.filter(
          item =>
            !trend ||
            trend === 'all' ||
            (trend === 'up' && (item.price_change ?? 0) > 0) ||
            (trend === 'down' && (item.price_change ?? 0) < 0)
        );
        json = {
          items,
          total: items.length,
          page: 1,
          page_size: 25,
          summary: {
            tracked_parts: matching.length,
            price_increases: matching.filter(item => (item.price_change ?? 0) > 0).length,
            price_decreases: matching.filter(item => (item.price_change ?? 0) < 0).length,
            unchanged_parts: matching.filter(item => item.price_change === 0).length,
            new_parts: 0,
          },
        };
      } else if (/\/purchasing\/price-history\/\d+$/.test(url.pathname)) {
        const part = inventory.find(item => item.part_id === Number(url.pathname.split('/').pop()));
        if (!part) return route.fulfill({ status: 404, json: { detail: 'Item not found' } });
        json = detailFor(part, url.searchParams);
      }
      return route.fulfill({ json });
    }
    if (url.origin !== frontendOrigin) return route.abort();
    return route.continue();
  });
}

test.beforeEach(async ({ page, baseURL }) => {
  test.skip(
    !baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname),
    'Synthetic QA runs only against a local frontend.'
  );
  await mockWorkspace(page, baseURL!);
});

for (const viewport of [
  { name: 'desktop', width: 1440, height: 1100 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`${viewport.name}: purchase costs are readable, filterable and linked to source POs`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const pageErrors: string[] = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto('/purchasing/price-history');
    await expect(page.getByRole('heading', { level: 1, name: /material price history/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /PO-0107/ }).first()).toHaveAttribute('href', '/purchasing?po=107');
    await expect(page.getByLabel('Supplier')).toBeVisible();
    const layout = await page.evaluate(() => ({
      width: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    const outDir = path.resolve('.browser-harness');
    await mkdir(outDir, { recursive: true });
    await page.screenshot({ path: path.join(outDir, `material-price-history-${viewport.name}.png`), fullPage: true });
    expect(layout.scrollWidth, JSON.stringify(layout)).toBeLessThanOrEqual(layout.width);

    await page.getByLabel('Supplier').selectOption('6');
    await expect(page.getByRole('link', { name: /PO-0107/ })).toHaveCount(0);
    await expect(page.getByRole('link', { name: /PO-0106/ }).first()).toBeVisible();
    await page.getByRole('button', { name: /HW-RIVET-125/ }).click();
    await expect(page).toHaveURL(/part=8/);
    await expect(page.getByRole('link', { name: /PO-0207/ }).first()).toBeVisible();
    await expect(page.getByLabel('Supplier')).toHaveValue('');
    if (viewport.name === 'mobile')
      await expect(page.getByRole('region', { name: 'Item price details' })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(pageErrors).toEqual([]);
  });
}

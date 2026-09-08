import { test, expect } from './fixtures';

test('real browser performance reaches the ERP without query data', async ({ adminPage: page }) => {
  const config = page.waitForResponse(response => response.url().endsWith('/runtime-metrics/config'));
  await page.goto('/work-orders?ux_probe=private-customer-name');
  expect((await config).status()).toBe(200);
  await expect(page.getByRole('heading', { name: 'Work Orders', exact: true })).toBeVisible();
  const upload = page.waitForResponse(response => (
    response.url().endsWith('/runtime-metrics/samples') && response.request().method() === 'POST'
  ));
  // An actual input finalizes LCP; no browser metric or network response is mocked.
  await page.getByRole('heading', { name: 'Work Orders', exact: true }).click();
  const receipt = await upload;
  expect(receipt.status()).toBe(202);
  const body = receipt.request().postDataJSON();
  expect(body.samples.length).toBeGreaterThan(0);
  expect(JSON.stringify(body)).not.toContain('private-customer-name');
  expect(JSON.stringify(body)).not.toContain('ux_probe');
  for (const sample of body.samples) {
    expect(sample.route).toBe('/work-orders');
    expect(Object.keys(sample).sort()).toEqual([
      'device', 'metric_id', 'name', 'navigation', 'release', 'route', 'sequence', 'value',
    ]);
  }
  await page.goto('/admin/settings?tab=performance');
  await expect(page.getByRole('heading', { name: 'App performance', exact: true })).toBeVisible();
  await expect(page.getByRole('table', { name: 'Real user performance by screen and release' })).toBeVisible();
  await expect(page.getByText('Loading (LCP)', { exact: true }).first()).toBeVisible();
});

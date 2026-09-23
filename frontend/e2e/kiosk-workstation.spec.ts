import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

// Exercise the actual kiosk, storage and isolated fetch client without seeded
// users or manufacturing records. Backend tenant/auth fences have API tests.
for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`operator kiosk needs no workstation URL at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const user = {
      id: 11, company_id: 1, email: 'operator@example.test', employee_id: 'E011',
      first_name: 'Pat', last_name: 'Operator', role: 'operator', is_active: true,
    };
    const centers = [
      { id: 7, code: 'WELD1', name: 'Weld Bay 1', is_active: true },
      { id: 8, code: 'LASER1', name: 'Laser Cutting', is_active: true },
    ];
    await page.addInitScript((operator) => {
      sessionStorage.setItem('token', 'test-operator-token');
      sessionStorage.setItem('user', JSON.stringify(operator));
    }, user);
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/auth/me')) {
        await route.fulfill({ json: user });
      } else if (path.endsWith('/companies/me')) {
        await route.fulfill({ json: { id: 1, name: 'Test Shop' } });
      } else if (/\/work-centers\/?$/.test(path)) {
        await route.fulfill({ json: centers });
      } else if (path.endsWith('/shop-floor/my-active-job')) {
        await route.fulfill({ json: { active_jobs: [] } });
      } else if (path.includes('/work-center-queue/')) {
        const center = centers.find((option) => path.endsWith(`/${option.id}`));
        await route.fulfill({ json: { queue: [], held: [], work_center: center, server_time: new Date().toISOString() } });
      } else if (path.includes('/quality/scrap-reason-codes')) {
        await route.fulfill({ json: [] });
      } else {
        await route.fulfill({ status: 404, json: { detail: 'Not part of kiosk setup' } });
      }
    });

    await page.goto('/kiosk');
    await expect(page.getByRole('heading', { name: 'Choose workstation' })).toBeVisible();
    await page.getByRole('button', { name: /WELD1/ }).click();
    await expect(page.locator('header')).toContainText('WELD1');
    await page.getByRole('button', { name: 'Change workstation' }).click();
    await page.getByRole('button', { name: /LASER1/ }).click();
    await expect(page.locator('header')).toContainText('LASER1');
    await page.goto('/kiosk');
    await expect(page.locator('header')).toContainText('LASER1');
    await expect(page.getByRole('button', { name: 'Change workstation' })).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`operator-kiosk-${viewport.width}.png`), fullPage: true });
  });

  test(`crew workstation selection persists at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const centers = [
      { id: 7, code: 'WELD1', name: 'Weld Bay 1' },
      { id: 8, code: 'LASER1', name: 'Laser Cutting' },
      { id: 9, code: 'ASSEMBLY', name: 'Final Assembly' },
    ];
    let station = {
      id: 3, label: 'Shop Tablet', work_center_id: 7,
      work_center_code: 'WELD1', work_center_name: 'Weld Bay 1',
    };
    await page.addInitScript((initialStation) => {
      sessionStorage.setItem('kiosk_station_token', 'test-station-token');
      if (!sessionStorage.getItem('kiosk_station_info')) {
        sessionStorage.setItem('kiosk_station_info', JSON.stringify(initialStation));
      }
    }, station);
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith('/kiosk-stations/work-centers')) {
        expect(request.headers().authorization).toBe('Bearer test-station-token');
        await route.fulfill({ json: { work_centers: centers, station } });
      } else if (path.endsWith('/kiosk-stations/work-center') && request.method() === 'PUT') {
        expect(request.headers().authorization).toBe('Bearer test-station-token');
        const selected = centers.find((center) => center.id === request.postDataJSON().work_center_id)!;
        station = { ...station, work_center_id: selected.id, work_center_code: selected.code, work_center_name: selected.name };
        await route.fulfill({ json: station });
      } else if (path.includes('/work-center-queue/')) {
        expect(path).toMatch(new RegExp(`/${station.work_center_id}$`));
        await route.fulfill({ json: { queue: [], held: [], station, server_time: new Date().toISOString() } });
      } else {
        await route.fulfill({ status: 404, json: { detail: 'Not part of kiosk setup' } });
      }
    });

    await page.goto('/kiosk?kiosk=1&station=3');
    await page.getByRole('button', { name: 'Change workstation' }).click();
    await expect(page.getByRole('heading', { name: 'Choose workstation' })).toBeVisible();
    await expect(page.getByRole('button', { name: /WELD1/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('button', { name: /LASER1/ })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(accessibility.violations.filter((issue) => ['serious', 'critical'].includes(issue.impact || ''))).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`workstation-picker-${viewport.width}.png`), fullPage: true });

    await page.getByRole('searchbox', { name: 'Find a workstation' }).fill('laser');
    await expect(page.getByRole('button', { name: /WELD1/ })).toHaveCount(0);
    await page.getByRole('button', { name: /LASER1/ }).click();
    await expect(page.locator('header')).toContainText('Shop Tablet · LASER1');
    await page.reload();
    await expect(page.locator('header')).toContainText('Shop Tablet · LASER1');
    await page.getByRole('button', { name: 'Change workstation' }).click();
    await expect(page.getByRole('button', { name: /LASER1/ })).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('header')).toContainText('Shop Tablet · LASER1');
  });
}

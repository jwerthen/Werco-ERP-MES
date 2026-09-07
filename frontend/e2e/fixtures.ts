/**
 * E2E Test Fixtures
 *
 * Shared test utilities and authentication helpers.
 *
 * Test credentials are loaded from environment variables:
 * - E2E_ADMIN_EMAIL, E2E_ADMIN_SECRET
 * - E2E_MANAGER_EMAIL, E2E_MANAGER_SECRET
 * - E2E_OPERATOR_EMAIL, E2E_OPERATOR_SECRET
 */

import { test as base, expect, Locator, Page } from '@playwright/test';

// Test user credentials from environment
export const TEST_USERS = {
  admin: {
    email: process.env.E2E_ADMIN_EMAIL || 'admin@werco.com',
    secret: process.env.E2E_ADMIN_SECRET || '',
    role: 'admin',
  },
  manager: {
    email: process.env.E2E_MANAGER_EMAIL || 'manager@werco.com',
    secret: process.env.E2E_MANAGER_SECRET || '',
    role: 'manager',
  },
  operator: {
    email: process.env.E2E_OPERATOR_EMAIL || 'operator@werco.com',
    secret: process.env.E2E_OPERATOR_SECRET || '',
    role: 'operator',
  },
};

// Extended test with authentication helpers
export const test = base.extend<{
  authenticatedPage: Page;
  adminPage: Page;
}>({
  // Workflow tests exercise the ERP against its seeded API. External web fonts
  // are cosmetic and can hold document load open when the font host stalls.
  // Keep these tests independent of that third-party service; the browser uses
  // the app's declared fallback fonts and all UI assertions remain unchanged.
  page: async ({ page }, use) => {
    await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
    await use(page);
  },
  // Page with operator logged in
  authenticatedPage: async ({ page }, use) => {
    await loginAs(page, TEST_USERS.operator);
    await use(page);
  },

  // Page with admin logged in
  adminPage: async ({ page }, use) => {
    await loginAs(page, TEST_USERS.admin);
    await use(page);
  },
});

/**
 * Login as a specific user
 */
export async function loginAs(page: Page, user: typeof TEST_USERS.admin) {
  await page.goto('/login');
  await page.fill('input[name="email"]', user.email);
  await page.fill('input[type="password"]', user.secret);
  await page.click('button[type="submit"]');

  // Wait for the post-login redirect. Operators are redirected to the
  // shop-floor kiosk route (/shop-floor/operations?kiosk=1), everyone else
  // to their role default — so wait for "no longer on /login" rather than a
  // specific destination URL.
  await page.waitForURL(url => !url.pathname.startsWith('/login'), { timeout: 10000 });

  // Use the actual UI-authenticated identity, before a test opens Dashboard.
  // TourProvider intentionally ignores the former global preference; only this
  // user's completion is seeded, and other tours/workspaces remain unchanged.
  // No token is read or exposed, and the real login/role redirect still runs.
  await page.evaluate(() => {
    const user = JSON.parse(sessionStorage.getItem('user') || 'null');
    if (!user || typeof user.id !== 'number') {
      throw new Error('E2E login did not persist an authenticated user');
    }
    const key = `werco-completed-tours:${user.company_id ?? 'workspace'}:${user.id}`;
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    const completed = Array.isArray(saved) ? saved.filter(item => typeof item === 'string') : [];
    localStorage.setItem(key, JSON.stringify(Array.from(new Set([...completed, 'getting-started']))));
  });
}

/**
 * Logout current user
 */
export async function logout(page: Page) {
  await page.click('button[title="Sign out"]');
  await page.waitForURL(url => url.pathname === '/login');
  // A pending protected request may retain its destination when logout wins.
  // Login itself must never become the return destination of a second redirect.
  const returnTo = new URL(page.url()).searchParams.get('returnTo');
  if (returnTo) {
    expect(new URL(returnTo, page.url()).pathname).not.toMatch(/^\/login\/?$/i);
  }
}

/**
 * Wait for API response
 */
export async function waitForApi(page: Page, urlPattern: string | RegExp) {
  return page.waitForResponse(
    response => {
      const url = response.url();
      if (typeof urlPattern === 'string') {
        return url.includes(urlPattern);
      }
      return urlPattern.test(url);
    },
    { timeout: 10000 }
  );
}

/**
 * Navigate to a page and wait for it to load
 */
export async function navigateTo(page: Page, path: string, waitForSelector?: string) {
  await page.goto(path);
  if (waitForSelector) {
    await page.waitForSelector(waitForSelector, { timeout: 10000 });
  }
}

/**
 * Fill form field by label
 */
export async function fillField(page: Page, label: string, value: string) {
  const input = page.locator(`label:has-text("${label}") + input, label:has-text("${label}") + textarea`).first();
  await input.fill(value);
}

/**
 * Select option from dropdown by label
 */
export async function selectOption(page: Page, label: string, value: string) {
  const select = page.locator(`label:has-text("${label}") + select`).first();
  await select.selectOption(value);
}

/**
 * Click button by text
 */
export async function clickButton(page: Page, text: string) {
  await page.click(`button:has-text("${text}")`);
}

/**
 * Assert toast message appears
 */
export async function expectToast(page: Page, message: string | RegExp) {
  const toast = page.locator('.toast, [role="alert"]').filter({ hasText: message });
  await expect(toast).toBeVisible({ timeout: 5000 });
}

/**
 * Assert table row exists
 */
export async function expectTableRow(page: Page, text: string) {
  await expect(page.locator('table tbody tr').filter({ hasText: text })).toBeVisible();
}

/**
 * The first REAL row of a list table, or null when the list genuinely has none.
 *
 * `table tbody tr` is NOT a "the list has loaded" signal, and treating it as one
 * is what made the detail-title test lose a race on essentially every cold CI run.
 * Every lazy route mounts under `<Suspense fallback={<PageLoader/>}>`
 * (src/App.tsx), and PageLoader renders SkeletonDashboard, which ends in a REAL
 * `<table><tbody>` of five inert `<tr class="animate-pulse">`
 * (src/components/ui/Skeleton.tsx). Those rows are present, visible and
 * clickable, and they carry no click handler.
 *
 * So a test that waits on `table tbody tr` can resolve against a decoration,
 * click it, schedule no navigation at all, and then wait out its entire budget.
 * Raising the timeout can never help — nothing was ever going to happen. The
 * previous "fix" here removed a timeout on exactly that misreading.
 *
 * Both skeleton sources carry `animate-pulse` (the Suspense fallback and
 * DataTable's own loading rows), and DataTable's group headers are excluded by
 * their testid. What survives is a row that actually came from data.
 *
 * The jest suite already learned this: WorkOrders.render.test.tsx waits for real
 * row CONTENT for the same reason. This is that rule, ported to Playwright.
 */
export async function firstDataRow(page: Page, timeout = 15000): Promise<Locator | null> {
  const row = page.locator('table tbody tr:not(.animate-pulse):not([data-testid="group-header"])').first();
  await row.waitFor({ state: 'visible', timeout }).catch(() => null);
  return (await row.isVisible().catch(() => false)) ? row : null;
}

// Re-export expect for convenience
export { expect };

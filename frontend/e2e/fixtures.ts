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

import { test as base, expect, Page } from '@playwright/test';

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
  
  // Wait for redirect to dashboard
  await page.waitForURL('**/dashboard', { timeout: 10000 });
}

/**
 * Logout current user
 */
export async function logout(page: Page) {
  // Click user menu and logout
  await page.click('[data-testid="user-menu"]');
  await page.click('text=Logout');
  await page.waitForURL('**/login');
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

// Re-export expect for convenience
export { expect };

/**
 * Authentication E2E Tests
 * 
 * Tests for login, logout, and authentication flows.
 */

import { test, expect, TEST_USERS, loginAs, logout } from './fixtures';

test.describe('Authentication', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
  });

  test('displays login page', async ({ page }) => {
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[type="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });

  test('shows error for invalid credentials', async ({ page }) => {
    await page.fill('input[name="email"]', 'invalid@test.com');
    await page.fill('input[type="password"]', 'invalid');
    await page.click('button[type="submit"]');
    
    // Should show error message
    await expect(page.locator('text=/invalid|incorrect|failed/i')).toBeVisible({ timeout: 5000 });
    
    // Should stay on login page
    await expect(page).toHaveURL(/\/login/);
  });

  test('shows validation errors for empty fields', async ({ page }) => {
    await page.click('button[type="submit"]');

    // Native HTML5 `required` validation blocks submission and marks the field
    // invalid (no network request fires; the page stays on /login).
    await expect(page).toHaveURL(/\/login/);
    const emailInvalid = await page
      .locator('input[name="email"]')
      .evaluate((el: HTMLInputElement) => !el.validity.valid);
    expect(emailInvalid).toBe(true);
  });

  test('successful login redirects to the role default landing', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);

    // B0.3 "Action Inbox as the front door": admins/managers land on the
    // Action Inbox by default (see utils/defaultLanding.ts), not "/".
    await expect(page).toHaveURL(/\/action-inbox/);
    await expect(page.locator('text=/action inbox/i').first()).toBeVisible();

    // The classic dashboard remains reachable at "/".
    await page.goto('/');
    await expect(page.locator('text=/dashboard|overview/i').first()).toBeVisible();
  });

  test('remembers user session after page reload', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    
    // Reload the page
    await page.reload();
    
    // Should still be logged in (not redirected to login)
    await expect(page).not.toHaveURL(/\/login/);
  });

  test('logout clears session', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    
    // Logout
    await logout(page);
    
    // Should be on login page
    await expect(page).toHaveURL(/\/login/);
    
    // Trying to access protected page should redirect to login
    await page.goto('/parts');
    await expect(page).toHaveURL(/\/login/);
  });

  test('unauthorized access redirects to login', async ({ page }) => {
    // Try to access protected page without login
    await page.goto('/parts');
    
    // Should redirect to login
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe('Role-Based Access', () => {
  test('admin can access admin settings', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/admin/settings');
    
    // Should be able to access admin page
    await expect(page).toHaveURL(/\/admin\/settings/);
    await expect(page.locator('text=/settings|configuration/i').first()).toBeVisible();
  });

  test('operator cannot access admin settings', async ({ page }) => {
    await loginAs(page, TEST_USERS.operator);
    await page.goto('/admin/settings');

    // AdminRoute (requireAdmin) redirects non-admins away client-side, so the
    // operator must not remain on the admin settings page.
    await expect(page).not.toHaveURL(/\/admin\/settings/, { timeout: 10000 });
  });

  test('operator can access shop floor', async ({ page }) => {
    await loginAs(page, TEST_USERS.operator);
    await page.goto('/shop-floor');
    
    // Should be able to access shop floor
    await expect(page).toHaveURL(/\/shop-floor/);
  });
});

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
    
    // Should show validation errors
    await expect(page.locator('text=/required|enter/i')).toBeVisible();
  });

  test('successful login redirects to dashboard', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    
    // Should be on dashboard
    await expect(page).toHaveURL(/\/dashboard/);
    
    // Dashboard elements should be visible
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
    await page.goto('/dashboard');
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
    await page.goto('/admin');
    
    // Should be able to access admin page
    await expect(page).toHaveURL(/\/admin/);
    await expect(page.locator('text=/settings|configuration/i').first()).toBeVisible();
  });

  test('operator cannot access admin settings', async ({ page }) => {
    await loginAs(page, TEST_USERS.operator);
    await page.goto('/admin');
    
    // Should see unauthorized or be redirected
    const isUnauthorized = await page.locator('text=/unauthorized|access denied|forbidden/i').isVisible().catch(() => false);
    const isRedirected = await page.url().includes('/unauthorized') || await page.url().includes('/dashboard');
    
    expect(isUnauthorized || isRedirected).toBe(true);
  });

  test('operator can access shop floor', async ({ page }) => {
    await loginAs(page, TEST_USERS.operator);
    await page.goto('/shop-floor');
    
    // Should be able to access shop floor
    await expect(page).toHaveURL(/\/shop-floor/);
  });
});

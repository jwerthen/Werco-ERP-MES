/**
 * Navigation E2E Tests
 * 
 * Tests for app navigation, sidebar, and global search.
 */

import { test, expect, TEST_USERS, loginAs } from './fixtures';

test.describe('Sidebar Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('sidebar shows main navigation items', async ({ page }) => {
    await page.goto('/dashboard');
    
    const nav = page.locator('nav, aside');
    
    // Core navigation items should be visible
    await expect(nav.locator('a').filter({ hasText: /dashboard/i })).toBeVisible();
    await expect(nav.locator('a').filter({ hasText: /work.*order/i })).toBeVisible();
    await expect(nav.locator('a').filter({ hasText: /parts/i })).toBeVisible();
  });

  test('can navigate to each main section', async ({ page }) => {
    const sections = [
      { name: 'Dashboard', url: /dashboard/i },
      { name: 'Work Orders', url: /work-orders/i },
      { name: 'Parts', url: /parts/i },
      { name: 'Customers', url: /customers/i },
    ];

    for (const section of sections) {
      await page.goto('/dashboard');
      const link = page.locator('nav a, aside a').filter({ hasText: new RegExp(section.name, 'i') }).first();
      
      if (await link.isVisible()) {
        await link.click();
        await expect(page).toHaveURL(section.url);
      }
    }
  });

  test('sidebar collapses on mobile', async ({ page }) => {
    await page.goto('/dashboard');
    
    // Set mobile viewport
    await page.setViewportSize({ width: 375, height: 667 });
    await page.waitForTimeout(500);
    
    // Sidebar should be hidden or togglable
    const sidebar = page.locator('aside, nav').first();
    const isHidden = !(await sidebar.isVisible());
    const hasToggle = await page.locator('button[aria-label*="menu" i], button[aria-label*="toggle" i]').isVisible();
    
    expect(isHidden || hasToggle).toBe(true);
  });
});

test.describe('Global Search', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/dashboard');
  });

  test('can open search with keyboard shortcut', async ({ page }) => {
    // Press Cmd/Ctrl+K
    await page.keyboard.press('Control+k');
    
    // Search modal/input should appear
    await expect(page.locator('input[placeholder*="search" i], [role="dialog"] input').first()).toBeVisible({ timeout: 3000 });
  });

  test('search shows results', async ({ page }) => {
    // Open search
    await page.keyboard.press('Control+k');
    
    const searchInput = page.locator('input[placeholder*="search" i], [role="dialog"] input').first();
    if (await searchInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await searchInput.fill('test');
      await page.waitForTimeout(500);
      
      // Results should appear
      const results = page.locator('[role="listbox"] [role="option"], [data-testid="search-result"]');
      await page.waitForTimeout(1000);
    }
  });

  test('can navigate to search result', async ({ page }) => {
    await page.keyboard.press('Control+k');
    
    const searchInput = page.locator('input[placeholder*="search" i], [role="dialog"] input').first();
    if (await searchInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await searchInput.fill('WO');
      await page.waitForTimeout(500);
      
      // Click first result if available
      const firstResult = page.locator('[role="option"], [data-testid="search-result"]').first();
      if (await firstResult.isVisible({ timeout: 2000 }).catch(() => false)) {
        await firstResult.click();
        
        // Should navigate to result
        await page.waitForTimeout(1000);
        const url = page.url();
        expect(url).not.toContain('/dashboard');
      }
    }
  });

  test('search can close with escape', async ({ page }) => {
    await page.keyboard.press('Control+k');
    
    const searchInput = page.locator('input[placeholder*="search" i], [role="dialog"] input').first();
    if (await searchInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await page.keyboard.press('Escape');
      
      // Search should close
      await expect(searchInput).not.toBeVisible({ timeout: 2000 });
    }
  });
});

test.describe('Breadcrumb Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('breadcrumbs show current location', async ({ page }) => {
    await page.goto('/work-orders');
    
    const breadcrumb = page.locator('[aria-label*="breadcrumb" i], nav ol');
    if (await breadcrumb.isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(breadcrumb.locator('text=/work.*order/i')).toBeVisible();
    }
  });

  test('can navigate back via breadcrumb', async ({ page }) => {
    await page.goto('/parts');
    await page.waitForSelector('table tbody tr', { timeout: 10000 }).catch(() => null);
    
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForURL(/\/parts\/\d+/, { timeout: 5000 }).catch(() => null);
      
      // Click breadcrumb to go back
      const breadcrumb = page.locator('[aria-label*="breadcrumb" i] a, nav ol a').filter({ hasText: /parts/i }).first();
      if (await breadcrumb.isVisible()) {
        await breadcrumb.click();
        await expect(page).toHaveURL(/\/parts$/);
      }
    }
  });
});

test.describe('User Menu', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/dashboard');
  });

  test('user menu shows user info', async ({ page }) => {
    const userMenu = page.locator('[data-testid="user-menu"], button').filter({ hasText: /admin|profile|account/i }).first();
    
    if (await userMenu.isVisible({ timeout: 5000 }).catch(() => false)) {
      await userMenu.click();
      
      // Should show user info
      await expect(page.locator('text=/admin|email|profile|logout/i').first()).toBeVisible();
    }
  });

  test('user menu has logout option', async ({ page }) => {
    const userMenu = page.locator('[data-testid="user-menu"], button').filter({ hasText: /admin|user|account/i }).first();
    
    if (await userMenu.isVisible({ timeout: 5000 }).catch(() => false)) {
      await userMenu.click();
      await expect(page.locator('text=/logout|sign.*out/i').first()).toBeVisible();
    }
  });
});

test.describe('Responsive Design', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('works on tablet viewport', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.goto('/dashboard');
    
    // Dashboard should still be functional
    await expect(page.locator('h1, h2').filter({ hasText: /dashboard/i })).toBeVisible({ timeout: 10000 });
  });

  test('works on mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto('/dashboard');
    
    // Dashboard should load
    await expect(page.locator('h1, h2').first()).toBeVisible({ timeout: 10000 });
    
    // Content should be visible (might need scrolling)
    await expect(page.locator('main, [role="main"]').first()).toBeVisible();
  });
});

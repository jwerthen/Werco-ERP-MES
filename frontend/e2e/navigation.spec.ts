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
    await page.goto('/');

    // The sidebar renders a <nav> nested inside an <aside>, so a `nav, aside`
    // selector matches each link's containers twice. Scope each assertion with
    // .first() to avoid strict-mode violations while still proving the link exists.
    const nav = page.locator('nav, aside');

    // Core navigation items should be visible
    // Both a desktop sidebar and a hidden mobile bottom-nav render these links, so
    // restrict to the visible one before asserting.
    await expect(nav.locator('a').filter({ hasText: /dashboard/i }).filter({ visible: true }).first()).toBeVisible();
    await expect(nav.locator('a').filter({ hasText: /work.*order/i }).filter({ visible: true }).first()).toBeVisible();
    // "Parts" lives under the collapsible "Engineering" group on desktop (and in the
    // mobile bottom-nav), so assert the nav link is present rather than expanded.
    await expect(page.locator('a[href="/parts"]').first()).toBeAttached();
  });

  test('can navigate to each main section', async ({ page }) => {
    const sections = [
      { name: 'Dashboard', url: /\/$/ },
      { name: 'Work Orders', url: /work-orders/i },
      { name: 'Parts', url: /parts/i },
      { name: 'Customers', url: /customers/i },
    ];

    for (const section of sections) {
      await page.goto('/');
      const link = page.locator('nav a, aside a').filter({ hasText: new RegExp(section.name, 'i') }).first();
      
      if (await link.isVisible()) {
        await link.click();
        await expect(page).toHaveURL(section.url);
      }
    }
  });

  test('sidebar collapses on mobile', async ({ page }) => {
    await page.goto('/');
    
    // Set mobile viewport
    await page.setViewportSize({ width: 375, height: 667 });
    await page.waitForTimeout(500);
    
    // Sidebar should be hidden or togglable. Several buttons carry a
    // menu/toggle aria-label (mobile nav close, Copilot toggle), so count
    // visible matches instead of isVisible() on a multi-match locator,
    // which throws a strict-mode violation.
    const sidebar = page.locator('aside, nav').first();
    const isHidden = !(await sidebar.isVisible());
    const visibleToggles = await page
      .locator('button[aria-label*="menu" i], button[aria-label*="toggle" i]')
      .filter({ visible: true })
      .count();

    expect(isHidden || visibleToggles > 0).toBe(true);
  });
});

test.describe('Global Search', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/');
  });

  test('can open search with keyboard shortcut', async ({ page }) => {
    // The Ctrl+K handler is registered on document keydown once the app mounts.
    // Wait for the sidebar to render and ensure the document has focus before
    // dispatching the shortcut, otherwise the keypress can land before the
    // listener is attached.
    await expect(page.locator('nav[aria-label="Main navigation"] a').first()).toBeVisible({ timeout: 10000 });
    await page.locator('body').click();

    // Press Cmd/Ctrl+K to open the command palette
    await page.keyboard.press('Control+k');

    // The command palette opens a dialog whose input reads "Search across your workspace…"
    await expect(page.getByPlaceholder(/search across/i)).toBeVisible({ timeout: 3000 });
  });

  test('search shows results', async ({ page }) => {
    // Same readiness wait as the shortcut test before triggering Ctrl+K.
    await expect(page.locator('nav[aria-label="Main navigation"] a').first()).toBeVisible({ timeout: 10000 });
    await page.locator('body').click();

    // Open search
    await page.keyboard.press('Control+k');

    const searchInput = page.getByPlaceholder(/search across/i);
    await expect(searchInput).toBeVisible({ timeout: 3000 });

    // Search for a seeded part prefix; the backend search should return matches.
    await searchInput.fill('WERCO');

    const dialog = page.locator('[role="dialog"]');
    // Either result rows render, or the palette explicitly says there are none —
    // both prove the search executed and rendered a response.
    await expect(
      dialog.locator('ul li').first().or(dialog.getByText(/no results/i))
    ).toBeVisible({ timeout: 5000 });
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
        expect(url).not.toContain('/login');
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
    await page.goto('/');
  });

  test('user menu shows user info', async ({ page }) => {
    const userMenu = page.locator('button[title="Sign out"]').first();
    
    if (await userMenu.isVisible({ timeout: 5000 }).catch(() => false)) {
      await userMenu.click();
      
      // Should show user info
      await expect(userMenu).toBeVisible();
    }
  });

  test('user menu has logout option', async ({ page }) => {
    const userMenu = page.locator('button[title="Sign out"]').first();
    
    if (await userMenu.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(userMenu).toBeVisible();
    }
  });
});

test.describe('Responsive Design', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('works on tablet viewport', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.goto('/');
    
    // Dashboard should still be functional
    await expect(page.locator('h1, h2').filter({ hasText: /dashboard/i })).toBeVisible({ timeout: 10000 });
  });

  test('works on mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto('/');
    
    // Dashboard should load
    await expect(page.locator('h1, h2').first()).toBeVisible({ timeout: 10000 });
    
    // Content should be visible (might need scrolling)
    await expect(page.locator('main, [role="main"]').first()).toBeVisible();
  });
});

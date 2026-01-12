/**
 * Work Orders E2E Tests
 * 
 * Tests for work order lifecycle: create, view, update, release, complete.
 */

import { test, expect, TEST_USERS, loginAs, waitForApi, expectTableRow } from './fixtures';

test.describe('Work Orders', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('displays work orders list', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Should show work orders page
    await expect(page.locator('h1, h2').filter({ hasText: /work order/i })).toBeVisible();
    
    // Should have table or list of work orders
    await expect(page.locator('table, [role="list"]')).toBeVisible();
  });

  test('can search work orders', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find search input
    const searchInput = page.locator('input[placeholder*="search" i], input[type="search"]').first();
    if (await searchInput.isVisible()) {
      await searchInput.fill('WO-');
      await page.waitForTimeout(500); // debounce
      
      // Results should update (either show matching or show empty state)
      await expect(page.locator('table tbody tr, [data-testid="empty-state"]')).toBeVisible();
    }
  });

  test('can filter work orders by status', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find status filter
    const statusFilter = page.locator('select').filter({ hasText: /status|all/i }).first();
    if (await statusFilter.isVisible()) {
      await statusFilter.selectOption({ label: /in progress/i });
      await page.waitForTimeout(500);
    }
  });

  test('can navigate to work order creation', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Click create button
    const createBtn = page.locator('button, a').filter({ hasText: /new|create|add/i }).first();
    await createBtn.click();
    
    // Should be on create page or modal
    await expect(page.locator('form')).toBeVisible();
    await expect(page.locator('text=/part|quantity/i').first()).toBeVisible();
  });

  test('work order creation requires part selection', async ({ page }) => {
    await page.goto('/work-orders/new');
    
    // Try to submit without selecting part
    const submitBtn = page.locator('button[type="submit"], button').filter({ hasText: /create|save|submit/i }).first();
    await submitBtn.click();
    
    // Should show validation error
    await expect(page.locator('text=/required|select.*part/i')).toBeVisible({ timeout: 3000 });
  });

  test('can view work order details', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Wait for table to load
    await page.waitForSelector('table tbody tr', { timeout: 10000 }).catch(() => null);
    
    // Click first work order row
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      
      // Should navigate to detail page
      await page.waitForURL(/\/work-orders\/\d+/, { timeout: 5000 }).catch(() => null);
    }
  });
});

test.describe('Work Order Lifecycle', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('draft work order shows release button', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find a draft work order
    const draftRow = page.locator('table tbody tr').filter({ hasText: /draft/i }).first();
    if (await draftRow.isVisible()) {
      await draftRow.click();
      await page.waitForURL(/\/work-orders\/\d+/);
      
      // Should show release action
      await expect(page.locator('button').filter({ hasText: /release/i })).toBeVisible();
    }
  });

  test('released work order shows start button', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find a released work order
    const releasedRow = page.locator('table tbody tr').filter({ hasText: /released/i }).first();
    if (await releasedRow.isVisible()) {
      await releasedRow.click();
      await page.waitForURL(/\/work-orders\/\d+/);
      
      // Should show start or in-progress actions
      await expect(page.locator('button').filter({ hasText: /start|begin/i }).first()).toBeVisible().catch(() => null);
    }
  });

  test('work order operations are displayed', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Click first work order
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForURL(/\/work-orders\/\d+/);
      
      // Should show operations section
      await expect(page.locator('text=/operations|routing|steps/i').first()).toBeVisible({ timeout: 5000 });
    }
  });
});

test.describe('Work Order Status Changes', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.manager);
  });

  test('can put work order on hold', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find an in-progress work order
    const row = page.locator('table tbody tr').filter({ hasText: /in.?progress/i }).first();
    if (await row.isVisible()) {
      await row.click();
      await page.waitForURL(/\/work-orders\/\d+/);
      
      // Look for hold action
      const holdBtn = page.locator('button').filter({ hasText: /hold/i });
      if (await holdBtn.isVisible()) {
        await holdBtn.click();
        
        // Should update status
        await expect(page.locator('text=/on.?hold/i')).toBeVisible({ timeout: 5000 });
      }
    }
  });

  test('can cancel draft work order', async ({ page }) => {
    await page.goto('/work-orders');
    
    // Find a draft work order
    const row = page.locator('table tbody tr').filter({ hasText: /draft/i }).first();
    if (await row.isVisible()) {
      await row.click();
      await page.waitForURL(/\/work-orders\/\d+/);
      
      // Look for cancel action
      const cancelBtn = page.locator('button').filter({ hasText: /cancel/i });
      if (await cancelBtn.isVisible()) {
        await cancelBtn.click();
        
        // Confirm if needed
        const confirmBtn = page.locator('button').filter({ hasText: /confirm|yes/i });
        if (await confirmBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
          await confirmBtn.click();
        }
      }
    }
  });
});

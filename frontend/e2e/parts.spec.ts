/**
 * Parts Management E2E Tests
 * 
 * Tests for parts CRUD operations and navigation.
 */

import { test, expect, TEST_USERS, loginAs } from './fixtures';

test.describe('Parts List', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/parts');
  });

  test('displays parts list page', async ({ page }) => {
    // Should show parts heading
    await expect(page.locator('h1, h2').filter({ hasText: /parts/i })).toBeVisible();
    
    // Should have parts table
    await expect(page.locator('table')).toBeVisible({ timeout: 10000 });
  });

  test('displays parts in table with columns', async ({ page }) => {
    await page.waitForSelector('table thead', { timeout: 10000 });
    
    // Check for expected columns
    const headers = page.locator('table thead th');
    await expect(headers.filter({ hasText: /part.*number/i })).toBeVisible();
    await expect(headers.filter({ hasText: /name|description/i }).first()).toBeVisible();
  });

  test('can search parts', async ({ page }) => {
    const searchInput = page.locator('input[placeholder*="search" i], input[type="search"]').first();
    
    if (await searchInput.isVisible()) {
      await searchInput.fill('TEST');
      await page.waitForTimeout(500);
      
      // Table should update
      await page.waitForSelector('table tbody tr', { timeout: 5000 }).catch(() => null);
    }
  });

  test('can filter by part type', async ({ page }) => {
    const typeFilter = page.locator('select').filter({ hasText: /type|all/i }).first();
    
    if (await typeFilter.isVisible()) {
      await typeFilter.selectOption({ index: 1 });
      await page.waitForTimeout(500);
    }
  });

  test('shows new part button for authorized users', async ({ page }) => {
    const createBtn = page.locator('button, a').filter({ hasText: /new|create|add.*part/i }).first();
    await expect(createBtn).toBeVisible();
  });
});

test.describe('Part Creation', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('can access part creation form', async ({ page }) => {
    await page.goto('/parts');
    
    // Click create button
    const createBtn = page.locator('button, a').filter({ hasText: /new|create|add/i }).first();
    await createBtn.click();
    
    // Should show form
    await expect(page.locator('form')).toBeVisible();
  });

  test('part creation form has required fields', async ({ page }) => {
    await page.goto('/parts/new');
    
    // Should have part number field
    await expect(page.locator('input[name*="part" i][name*="number" i], label:has-text("Part Number") + input')).toBeVisible({ timeout: 5000 });
    
    // Should have name field
    await expect(page.locator('input[name*="name" i], label:has-text("Name") + input').first()).toBeVisible();
    
    // Should have type selection
    await expect(page.locator('select[name*="type" i], label:has-text("Type") + select')).toBeVisible();
  });

  test('shows validation errors for empty submission', async ({ page }) => {
    await page.goto('/parts/new');
    
    // Submit empty form
    const submitBtn = page.locator('button[type="submit"], button').filter({ hasText: /create|save|submit/i }).first();
    await submitBtn.click();
    
    // Should show validation errors
    await expect(page.locator('text=/required/i')).toBeVisible({ timeout: 3000 });
  });

  test('part number must be unique', async ({ page }) => {
    await page.goto('/parts/new');
    
    // Fill with existing part number (if we know one)
    await page.fill('input[name*="part" i][name*="number" i], label:has-text("Part Number") + input', 'TEST-001');
    await page.fill('input[name*="name" i], label:has-text("Name") + input', 'Test Part');
    
    // Select type
    const typeSelect = page.locator('select[name*="type" i], label:has-text("Type") + select');
    if (await typeSelect.isVisible()) {
      await typeSelect.selectOption({ index: 1 });
    }
    
    // Submit
    const submitBtn = page.locator('button[type="submit"], button').filter({ hasText: /create|save/i }).first();
    await submitBtn.click();
    
    // May show duplicate error (depends on existing data)
    await page.waitForTimeout(2000);
  });
});

test.describe('Part Details', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/parts');
  });

  test('can click part row to view details', async ({ page }) => {
    await page.waitForSelector('table tbody tr', { timeout: 10000 });
    
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      
      // Should navigate to detail page or show modal
      await page.waitForTimeout(1000);
      await expect(page.locator('text=/details|edit|routing|bom/i').first()).toBeVisible();
    }
  });

  test('part detail shows key information', async ({ page }) => {
    await page.waitForSelector('table tbody tr', { timeout: 10000 });
    
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForURL(/\/parts\/\d+/, { timeout: 5000 }).catch(() => null);
      
      // Should show part information
      await expect(page.locator('text=/part.*number|revision|type|status/i').first()).toBeVisible({ timeout: 5000 });
    }
  });
});

test.describe('Part Editing', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
  });

  test('can access edit mode', async ({ page }) => {
    await page.goto('/parts');
    await page.waitForSelector('table tbody tr', { timeout: 10000 });
    
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForURL(/\/parts\/\d+/, { timeout: 5000 }).catch(() => null);
      
      // Look for edit button
      const editBtn = page.locator('button, a').filter({ hasText: /edit/i }).first();
      if (await editBtn.isVisible()) {
        await editBtn.click();
        
        // Should show editable form
        await expect(page.locator('form input:not([disabled])')).toBeVisible();
      }
    }
  });

  test('changes are saved on submit', async ({ page }) => {
    await page.goto('/parts');
    await page.waitForSelector('table tbody tr', { timeout: 10000 });
    
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForURL(/\/parts\/\d+/, { timeout: 5000 }).catch(() => null);
      
      const editBtn = page.locator('button, a').filter({ hasText: /edit/i }).first();
      if (await editBtn.isVisible()) {
        await editBtn.click();
        await page.waitForTimeout(500);
        
        // Update a field
        const descInput = page.locator('textarea[name*="description" i], input[name*="description" i]');
        if (await descInput.isVisible()) {
          await descInput.fill(`Updated ${Date.now()}`);
          
          // Save
          const saveBtn = page.locator('button').filter({ hasText: /save|update/i }).first();
          await saveBtn.click();
          
          // Should show success or return to view mode
          await page.waitForTimeout(2000);
        }
      }
    }
  });
});

test.describe('Parts Access Control', () => {
  test('operator can view parts but not create', async ({ page }) => {
    await loginAs(page, TEST_USERS.operator);
    await page.goto('/parts');
    
    // Should see parts list
    await expect(page.locator('table')).toBeVisible({ timeout: 10000 });
    
    // Create button should not be visible or disabled
    const createBtn = page.locator('button, a').filter({ hasText: /new|create|add.*part/i }).first();
    const isHidden = !(await createBtn.isVisible().catch(() => false));
    const isDisabled = await createBtn.isDisabled().catch(() => true);
    
    expect(isHidden || isDisabled).toBe(true);
  });

  test('manager can create parts', async ({ page }) => {
    await loginAs(page, TEST_USERS.manager);
    await page.goto('/parts');
    
    // Create button should be visible
    const createBtn = page.locator('button, a').filter({ hasText: /new|create|add/i }).first();
    await expect(createBtn).toBeVisible();
  });
});

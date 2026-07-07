/**
 * Parts Management E2E Tests
 * 
 * Tests for parts CRUD operations and navigation.
 */

import { test, expect, TEST_USERS, loginAs } from './fixtures';
import type { Page } from '@playwright/test';

const API_URL = process.env.E2E_API_URL || 'http://localhost:8000/api/v1';

async function openCreatePartForm(page: Page) {
  await page.goto('/parts');
  await page.locator('button, a').filter({ hasText: /new|create|add/i }).first().click();
  await expect(page.locator('form')).toBeVisible();
}

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
    // The parts list defaults to the table view. Assert the page heading and the
    // actual column headers the app renders ("Part #" and "Name"), not assumed labels.
    await expect(page.locator('h1').filter({ hasText: /^parts$/i })).toBeVisible();
    await page.waitForSelector('table thead', { timeout: 10000 });

    const headers = page.locator('table thead th');
    await expect(headers.filter({ hasText: /part\s*#/i }).first()).toBeVisible();
    await expect(headers.filter({ hasText: /^name$/i }).first()).toBeVisible();

    // A known seeded part should appear in the table body. The WERCO-002 family
    // is always present (assemblies are never collapsed under another row).
    await expect(
      page.locator('table tbody tr')
        .filter({ hasText: /WERCO-002-01|WERCO-002-02|RAW-001|WERCO-002|WERCO-001/ })
        .first()
    ).toBeVisible({ timeout: 10000 });
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
    await openCreatePartForm(page);
    
    // Should have part number field
    await expect(page.locator('input[name*="part" i][name*="number" i], label:has-text("Part Number") + input')).toBeVisible({ timeout: 5000 });
    
    // Should have name field
    await expect(page.locator('input[name*="name" i], label:has-text("Name") + input').first()).toBeVisible();
    
    // Should have type selection
    await expect(page.locator('select[name*="type" i], label:has-text("Type") + select')).toBeVisible();
  });

  test('shows validation errors for empty submission', async ({ page }) => {
    await openCreatePartForm(page);

    // The New Part form guards required fields with native HTML5 validation:
    // Part Number / Revision / Name carry the `required` attribute, so an empty
    // submit is blocked by the browser and never reaches the API.
    const partNumberInput = page.locator('label:has-text("Part Number") + input').first();
    await expect(partNumberInput).toHaveJSProperty('required', true);

    let createCalled = false;
    page.on('request', (req) => {
      // api.createPart() POSTs to `${API}/parts/` (trailing slash, no sub-path).
      if (req.method() === 'POST' && /\/parts\/(\?.*)?$/.test(req.url())) createCalled = true;
    });

    // Attempt to submit the empty form.
    const submitBtn = page.locator('button[type="submit"]').filter({ hasText: /create part/i }).first();
    await submitBtn.click();

    // Submission is blocked: the modal/form stays open, the required field reports
    // invalid via the Constraint Validation API, and no create request was issued.
    await expect(page.locator('form')).toBeVisible();
    const partNumberValid = await partNumberInput.evaluate(
      (el) => (el as HTMLInputElement).checkValidity()
    );
    expect(partNumberValid).toBe(false);
    expect(createCalled).toBe(false);
  });

  test('part number must be unique', async ({ page }) => {
    await openCreatePartForm(page);

    // Fill every required field with a DUPLICATE part number. Revision defaults to
    // "A" and Type defaults to "manufactured", so Part Number + Name complete the form.
    // WERCO-002-01 is a seeded part, so the backend rejects it as a duplicate.
    await page.locator('label:has-text("Part Number") + input').first().fill('WERCO-002-01');
    await page.locator('label:has-text("Name") + input').first().fill('Duplicate Part Number Test');

    // Submit the now-valid form; the create request reaches the API.
    const submitBtn = page.locator('button[type="submit"]').filter({ hasText: /create part/i }).first();
    await submitBtn.click();

    // The API returns 400 "Part number already exists" and the page surfaces it as
    // an error toast. The modal stays open (no navigation to a new part detail page).
    await expect(page.getByText(/already exists/i)).toBeVisible({ timeout: 8000 });
    await expect(page).toHaveURL(/\/parts$/);
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
    // Resolve a real part id via the API (using the logged-in token) and open its
    // detail page directly — robust against list view-mode and row-click targeting
    // (the list->detail click flow is covered by "can click part row to view details").
    const token = await page.evaluate(() => sessionStorage.getItem('token'));
    const res = await page.request.get(`${API_URL}/parts/?limit=1`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await res.json();
    const part = Array.isArray(body) ? body[0] : (body.items || body.data || body.results || [])[0];
    expect(part?.id, 'expected at least one seeded part').toBeTruthy();
    await page.goto(`/parts/${part.id}`);

    // The detail page renders the part number as the page heading and a row of
    // quick stats on the default Overview tab — both are always visible (not
    // gated behind a tab panel). Assert against those visible elements.
    await expect(page.locator('h1').first()).toBeVisible({ timeout: 5000 });
    await expect(page.locator('h1').first()).toHaveText(/\w/);
    await expect(page.getByText('Standard Cost').first()).toBeVisible();
    await expect(page.getByText(/Rev\s+\w/).first()).toBeVisible();
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

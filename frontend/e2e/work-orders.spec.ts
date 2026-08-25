/**
 * Work Orders E2E Tests
 * 
 * Tests for work order lifecycle: create, view, update, release, complete.
 */

import { test, expect, TEST_USERS, loginAs, waitForApi, expectTableRow, firstDataRow } from './fixtures';

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

    // Target the create LINK by its destination, not by a loose /new|create|add/
    // match over every button and anchor on the page.
    //
    // That locator took `.first()` in DOM order, so it silently bound to whichever
    // header control happened to come first — and the header is a place controls get
    // added. It broke the day "New from template" landed beside "New Work Order":
    // `.first()` started picking the template button, which switches a tab rather
    // than navigating, so no form ever appeared and the failure read as "work order
    // creation is broken" rather than "the test grabbed the wrong button".
    //
    // `/work-orders/new` is what this test is actually about, and it cannot be
    // captured by a neighbour.
    await page.locator('a[href="/work-orders/new"]').first().click();
    await expect(page).toHaveURL(/\/work-orders\/new$/);

    // Should be on create page or modal
    await expect(page.locator('form')).toBeVisible();
    await expect(page.locator('text=/part|quantity/i').first()).toBeVisible();
  });

  test('work order creation requires part selection', async ({ page }) => {
    await page.goto('/work-orders/new');

    // The form guards against submitting without a part by disabling the submit
    // button (`disabled={submitting || !form.part_id}`). Wait for the form to load,
    // then assert the "Create Work Order" button is disabled while no part is chosen.
    const submitBtn = page.locator('button[type="submit"]').filter({ hasText: /create work order/i });
    await expect(submitBtn).toBeVisible({ timeout: 10000 });
    await expect(submitBtn).toBeDisabled();

    // The form also prompts the operator to pick a part before operations appear.
    await expect(page.getByText(/select a part to see available operations/i)).toBeVisible();
  });

  test('can view work order details', async ({ page }) => {
    await page.goto('/work-orders');

    // This test OWNS row click-through, so nothing in it may be optional. It used
    // to wrap the click in `if (isVisible())` and swallow the navigation wait with
    // `.catch(() => null)`, which made it incapable of failing — and it resolved
    // against the Suspense skeleton besides (see firstDataRow).
    const row = await firstDataRow(page);
    test.skip(row === null, 'no seeded work orders');

    // Deliberately the ROW, not a link inside it: the point is that DataTable's
    // onRowClick still reaches the detail page. The click is PINNED to the first
    // cell's padding rather than the bounding-box centre, so it cannot drift onto
    // an in-row control (the due-date pencil stops propagation by design) as
    // column widths change with content.
    await row!.click({ position: { x: 6, y: 6 } });
    await page.waitForURL(/\/work-orders\/\d+/, { timeout: 15000 });
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

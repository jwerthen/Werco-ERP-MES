import { test, expect } from './fixtures';

test('saved work-order views restore filters and layout across a reload', async ({ adminPage: page }) => {
  await page.goto('/work-orders');
  await expect(page.getByLabel('Saved views', { exact: true })).toBeEnabled();
  await page.getByLabel('Status filter', { exact: true }).selectOption('released');
  await page.getByRole('button', { name: 'Table options', exact: true }).click();
  await page.getByLabel('Compact rows', { exact: true }).check();
  await page.getByLabel('Customer', { exact: true }).uncheck();
  await page.getByRole('button', { name: 'Save layout', exact: true }).click();
  await expect(page.getByText('Layout saved for your next visit.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Save current view', exact: true }).click();
  const name = `Released jobs ${Date.now()}`;
  await page.getByLabel('View name', { exact: true }).fill(name);
  await page.getByRole('button', { name: 'Save view', exact: true }).click();
  await expect(page.getByText('View saved to your account.', { exact: true })).toBeVisible();
  await page.goto('/work-orders?status=on_hold');
  await expect(page.getByLabel('Saved views', { exact: true })).toBeEnabled();
  await expect(page.getByLabel('Status filter', { exact: true })).toHaveValue('on_hold');
  await page.getByLabel('Saved views', { exact: true }).selectOption({ label: name });
  await page.getByRole('button', { name: 'Apply view', exact: true }).click();
  await expect(page.getByLabel('Status filter', { exact: true })).toHaveValue('released');
  await page.getByRole('button', { name: 'Table options', exact: true }).click();
  await expect(page.getByLabel('Compact rows', { exact: true })).toBeChecked();
  await expect(page.getByLabel('Customer', { exact: true })).not.toBeChecked();
  await page.getByRole('button', { name: 'Remove view', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm removal', exact: true }).click();
  await expect(page.getByText('Saved view removed.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reset layout', exact: true }).click();
  await page.getByRole('button', { name: 'Save layout', exact: true }).click();
  await expect(page.getByText('Layout saved for your next visit.', { exact: true })).toBeVisible();
});

test('work-order form restores a server draft after reload without replacing fresh typing automatically', async ({
  adminPage: page,
}) => {
  page.on('dialog', dialog => dialog.accept());
  await page.goto('/work-orders/new');
  await expect(page.getByRole('button', { name: 'Save draft now', exact: true })).toBeEnabled();
  const note = `Recovery check ${Date.now()}`;
  await page.getByLabel('Notes', { exact: true }).fill(note);
  await page.getByRole('button', { name: 'Save draft now', exact: true }).click();
  await expect(page.getByText(/Draft saved \d/)).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Resume draft', exact: true })).toBeVisible();
  await expect(page.getByLabel('Notes', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(page.getByLabel('Notes', { exact: true })).toHaveValue(note);
  await page.reload();
  await page.getByRole('button', { name: 'Remove saved draft', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm removal', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume draft', exact: true })).toHaveCount(0);
});

test('purchase-order draft can be continued from its list and removed explicitly', async ({ adminPage: page }) => {
  page.on('dialog', dialog => dialog.accept());
  await page.goto('/purchasing');
  await page.getByRole('button', { name: 'New PO', exact: true }).first().click();
  const editor = page.getByRole('dialog');
  await expect(editor.getByRole('button', { name: 'Save draft now', exact: true })).toBeEnabled();
  await editor.getByLabel('Notes', { exact: true }).fill('Synthetic PO draft recovery');
  await editor.getByRole('button', { name: 'Save draft now', exact: true }).click();
  await expect(editor.getByText(/Draft saved \d/)).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Continue PO draft', exact: true }).click();
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(page.getByRole('dialog').getByLabel('Notes', { exact: true })).toHaveValue(
    'Synthetic PO draft recovery'
  );
  await page.reload();
  await page.getByRole('button', { name: 'Continue PO draft', exact: true }).click();
  await page.getByRole('button', { name: 'Remove saved draft', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm removal', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume draft', exact: true })).toHaveCount(0);
});

test('new quote draft preserves its entries after returning to the quote list', async ({ adminPage: page }) => {
  page.on('dialog', dialog => dialog.accept());
  await page.goto('/quotes');
  await page.getByRole('button', { name: 'New Quote', exact: true }).first().click();
  const editor = page.getByRole('dialog');
  await expect(editor.getByRole('button', { name: 'Save draft now', exact: true })).toBeEnabled();
  await editor.getByRole('textbox', { name: /Customer Name/ }).fill('Synthetic quote recovery');
  await editor.getByRole('button', { name: 'Save draft now', exact: true }).click();
  await expect(editor.getByText(/Draft saved \d/)).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Continue quote draft', exact: true }).click();
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('textbox', { name: /Customer Name/ })).toHaveValue(
    'Synthetic quote recovery'
  );
  await page.reload();
  await page.getByRole('button', { name: 'Continue quote draft', exact: true }).click();
  await page.getByRole('button', { name: 'Remove saved draft', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm removal', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume draft', exact: true })).toHaveCount(0);
});

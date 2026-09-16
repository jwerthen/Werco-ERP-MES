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
  await page.getByLabel('Saved views', { exact: true }).selectOption({ label: `${name} · Private` });
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

test('customer quote entry opens fabrication quoting and its saved draft survives reload and library reopen', async ({ adminPage: page }) => {
  page.on('dialog', dialog => dialog.accept());
  await page.goto('/quotes');
  await page.getByRole('button', { name: 'New Quote', exact: true }).first().click();
  await expect(page).toHaveURL(/\/fabrication-quotes$/);
  await expect(page.getByLabel('Quote title', { exact: true })).toBeEnabled();

  const title = `Synthetic quote recovery ${Date.now()}`;
  const partName = 'Synthetic recovery fixture';
  await page.getByLabel('Quote title', { exact: true }).fill(title);
  await page.getByRole('button', { name: 'Add part', exact: true }).click();
  await page.getByLabel('Part number / description', { exact: true }).fill(partName);
  await page.getByRole('button', { name: 'Add demand', exact: true }).click();
  await page.getByLabel('Customer quantity', { exact: true }).fill('17');

  const savedResponse = page.waitForResponse(response =>
    response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/fabrication-quotes')
  );
  await page.getByRole('button', { name: 'Save draft', exact: true }).click();
  const response = await savedResponse;
  expect(response.ok()).toBeTruthy();
  const saved = await response.json();
  expect(saved.title).toBe(title);
  expect(saved.status).toBe('draft');
  expect(saved.plan.parts).toEqual([expect.objectContaining({ name: partName })]);
  expect(saved.plan.roots).toEqual([{ part_id: saved.plan.parts[0].id, quantity: '17' }]);
  await expect(page).toHaveURL(new RegExp(`/fabrication-quotes\\?id=${saved.id}$`));
  const savedUrl = page.url();

  await page.reload();
  await expect(page.getByLabel('Quote title', { exact: true })).toHaveValue(title);
  await expect(page.getByLabel('Part number / description', { exact: true })).toHaveValue(partName);
  await expect(page.getByLabel('Customer quantity', { exact: true })).toHaveValue('17');

  await page.goto('/quotes');
  await page.getByRole('button', { name: 'New Quote', exact: true }).first().click();
  await expect(page.getByLabel('Quote title', { exact: true })).toHaveValue('');
  const library = page.getByRole('complementary', { name: 'Quote library' });
  await library.getByLabel('Find a quote', { exact: true }).fill(title);
  await library.getByRole('button', { name: new RegExp(title) }).click();
  await expect(page).toHaveURL(savedUrl);
  await expect(page.getByLabel('Quote title', { exact: true })).toHaveValue(title);
  await expect(page.getByLabel('Part number / description', { exact: true })).toHaveValue(partName);
  await expect(page.getByLabel('Customer quantity', { exact: true })).toHaveValue('17');
});

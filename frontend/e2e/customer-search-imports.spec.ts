import { test, expect, TEST_USERS } from './fixtures';

test('customer editing preserves shipping and account requirements after reload', async ({
  adminPage: page,
  request,
}, testInfo) => {
  const api = process.env.E2E_API_URL || 'http://127.0.0.1:8000/api/v1';
  const login = await request.post(`${api}/auth/login`, {
    form: { username: TEST_USERS.admin.email, password: TEST_USERS.admin.secret },
  });
  expect(login.ok()).toBeTruthy();
  const headers = {
    Authorization: `Bearer ${(await login.json()).access_token}`,
    'X-Requested-With': 'XMLHttpRequest',
  };
  const name = `QA-FULL-${Date.now()}`;
  const fields = {
    name,
    country: 'Canada',
    address_line2: 'Suite 12',
    phone: '416-555-0101',
    ship_to_name: 'Receiving Team',
    ship_address_line1: '12 Dock Street',
    ship_address_line2: 'Door B',
    ship_city: 'Toronto',
    ship_state: 'ON',
    ship_zip_code: 'M1M 1M1',
    ship_country: 'Canada',
    special_requirements: 'Preserve drawing revision markings',
    notes: 'Call receiving before delivery',
  };
  const created = await request.post(`${api}/customers/`, { headers, data: fields });
  expect(created.ok(), await created.text()).toBeTruthy();
  const id = (await created.json()).id;
  await page.goto(`/customers?id=${id}`);
  await page.getByRole('button', { name: 'Edit Customer', exact: true }).click();
  const form = page
    .getByRole('dialog')
    .filter({ has: page.getByRole('heading', { name: 'Edit Customer', exact: true }) });
  await expect(form.getByLabel('Shipping Address Line 2', { exact: true })).toHaveValue('Door B');
  await expect(form.getByLabel('Country', { exact: true })).toHaveValue('Canada');
  await expect(form.getByLabel('Special Requirements', { exact: true })).toHaveValue(fields.special_requirements);
  await form.getByLabel('Phone', { exact: true }).fill('416-555-0202');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(form).toHaveCSS('opacity', '1');
  await page.screenshot({ path: testInfo.outputPath('customer-preserved-mobile.png'), animations: 'disabled' });
  const update = page.waitForRequest(req => req.method() === 'PUT' && req.url().endsWith(`/customers/${id}`));
  await form.getByRole('button', { name: 'Update', exact: true }).click();
  expect((await update).postDataJSON()).toEqual({ phone: '416-555-0202' });
  await expect(form).toHaveCount(0);
  await page.goto(`/customers?id=${id}`);
  await page.getByRole('button', { name: 'Edit Customer', exact: true }).click();
  await expect(form.getByLabel('Shipping Address Line 2', { exact: true })).toHaveValue('Door B');
  await expect(form.getByLabel('Notes', { exact: true })).toHaveValue(fields.notes);
  const persisted = await request.get(`${api}/customers/${id}`, { headers });
  expect(await persisted.json()).toMatchObject({ ...fields, phone: '416-555-0202' });
});

test('saved import receipt corrects only failed rows and search opens the created customer', async ({
  adminPage: page,
}, testInfo) => {
  const stamp = `QA-RECOVERY-${Date.now()}`;
  await page.goto('/import-center?type=customers');
  await page
    .getByLabel('Import file', { exact: true })
    .setInputFiles({
      name: `${stamp}.csv`,
      mimeType: 'text/csv',
      buffer: Buffer.from(
        `name,code,email\n${stamp} Ready,${stamp}-1,ready@example.com\n${stamp} Correct,${stamp}-2,invalid-address\n`
      ),
    });
  await page.getByRole('button', { name: 'Validate file (dry run)', exact: true }).click();
  await expect(
    page.getByText('2 rows · 1 ready · 0 records created · 1 rows need correction', { exact: true })
  ).toBeVisible();
  const receiptURL = page.url();
  await expect(page.getByRole('button', { name: 'Commit ready rows', exact: true })).toBeDisabled();
  await page.getByRole('checkbox', { name: /I reviewed the ready rows/ }).check();
  await page.getByRole('button', { name: 'Commit ready rows', exact: true }).click();
  await expect(
    page.getByText('2 rows · 0 ready · 1 records created · 1 rows need correction', { exact: true })
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText('2 rows · 0 ready · 1 records created · 1 rows need correction', { exact: true })
  ).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download failed rows CSV', exact: true }).click(),
  ]);
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const csv = Buffer.concat(chunks).toString('utf8');
  expect(csv).toContain('_import_row_id');
  expect(csv).not.toContain(`${stamp} Ready`);
  await page
    .getByLabel('Corrected failed-row file', { exact: true })
    .setInputFiles({
      name: 'corrected.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(csv.replace('invalid-address', 'corrected@example.com')),
    });
  await page.getByRole('button', { name: 'Validate corrections', exact: true }).click();
  await expect(
    page.getByText('2 rows · 1 ready · 1 records created · 0 rows need correction', { exact: true })
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('import-correction-mobile.png'), animations: 'disabled' });
  await page.getByRole('checkbox', { name: /I reviewed the ready rows/ }).check();
  await page.getByRole('button', { name: 'Commit ready rows', exact: true }).click();
  await expect(
    page.getByText('2 rows · 0 ready · 2 records created · 0 rows need correction', { exact: true })
  ).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('region', { name: 'Import receipt', exact: true }).screenshot({ path: testInfo.outputPath('import-complete-desktop.png'), animations: 'disabled' });
  await page.goto(`/search?q=${stamp}`);
  await expect(page.getByText('1–2 of 2 matches', { exact: true })).toBeVisible();
  const filtered = page.waitForResponse(response => response.url().includes('/search/') && new URL(response.url()).searchParams.get('types') === 'customer');
  await page.getByLabel('Record type', { exact: true }).selectOption('customer');
  await filtered;
  await expect(page.getByText('Searching records…', { exact: true })).toHaveCount(0);
  await expect(page.getByText('1–2 of 2 matches', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('search-customer-results-desktop.png'), animations: 'disabled' });
  await page
    .getByRole('link')
    .filter({ has: page.getByText(`${stamp} Correct`, { exact: true }) })
    .click();
  await expect(page.getByRole('heading', { name: `${stamp} Correct`, exact: true })).toBeVisible();
  await page.goto(receiptURL);
  await expect(
    page.getByText('2 rows · 0 ready · 2 records created · 0 rows need correction', { exact: true })
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Commit ready rows', exact: true })).toBeDisabled();
});

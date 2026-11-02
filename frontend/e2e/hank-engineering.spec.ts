import { expect, test, type BrowserContext, type Page } from '@playwright/test';

// Synthetic local evidence only. No production data or external calls.
function drawingPdf(extra = ''): Buffer {
  const lines = ['DRAWING NUMBER: TEST-ASM', 'PART NUMBER: TEST-ASM', 'REV: B', 'TITLE: Test assembly', 'PART NUMBER | QTY | UOM | DESCRIPTION', 'TEST-DETAIL | 2 | each | Test detail', 'DEBURR ALL EDGES', extra].filter(Boolean);
  const stream = `BT /F1 12 Tf 40 740 Td ${lines.map((line, i) => `${i ? '0 -24 Td ' : ''}(${line}) Tj`).join('\n')} ET`;
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`];
  let pdf = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(pdf);
}
async function workspace(context: BrowserContext, baseURL: string) {
  const user = { id: 7, company_id: 1, email: 'manager@example.test', role: 'manager', employee_id: 'SYN-007', first_name: 'Test', last_name: 'Engineer', is_active: true, is_superuser: false };
  await context.addInitScript(saved => {
    sessionStorage.setItem('token', `test.${btoa(JSON.stringify({ sub: '7', cid: 1, type: 'access', exp: 9999999999 }))}.test`);
    sessionStorage.setItem('user', JSON.stringify(saved));
    localStorage.setItem('werco-completed-tours:1:7', '["getting-started"]');
  }, user);
  const parts: Record<string, any>[] = []; const boms: Record<string, any>[] = []; const routings: Record<string, any>[] = []; const orders: Record<string, any>[] = [];
  const mutations: string[] = []; const unexpected: string[] = [];
  const center = { id: 1, code: 'LASER-01', name: 'Laser', is_active: true, work_center_type: 'laser', hourly_rate: 0 };
  await context.route('**/*', async route => {
    const req = route.request(); const url = new URL(req.url()); const path = url.pathname.replace(/^\/api\/v1/, '');
    if (!url.pathname.startsWith('/api/v1/')) { if (url.origin !== new URL(baseURL).origin) return route.abort(); return route.continue(); }
    const send = (json: unknown) => route.fulfill({ json });
    if (req.method() !== 'GET') mutations.push(`${req.method()} ${path}`);
    if (path === '/users/me') return send(user);
    if (path === '/companies/me') return send({ id: 1, name: 'Synthetic company' });
    if (path.endsWith('/unread-count') || path.endsWith('/pending-approvals/summary')) return send({ count: 0 });
    if (path === '/work-centers/') return send([center]);
    if (path === '/parts/' && req.method() === 'POST') { const saved = { id: parts.length + 1, is_active: true, version: 1, ...req.postDataJSON() }; parts.push(saved); return send(saved); }
    if (path === '/parts/') return send(parts);
    if (/^\/parts\/\d+$/.test(path)) return send(parts.find(p => p.id === Number(path.split('/')[2])));
    if (path === '/bom/' && req.method() === 'POST') {
      const body = req.postDataJSON(); const saved = { id: 10, is_active: true, status: 'draft', ...body, part: parts.find(p => p.id === body.part_id), items: body.items.map((line: object, i: number) => ({ id: i + 1, is_optional: false, is_alternate: false, scrap_factor: 0, ...line })) }; boms.push(saved); return send(saved);
    }
    if (path === '/bom/') return send(boms);
    if (path === '/bom/10/release') { boms[0].status = 'released'; return send(boms[0]); }
    if (path === '/bom/10') return send(boms[0]);
    if (path === '/routing/' && req.method() === 'POST') { const body = req.postDataJSON(); const saved = { id: 20, is_active: true, status: 'draft', operations: [], total_setup_hours: 0, total_run_hours_per_unit: 0, total_labor_cost: 0, total_overhead_cost: 0, ...body, part: parts.find(p => p.id === body.part_id) }; routings.push(saved); return send(saved); }
    if (path === '/routing/') return send(routings.filter(r => !url.searchParams.has('part_id') || r.part_id === Number(url.searchParams.get('part_id'))));
    if (path === '/routing/20/operations') { const op = { id: 30, is_active: true, is_outside_operation: false, ...req.postDataJSON(), work_center: center }; routings[0].operations.push(op); return send(op); }
    if (path === '/routing/20/release') { routings[0].status = 'released'; return send(routings[0]); }
    if (path === '/routing/20') return send(routings[0]);
    if (path === '/work-orders/' && req.method() === 'POST') {
      const body = req.postDataJSON(); const saved = { id: 40, work_order_number: 'WO-SYNTHETIC-0040', status: 'draft', ...body, operations: [{ sequence: 10, name: 'TEST-DETAIL - Cut and deburr', work_center_id: 1, component_part_id: parts.find(p => p.part_number === 'TEST-DETAIL')!.id, component_quantity: body.quantity_ordered * 2, run_instructions: routings[0].operations[0].work_instructions, requires_inspection: false, setup_time_hours: 0.2, run_time_hours: body.quantity_ordered * 0.05 }] }; orders.push(saved); return send(saved);
    }
    if (path === '/work-orders/') return send(orders);
    if (path === '/work-orders/40') return send(orders[0]);
    if (req.method() !== 'GET') unexpected.push(`${req.method()} ${path}`);
    return send([]);
  });
  return { parts, boms, routings, orders, mutations, unexpected };
}
async function review(page: Page) {
  await page.getByLabel('Fabrication, assembly or detail PDF (up to 25 pages)').setInputFiles({ name: 'synthetic.pdf', mimeType: 'application/pdf', buffer: drawingPdf() });
  await expect(page.getByLabel('Drawing number', { exact: true })).toHaveValue('TEST-ASM');
  await page.getByLabel(/^I inspected every page/).check();
  const parts = page.getByRole('region', { name: 'Reviewed parts' }).locator('fieldset');
  for (let i = 0; i < 2; i++) {
    const part = parts.nth(i); await part.getByLabel('Revision', { exact: true }).fill('B');
    const pn = await part.getByLabel('Part number', { exact: true }).inputValue();
    await part.getByLabel('Make / buy type').selectOption(pn === 'TEST-ASM' ? 'assembly' : 'manufactured');
    await part.getByLabel('Stocking unit').selectOption('each');
    await part.getByLabel(/^Confirmed against source/).check();
  }
  await page.getByRole('region', { name: 'Reviewed components' }).getByLabel(/^Confirmed against source/).check();
  await page.getByRole('region', { name: 'Manufacturing requirements' }).getByLabel(/^Confirmed against source/).check();
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  const operation = page.getByRole('region', { name: 'Reviewed routing' });
  await operation.getByLabel('Operation part').selectOption('TEST-DETAIL');
  await operation.getByLabel('Operation description').fill('Cut and deburr');
  await operation.getByLabel('Work center', { exact: true }).selectOption('1');
  await operation.getByLabel('Approved setup minutes').fill('12'); await operation.getByLabel('Approved run minutes per owner unit').fill('3');
  await operation.getByLabel('Work instructions / manufacturing requirements').fill('DEBURR ALL EDGES');
  await operation.getByLabel('Drawing evidence / approval for supplied values').fill('Page 1. Jon approved sequence, center and time standards.');
  await operation.getByLabel(/^Confirmed against source/).check();
  await page.getByLabel(/^Review notes:/).fill('All sheets checked. No unresolved assumptions. Jon authorized entry.');
  await page.getByLabel('A new Draft Work Order was explicitly requested').check();
  await page.getByLabel('Authorized job quantity').fill('4');
  await page.getByLabel('Job quantity and WO authorization source').fill('Jon requested four assemblies and a Draft WO.');
  await page.getByLabel(/^All sheets, values and omissions reviewed/).check();
}
test.beforeEach(async ({ baseURL }) => { test.skip(!baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname), 'Synthetic local tests only'); });

test('PDF review -> Parts -> BOM -> Routing -> authorized Engineering release -> verified Draft WO', async ({ page, context, baseURL }) => {
  const state = await workspace(context, baseURL!); await page.goto('/hank-engineering'); await review(page);
  expect(state.mutations).toEqual([]);
  await page.getByRole('button', { name: 'Match existing records', exact: true }).click();
  await expect(page.getByText('TEST-ASM: missing; will create')).toBeVisible();
  await page.getByRole('button', { name: 'Save missing Parts', exact: true }).click();
  await expect.poll(() => state.parts.length).toBe(2); await expect(page.getByRole('button', { name: 'Save Draft BOM', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Save Draft BOM', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Review BOM', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Save Draft Routing', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Review Routing', exact: true })).toBeVisible();
  expect(state.orders).toHaveLength(0);
  for (const link of ['Review BOM', 'Review Routing']) {
    const popupPromise = page.waitForEvent('popup'); await page.getByRole('link', { name: link, exact: true }).click(); const popup = await popupPromise;
    await popup.getByRole('button', { name: 'Release', exact: true }).click();
    await expect.poll(() => link === 'Review BOM' ? state.boms[0].status : state.routings[0].status).toBe('released'); await popup.close();
  }
  await page.getByRole('button', { name: 'Match existing records', exact: true }).click();
  await page.getByRole('button', { name: 'Create requested Draft Work Order', exact: true }).click();
  await expect(page.getByText('Draft Work Order saved and read back. No production release was requested.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'WO-SYNTHETIC-0040' })).toBeVisible();
  expect(state.boms[0].items[0].quantity).toBe(2); expect(state.orders[0].quantity_ordered).toBe(4); expect(state.orders[0].operations[0].component_quantity).toBe(8);
  expect(state.orders[0].status).toBe('draft'); expect(state.mutations.some(path => /work-orders.*release/.test(path))).toBe(false); expect(state.unexpected).toEqual([]);
  await expect(page.getByRole('button', { name: 'Create requested Draft Work Order', exact: true })).toBeDisabled();
});
test('restriction-marked PDF cannot be entered, exported or sent to a model', async ({ page, context, baseURL }) => {
  const state = await workspace(context, baseURL!); await page.goto('/hank-engineering');
  await page.getByLabel('Fabrication, assembly or detail PDF (up to 25 pages)').setInputFiles({ name: 'restricted.pdf', mimeType: 'application/pdf', buffer: drawingPdf('ITAR') });
  await expect(page.getByRole('alert')).toContainText('Restriction marking found');
  await expect(page.getByRole('button', { name: 'Match existing records', exact: true })).toHaveCount(0); expect(state.mutations).toEqual([]);
});

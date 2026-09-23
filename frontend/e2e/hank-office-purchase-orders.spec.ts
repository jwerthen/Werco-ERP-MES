import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import type { HankIntakeFile, HankIntakePurchaseOrderDraft } from '../src/types/hankIntake';

// Synthetic local-only flow: blocks external origins and fulfills every API request.
const user = {
  id: 9001,
  company_id: 4,
  email: 'purchasing@example.test',
  role: 'manager',
  employee_id: 'QA-001',
  first_name: 'Alex',
  last_name: 'Buyer',
  is_active: true,
  is_superuser: false,
};
const file: HankIntakeFile = {
  id: 51,
  batch_id: 20,
  company_id: 4,
  filename: 'our-purchase-order.docx',
  file_size: 200,
  content_sha256: 'a'.repeat(64),
  page_count: null,
  source_format: 'docx',
  source_labels: ['Paragraphs 1–3; Table 1, rows 1–2'],
  status: 'awaiting_review',
  version: 3,
  source_url: '/api/v1/hank/intake/files/51/source',
  plan: null,
  result: null,
  error_message: null,
  created_at: '2026-09-23T14:00:00Z',
  updated_at: '2026-09-23T14:01:00Z',
  completed_at: null,
  analysis: {
    source_format: 'docx',
    source_labels: ['Paragraphs 1–3; Table 1, rows 1–2'],
    classification: 'purchase_order',
    confidence: 'high',
    summary: 'PO-WORD-009 orders aluminum sheets from Local Metals.',
    evidence: [{ page: 1, locator: 'Paragraph 1', excerpt: 'PO-WORD-009' }],
    fields: [],
    lines: [
      {
        description: '5052 aluminum sheets',
        part_number: 'AL-5052',
        quantity: '12',
        unit_price: '24.50',
        unit_of_measure: 'EA',
        lot_number: null,
        heat_number: null,
        confidence: 'high',
        evidence: [{ page: 1, locator: 'Table 1, row 2', excerpt: 'AL-5052 12 EA 24.50' }],
      },
    ],
    warnings: [],
    matches: [],
    duplicate_file_ids: [],
    duplicate_document_ids: [],
  },
};
const draft: HankIntakePurchaseOrderDraft = {
  file_id: 51,
  file_version: 3,
  company_id: 4,
  filename: file.filename,
  po_number: 'PO-WORD-009',
  order_date: '2026-09-23',
  required_date: '2026-10-05',
  vendor_id: 8,
  vendors: [{ id: 8, code: 'LM', name: 'Local Metals', reason: 'Printed supplier name matches' }],
  lines: [
    {
      ...file.analysis!.lines[0],
      source_line_index: 0,
      part_id: 7,
      candidates: [{ id: 7, part_number: 'AL-5052', name: '5052 aluminum sheets', unit_of_measure: 'EA' }],
      quantity_ordered: 12,
      unit_price_amount: 24.5,
      warnings: [],
    },
  ],
  warnings: [],
  has_duplicates: false,
  blocked_reason: null,
  can_ready_for_receiving: true,
  existing_purchase_orders: [],
};
for (const viewport of [
  { name: 'desktop', width: 1440, height: 1000 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`${viewport.name}: Word PO is reviewed and issued to Receiving with no inventory receipt`, async ({
    page,
    baseURL,
  }) => {
    test.skip(
      !baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname),
      'Synthetic flow requires local frontend.'
    );
    const origin = new URL(baseURL!).origin;
    await page.setViewportSize(viewport);
    await page.addInitScript(savedUser => {
      sessionStorage.setItem(
        'token',
        `header.${btoa(JSON.stringify({ sub: String(savedUser.id), cid: 4, ro: false, type: 'access' }))}.sig`
      );
      sessionStorage.setItem('user', JSON.stringify(savedUser));
      localStorage.setItem('werco-completed-tours:4:9001', '["getting-started"]');
    }, user);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/*', socket => socket.close());
    let uploaded = false;
    let input: Record<string, unknown> = {};
    let executed = false;
    const writes: string[] = [];
    const savedTask = () => ({
      id: 41,
      company_id: 4,
      kind: 'draft_purchase_order',
      title: 'Import PO-WORD-009',
      status: executed ? 'completed' : 'awaiting_review',
      version: executed ? 2 : 1,
      input,
      preview: {
        summary: 'Review PO-WORD-009 before creating it.',
        changes: ['Create an issued PO and add it to Receiving.'],
        warnings: [],
        references: [],
      },
      result: executed
        ? {
            summary: 'Created PO-WORD-009 and added it to Receiving.',
            warnings: [],
            references: [
              { type: 'purchase_order', id: 91, label: 'Open Receiving for PO-WORD-009', url: '/receiving?po=91' },
            ],
          }
        : null,
      error_message: null,
      created_at: file.created_at,
      updated_at: file.updated_at,
      completed_at: executed ? file.updated_at : null,
    });
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (!url.pathname.startsWith('/api/v1/')) {
        if (url.origin !== origin) return route.abort();
        return route.continue();
      }
      const endpoint = url.pathname.slice('/api/v1'.length);
      if (request.method() === 'POST') writes.push(endpoint);
      let json: unknown = {};
      if (endpoint === '/users/me') json = user;
      else if (endpoint === '/notifications/catalog') json = [];
      else if (endpoint === '/users/me/notification-preferences')
        json = { preferences: {}, sms_egress_enabled: false, sms_configured: false, phone: null };
      else if (endpoint === '/companies/me') json = { id: 4, name: 'Local Test Shop', slug: 'test' };
      else if (endpoint.includes('unread-count') || endpoint.includes('pending-approvals/summary')) json = { count: 0 };
      else if (endpoint === '/hank/capabilities')
        json = {
          company_id: 4,
          can_write: true,
          can_watch: true,
          allowed_kinds: ['draft_purchase_order', 'receive_delivery'],
        };
      else if (endpoint === '/hank/intake') {
        const batch = {
          id: 20,
          company_id: 4,
          request_key: 'synthetic-office',
          created_at: file.created_at,
          files: [file],
        };
        if (request.method() === 'POST') {
          uploaded = true;
          expect(request.postData()).toContain(file.filename);
          json = batch;
        } else json = { batches: uploaded ? [batch] : [], has_more: false, next_before_id: null };
      } else if (endpoint === '/hank/intake/files/51') json = file;
      else if (endpoint === '/hank/intake/files/51/purchase-order-draft') json = draft;
      else if (endpoint === '/hank/intake/files/51/source')
        return route.fulfill({
          contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          body: 'Synthetic Office source',
        });
      else if (endpoint === '/hank/intake/files/51/source-preview')
        json = {
          format: 'docx',
          units: ['PO-WORD-009\nVendor: Local Metals\nTable 1, row 2: AL-5052 12 EA 24.50'],
          labels: file.source_labels,
          warnings: [],
        };
      else if (endpoint === '/documents/types/list') json = [{ value: 'other', label: 'Other' }];
      else if (endpoint === '/purchasing/vendors') json = [{ id: 8, code: 'LM', name: 'Local Metals' }];
      else if (endpoint === '/parts/')
        json = [{ id: 7, part_number: 'AL-5052', name: '5052 aluminum sheets', unit_of_measure: 'EA' }];
      else if (endpoint === '/work-orders/') json = [];
      else if (endpoint === '/hank/tasks' && request.method() === 'POST') {
        const body = request.postDataJSON();
        expect(body.kind).toBe('draft_purchase_order');
        input = body.input;
        json = savedTask();
      } else if (endpoint === '/hank/tasks/41/execute') {
        executed = true;
        json = savedTask();
      } else if (endpoint === '/hank/work-queue') json = { checked_at: file.created_at, items: [], truncated: false };
      return route.fulfill({ json });
    });
    await page.goto('/settings');
    await page.getByRole('button', { name: 'Toggle Hank' }).click();
    const panel = page.getByRole('dialog', { name: 'Hank', exact: true });
    await panel.getByRole('button', { name: 'Upload documents' }).click();
    await panel.getByLabel('Documents to review').setInputFiles({
      name: file.filename,
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      buffer: Buffer.from('synthetic Office bytes'),
    });
    await panel.getByRole('button', { name: 'Upload for review' }).click();
    await panel.getByRole('button', { name: /our-purchase-order.docx · awaiting review/ }).click();
    await panel.getByRole('button', { name: 'Create purchase order', exact: true }).click();
    await expect(panel.getByLabel('Printed PO number')).toHaveValue('PO-WORD-009');
    await expect(panel.getByRole('combobox', { name: /Vendor/ })).toHaveValue('LM Local Metals');
    await expect(panel.getByRole('combobox', { name: /Part for line 1/ })).toHaveValue(
      'AL-5052 — 5052 aluminum sheets'
    );
    await expect(panel.getByText(/Unable to load choices/)).toHaveCount(0);
    await expect(panel.getByLabel('Quantity for line 1')).toHaveValue('12');
    await expect(panel.getByLabel('Stocking unit for line 1')).toHaveValue('EA');
    await expect(panel.getByLabel('Add to Receiving')).toBeChecked();
    await expect(panel.getByText('Table 1, row 2: AL-5052 12 EA 24.50', { exact: true })).toBeVisible();
    await panel.getByRole('button', { name: `Load source: ${file.filename}` }).click();
    await expect(panel.getByText('Paragraphs 1–3; Table 1, rows 1–2', { exact: true })).toBeVisible();
    await expect(panel.getByRole('link', { name: 'Download source' })).toHaveAttribute('download', file.filename);
    await expect(panel.getByRole('link', { name: /^Page/ })).toHaveCount(0);
    const outDir = path.resolve('.browser-harness/hank-office');
    await mkdir(outDir, { recursive: true });
    await panel.screenshot({ path: path.join(outDir, `po-source-${viewport.name}.png`) });
    await panel.getByRole('button', { name: 'Prepare purchase order for review' }).scrollIntoViewIfNeeded();
    await panel.screenshot({ path: path.join(outDir, `po-form-${viewport.name}.png`) });
    const size = await panel.evaluate(node => ({
      client: node.clientWidth,
      scroll: node.scrollWidth,
      right: node.getBoundingClientRect().right,
      width: window.innerWidth,
    }));
    expect(size.scroll).toBeLessThanOrEqual(size.client);
    expect(size.right).toBeLessThanOrEqual(size.width);
    await panel.getByRole('button', { name: 'Prepare purchase order for review' }).click();
    await expect(panel.getByText('Review PO-WORD-009 before creating it.')).toBeVisible();
    expect(executed).toBe(false);
    expect(input).toMatchObject({
      source_intake_file_id: 51,
      source_intake_version: 3,
      po_number: 'PO-WORD-009',
      ready_for_receiving: true,
      lines: [{ source_line_index: 0, part_id: 7, quantity_ordered: 12, unit_price: 24.5, unit_of_measure: 'EA' }],
    });
    await panel.getByRole('button', { name: 'Create purchase order and add to Receiving' }).click();
    await expect(panel.getByRole('link', { name: 'Open Receiving for PO-WORD-009' })).toHaveAttribute(
      'href',
      '/receiving?po=91'
    );
    await panel.screenshot({ path: path.join(outDir, `po-created-${viewport.name}.png`) });
    expect(writes).toEqual(['/hank/intake', '/hank/tasks', '/hank/tasks/41/execute']);
    expect(errors).toEqual([]);
  });
}

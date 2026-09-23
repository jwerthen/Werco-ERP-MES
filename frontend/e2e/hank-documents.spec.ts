import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import type { HankIntakeFile, HankIntakeReceivingDraft } from '../src/types/hankIntake';

// Synthetic local smoke: every API call is fulfilled here; all remote origins are blocked.
const user = {
  id: 9001,
  company_id: 4,
  email: 'receiving@example.test',
  role: 'manager',
  employee_id: 'QA-001',
  first_name: 'Alex',
  last_name: 'Receiver',
  is_active: true,
  is_superuser: false,
};
const file: HankIntakeFile = {
  id: 51,
  batch_id: 20,
  company_id: 4,
  filename: 'material-delivery.pdf',
  file_size: 200,
  content_sha256: 'a'.repeat(64),
  page_count: 1,
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
    classification: 'packing_slip',
    confidence: 'high',
    summary: '12 aluminum sheets delivered against PO-0011.',
    evidence: [{ page: 1, excerpt: 'PO-0011: AL-5052, 12 EA' }],
    fields: [],
    lines: [
      {
        description: '5052 aluminum sheets',
        part_number: 'AL-5052',
        quantity: '12',
        unit_of_measure: 'EA',
        unit_price: null,
        lot_number: 'LOT-923',
        heat_number: 'HEAT-817',
        confidence: 'high',
        evidence: [{ page: 1, excerpt: 'AL-5052, 12 EA, LOT-923, HEAT-817' }],
      },
    ],
    warnings: [],
    matches: [],
    duplicate_file_ids: [],
    duplicate_document_ids: [],
  },
};
const draft: HankIntakeReceivingDraft = {
  file_id: 51,
  file_version: 3,
  company_id: 4,
  filename: file.filename,
  purchase_order_id: 11,
  purchase_orders: [{ id: 11, po_number: 'PO-0011', vendor_name: 'Local Metals', reason: 'Exact printed PO number' }],
  packing_slip_number: 'PS-923',
  warnings: [],
  has_duplicates: false,
  requires_duplicate_acknowledgement: false,
  lines: [
    {
      ...file.analysis!.lines[0],
      unit_of_measure: 'EA',
      source_line_index: 0,
      po_line_id: 31,
      quantity_received: 12,
      candidates: [
        {
          po_line_id: 31,
          line_number: 1,
          part_id: 7,
          part_number: 'AL-5052',
          description: '5052 aluminum sheets',
          unit_of_measure: 'EA',
          quantity_remaining: 20,
        },
      ],
      warnings: [],
    },
  ],
};

for (const viewport of [
  { name: 'desktop', width: 1440, height: 1000 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`${viewport.name}: PDFs attach to chat and prepare reviewed material receiving`, async ({ page, baseURL }) => {
    test.skip(
      !baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname),
      'Mocked Hank smoke only runs on a local frontend.'
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
    const requests: Array<{ url: string; data: unknown }> = [];
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (!url.pathname.startsWith('/api/v1/')) {
        if (url.origin !== origin) return route.abort();
        return route.continue();
      }
      const endpoint = url.pathname.slice('/api/v1'.length);
      let json: unknown = {};
      if (endpoint === '/users/me') json = user;
      else if (endpoint === '/notifications/catalog') json = [];
      else if (endpoint === '/users/me/notification-preferences')
        json = { preferences: {}, sms_egress_enabled: false, sms_configured: false, phone: null };
      else if (endpoint === '/companies/me') json = { id: 4, name: 'Local Test Shop', slug: 'test' };
      else if (endpoint.includes('unread-count') || endpoint.includes('pending-approvals/summary')) json = { count: 0 };
      else if (endpoint === '/hank/capabilities')
        json = { company_id: 4, can_write: true, can_watch: true, allowed_kinds: ['receive_delivery'] };
      else if (endpoint === '/hank/intake') {
        const batch = { id: 20, company_id: 4, request_key: 'local-smoke', created_at: file.created_at, files: [file] };
        if (request.method() === 'POST') {
          uploaded = true;
          json = batch;
        } else json = { batches: uploaded ? [batch] : [], has_more: false, next_before_id: null };
      } else if (endpoint === '/hank/intake/files/51') json = file;
      else if (endpoint === '/hank/intake/files/51/receiving-draft') json = draft;
      else if (endpoint === '/hank/intake/files/51/source')
        return route.fulfill({
          contentType: 'application/pdf',
          body: '%PDF-1.4\n% Local synthetic source fixture\n%%EOF',
        });
      else if (endpoint === '/documents/types/list') json = [{ value: 'other', label: 'Other' }];
      else if (endpoint === '/purchasing/purchase-orders')
        json = [{ id: 11, po_number: 'PO-0011', vendor_name: 'Local Metals' }];
      else if (endpoint === '/receiving/po/11')
        json = {
          po_id: 11,
          po_number: 'PO-0011',
          lines: [
            {
              line_id: 31,
              line_number: 1,
              part_number: 'AL-5052',
              part_name: '5052 aluminum sheets',
              quantity_remaining: 20,
            },
          ],
        };
      else if (endpoint === '/hank/work-queue') json = { checked_at: file.created_at, items: [], truncated: false };
      else if (endpoint === '/copilot/chat') {
        requests.push({ url: endpoint, data: request.postDataJSON() });
        return route.fulfill({
          contentType: 'text/event-stream',
          body: `data: ${JSON.stringify({ type: 'final', answer: 'The PDF lists 12 aluminum sheets for PO-0011.', references: [], tool_trace: [], interaction_id: 1, rounds: 1, truncated: false })}\n\n`,
        });
      } else if (endpoint === '/hank/tasks' && request.method() === 'POST') {
        const body = request.postDataJSON();
        requests.push({ url: endpoint, data: body });
        json = {
          id: 41,
          company_id: 4,
          kind: 'receive_delivery',
          title: 'Receive PO-0011',
          status: 'awaiting_review',
          version: 1,
          input: body.input,
          preview: {
            summary: 'Review 12 EA of AL-5052 before posting.',
            changes: ['Receive 12 EA of AL-5052 with inspection hold.'],
            warnings: [],
            references: [],
          },
          result: null,
          error_message: null,
          created_at: file.created_at,
          updated_at: file.updated_at,
          completed_at: null,
        };
      } else if (/\/(parts|vendors|work-orders)\/?$/.test(endpoint)) json = [];
      return route.fulfill({ json });
    });
    await page.goto('/settings');
    await page.getByRole('button', { name: 'Toggle Hank' }).click();
    const panel = page.getByRole('dialog', { name: 'Hank', exact: true });
    await panel.getByRole('button', { name: 'Upload documents' }).click();
    await panel
      .getByLabel('Documents to review')
      .setInputFiles({ name: file.filename, mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\n%%EOF') });
    await panel.getByRole('button', { name: 'Upload for review' }).click();
    await panel.getByRole('button', { name: /material-delivery.pdf · awaiting review/ }).click();
    await expect(panel.getByRole('button', { name: 'Use in chat' })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Receive materials' })).toBeVisible();
    await panel.getByRole('button', { name: 'Load source: material-delivery.pdf' }).click();
    await expect(panel.getByRole('link', { name: 'Open material-delivery.pdf' })).toHaveAttribute('href', /^blob:/);
    await expect(panel.getByRole('link', { name: 'Page 1', exact: true })).toHaveAttribute('href', /#page=1$/);
    await panel.getByRole('button', { name: 'Use in chat' }).click();
    await expect(panel.getByLabel('Documents attached to chat')).toContainText(file.filename);
    const outDir = path.resolve('.browser-harness/hank');
    await mkdir(outDir, { recursive: true });
    await panel.screenshot({ path: path.join(outDir, `chat-${viewport.name}.png`) });
    await panel.getByLabel('Ask Hank', { exact: true }).fill('Check this delivery.');
    await panel.getByRole('button', { name: 'Send message' }).click();
    await expect(panel.getByText('The PDF lists 12 aluminum sheets for PO-0011.')).toBeVisible();
    expect(requests.find(request => request.url === '/copilot/chat')?.data).toMatchObject({
      intake_file_ids: [51],
    });
    await panel.getByRole('button', { name: 'Upload documents' }).click();
    await panel.getByRole('button', { name: /material-delivery.pdf · awaiting review/ }).click();
    await panel.getByRole('button', { name: 'Receive materials' }).click();
    await expect(panel.getByRole('heading', { name: 'Receive materials from material-delivery.pdf' })).toBeVisible();
    await expect(panel.getByText('PO line 1 · AL-5052 · Stocking unit EA · Remaining 20')).toBeVisible();
    await expect(panel.getByLabel('Delivered quantity, line 1')).toHaveValue('12');
    await expect(panel.getByLabel('Inspection required, line 1')).toHaveValue('');
    await expect(panel.getByLabel('lot number, line 1', { exact: true })).toHaveValue('LOT-923');
    await panel.screenshot({ path: path.join(outDir, `receiving-${viewport.name}.png`) });
    const size = await panel.evaluate(node => ({
      client: node.clientWidth,
      scroll: node.scrollWidth,
      right: node.getBoundingClientRect().right,
      width: window.innerWidth,
    }));
    expect(size.scroll).toBeLessThanOrEqual(size.client);
    expect(size.right).toBeLessThanOrEqual(size.width);
    await panel.getByLabel('Inspection required, line 1').selectOption('yes');
    await panel.getByRole('button', { name: 'Prepare for review' }).scrollIntoViewIfNeeded();
    await panel.screenshot({ path: path.join(outDir, `receiving-form-${viewport.name}.png`) });
    await panel.getByRole('button', { name: 'Prepare for review' }).click();
    await expect(panel.getByText('Review 12 EA of AL-5052 before posting.')).toBeVisible();
    expect(requests.find(request => request.url === '/hank/tasks')?.data).toMatchObject({
      input: {
        source_intake_file_id: 51,
        source_intake_version: 3,
        lines: [
          {
            po_line_id: 31,
            quantity_received: 12,
            requires_inspection: true,
            lot_number: 'LOT-923',
            heat_number: 'HEAT-817',
            packing_slip_number: 'PS-923',
          },
        ],
      },
    });
    await panel.getByRole('button', { name: 'Back to chat' }).click();
    await expect(panel.getByLabel('Ask Hank', { exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });
}

import { expect, Page, test } from '@playwright/test';
import type {
  ImprovementActivity,
  ImprovementDetail,
  ImprovementMetadata,
  ImprovementStatus,
} from '../src/types/continuousImprovement';

// Synthetic local browser acceptance. All API responses are intercepted, so
// these journeys never create shop records or require a seeded backend.
const manager = {
  id: 9001, company_id: 1, email: 'manager@example.test', role: 'manager',
  employee_id: 'QA-001', first_name: 'Alex', last_name: 'Rivera',
  is_active: true, is_superuser: false,
};
const statuses: ImprovementMetadata['statuses'] = [
  { value: 'new', label: 'New' },
  { value: 'under_review', label: 'Under review' },
  { value: 'approved', label: 'Approved' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'implemented', label: 'Implemented' },
  { value: 'on_hold', label: 'On hold' },
  { value: 'declined', label: 'Declined' },
];
test.use({ timezoneId: 'Asia/Tokyo' });

const submittedAt = '2026-09-28T14:15:00Z';
const reviewedAt = '2026-09-28T15:20:00Z';
const implementedAt = '2026-09-29T16:25:00Z';
const title = 'Prevent reversed bracket loading with an asymmetric locating pin';

function initialSuggestion(): ImprovementDetail {
  return {
    id: 42, company_id: 1, title,
    problem: 'A symmetric fixture lets a bracket be loaded backward.',
    proposed_solution: 'Add an offset locating pin so only the correct orientation fits.',
    expected_benefit: 'Prevent reversed assemblies and avoid repeat inspection.',
    category: 'poka_yoke', priority: 'high', area: 'Welding', status: 'new',
    owner_id: manager.id, owner_name: 'Alex Rivera', target_date: '2026-10-02',
    implementation_notes: null, created_by: manager.id, created_by_name: 'Alex Rivera',
    updated_by: manager.id, updated_by_name: 'Alex Rivera',
    created_at: submittedAt, updated_at: submittedAt, reviewed_at: null,
    implemented_at: null, version: 1,
    history: [{ id: 1, kind: 'submitted', actor_id: manager.id, actor_name: 'Alex Rivera',
      created_at: submittedAt, body: null, changes: { status: { from: null, to: 'new' } } }],
  };
}

async function mockWorkspace(page: Page, baseURL: string, role = 'manager', seed = false) {
  const user = { ...manager, role };
  const rows: ImprovementDetail[] = seed ? [initialSuggestion()] : [];
  const mutations: { method: string; path: string; body: Record<string, unknown> }[] = [];
  const unexpectedFeatureRequests: string[] = [];
  const metadata: ImprovementMetadata = {
    categories: [
      { value: 'poka_yoke', label: 'Poka-yoke / mistake proofing', description: 'Prevent mistakes at their source.' },
      { value: 'five_s', label: '5S / workplace organization' },
      { value: 'standard_work', label: 'Standard work' },
      { value: 'flow_layout', label: 'Flow / shop layout' },
      { value: 'quality', label: 'Quality / defect reduction' },
      { value: 'safety_ergonomics', label: 'Safety / ergonomics' },
      { value: 'setup_reduction', label: 'Setup reduction / SMED' },
      { value: 'equipment', label: 'Equipment / reliability' },
      { value: 'inventory', label: 'Inventory / material handling' },
      { value: 'other', label: 'Other improvement' },
    ],
    statuses,
    priorities: ['low', 'medium', 'high'].map(value => ({ value, label: value[0].toUpperCase() + value.slice(1) })),
    owners: [{ id: manager.id, name: 'Alex Rivera' }],
    can_manage: role === 'manager',
  };
  await page.addInitScript(savedUser => {
    sessionStorage.setItem('token', 'synthetic-local-test-token');
    sessionStorage.setItem('user', JSON.stringify(savedUser));
    localStorage.setItem(`werco-completed-tours:${savedUser.company_id}:${savedUser.id}`, '["getting-started"]');
  }, user);
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const endpoint = url.pathname;
    if (!endpoint.startsWith('/api/v1/')) {
      if (url.origin !== new URL(baseURL).origin) return route.abort();
      return route.continue();
    }
    if (endpoint === '/api/v1/users/me') return route.fulfill({ json: user });
    if (endpoint === '/api/v1/companies/me')
      return route.fulfill({ json: { id: 1, name: 'Werco Manufacturing', slug: 'werco' } });
    if (endpoint.endsWith('/unread-count') || endpoint.endsWith('/pending-approvals/summary'))
      return route.fulfill({ json: { count: 0 } });
    const prefix = '/api/v1/continuous-improvement';
    if (!endpoint.startsWith(prefix)) return route.fulfill({ json: {} });
    if (endpoint === `${prefix}/metadata`) return route.fulfill({ json: metadata });
    if (request.method() !== 'GET') {
      mutations.push({ method: request.method(), path: endpoint, body: request.postDataJSON() });
      if (!metadata.can_manage) return route.fulfill({ status: 403, json: { detail: 'Managers only' } });
    }
    if (endpoint === `${prefix}/` && request.method() === 'POST') {
      const row = { ...initialSuggestion(), ...request.postDataJSON() };
      rows.push(row);
      return route.fulfill({ status: 201, json: row });
    }
    if (endpoint === `${prefix}/` && request.method() === 'GET') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const matching = rows.filter(row =>
        (!q || `${row.title} ${row.problem} ${row.area}`.toLowerCase().includes(q)) &&
        (!url.searchParams.get('status') || row.status === url.searchParams.get('status')) &&
        (!url.searchParams.get('category') || row.category === url.searchParams.get('category')) &&
        (!url.searchParams.get('priority') || row.priority === url.searchParams.get('priority')) &&
        (!url.searchParams.get('owner_id') || row.owner_id === Number(url.searchParams.get('owner_id'))));
      const skip = Number(url.searchParams.get('skip') || 0);
      const limit = Number(url.searchParams.get('limit') || 25);
      return route.fulfill({ json: {
        items: matching.slice(skip, skip + limit), total: matching.length,
        status_counts: Object.fromEntries(statuses.map(status => [status.value, rows.filter(row => row.status === status.value).length])),
      } });
    }
    const match = endpoint.match(/\/continuous-improvement\/(\d+)(\/comments)?$/);
    if (match) {
      const row = rows.find(item => item.id === Number(match[1]));
      if (!row) return route.fulfill({ status: 404, json: { detail: 'Suggestion not found' } });
      if (request.method() === 'GET') return route.fulfill({ json: row });
      const body = request.postDataJSON();
      if (body.expected_version !== row.version)
        return route.fulfill({ status: 409, json: { detail: 'This suggestion changed. Refresh it before saving again.' } });
      const activity: ImprovementActivity = {
        id: row.history.length + 1, kind: match[2] ? 'comment' : 'updated',
        actor_id: user.id, actor_name: 'Alex Rivera', created_at: match[2] ? '2026-09-29T17:30:00Z' : reviewedAt,
        body: body.body || body.change_note || null, changes: {},
      };
      if (!match[2]) {
        const { expected_version: _version, change_note: _note, ...values } = body;
        for (const [key, value] of Object.entries(values)) {
          const previous = row[key as keyof ImprovementDetail];
          if (previous !== value) activity.changes[key] = { from: previous, to: value };
        }
        if (body.status && body.status !== row.status) {
          activity.kind = 'status_changed';
          row.reviewed_at ||= reviewedAt;
          if (body.status === 'implemented') {
            if (!body.implementation_notes?.trim())
              return route.fulfill({ status: 422, json: { detail: 'Implementation notes are required.' } });
            row.implemented_at = implementedAt;
            activity.created_at = implementedAt;
          }
        }
        Object.assign(row, values);
      }
      row.version += 1;
      row.updated_at = activity.created_at;
      row.history.push(activity);
      return route.fulfill({ json: row });
    }
    unexpectedFeatureRequests.push(`${request.method()} ${endpoint}`);
    return route.fulfill({ status: 404, json: { detail: 'Unexpected feature request' } });
  });
  return { rows, mutations, unexpectedFeatureRequests };
}

test.beforeEach(async ({ baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseURL).hostname),
    'Synthetic acceptance runs only against a local frontend.');
});

for (const viewport of [{ name: 'desktop', width: 1440, height: 1100 }, { name: 'mobile', width: 390, height: 844 }]) {
test(`${viewport.name}: manager submits, reviews, implements and retains timestamped history`, async ({ page, baseURL }, testInfo) => {
  const state = await mockWorkspace(page, baseURL!);
  await page.setViewportSize(viewport);
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto('/continuous-improvement');
  await expect(page.getByRole('heading', { level: 1, name: 'Continuous Improvement' })).toBeVisible();
  await page.getByRole('button', { name: 'New suggestion', exact: true }).click();
  const form = page.getByRole('dialog');
  await form.getByLabel(/^Title/).fill(title);
  await form.getByLabel(/^Category/).selectOption('poka_yoke');
  await form.getByLabel('Priority', { exact: true }).selectOption('high');
  await form.getByLabel('Shop area', { exact: true }).fill('Welding');
  await form.getByLabel('Owner', { exact: true }).selectOption(String(manager.id));
  await form.getByLabel('Target date', { exact: true }).fill('2026-10-02');
  await form.getByLabel(/^Problem \/ current condition/).fill(initialSuggestion().problem);
  await form.getByLabel(/^Proposed solution/).fill(initialSuggestion().proposed_solution);
  await form.getByLabel(/^Expected benefit/).fill(initialSuggestion().expected_benefit);
  await form.getByRole('button', { name: 'Submit suggestion', exact: true }).scrollIntoViewIfNeeded();
  await expect(form.getByRole('button', { name: 'Submit suggestion', exact: true })).toBeInViewport();
  if (viewport.name === 'mobile') await page.screenshot({ path: testInfo.outputPath('continuous-improvement-mobile-form.png'), animations: 'disabled' });
  await form.getByRole('button', { name: 'Submit suggestion', exact: true }).click();
  await expect(form.getByRole('button', { name: 'Submit suggestion', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  expect(state.mutations[0].body).toMatchObject({ title, category: 'poka_yoke', owner_id: manager.id });
  expect(state.mutations[0].body).not.toHaveProperty('created_at');
  await expect(page.getByRole('dialog').locator('time').filter({ hasText: /9:15(?::00)? AM CDT/ }).first()).toBeVisible();

  const changeStatus = async (status: ImprovementStatus, note: string, results?: string) => {
    await page.getByRole('button', { name: 'Edit suggestion', exact: true }).click();
    const editor = page.getByRole('dialog');
    await editor.getByLabel('Status', { exact: true }).selectOption(status);
    await editor.getByLabel(/^Change note/).fill(note);
    if (results) await editor.getByLabel(/^Implementation results/).fill(results);
    await editor.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect(editor.getByRole('button', { name: 'Save changes', exact: true })).toHaveCount(0);
  };
  await changeStatus('under_review', 'Review fixture fit with the welding lead.');
  await expect(page.getByRole('dialog').locator('time').filter({ hasText: /10:20(?::00)? AM CDT/ }).first()).toBeVisible();
  await changeStatus('implemented', 'Trial complete and standard work updated.', 'Installed the locating pin and verified ten correctly oriented assemblies.');
  await expect(page.getByRole('dialog').locator('time').filter({ hasText: /11:25(?::00)? AM CDT/ }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activity history', exact: true })).toBeVisible();
  await expect(page.getByText('Review fixture fit with the welding lead.', { exact: true })).toBeVisible();
  await expect(page.getByText('Trial complete and standard work updated.', { exact: true })).toBeVisible();
  await page.getByLabel('Add a comment', { exact: true }).fill('Check the fixture at the next weekly improvement review.');
  await page.getByRole('button', { name: 'Post comment', exact: true }).click();
  await expect(page.getByText('Check the fixture at the next weekly improvement review.', { exact: true })).toBeVisible();
  while (await page.getByRole('button', { name: 'Dismiss notification', exact: true }).count()) {
    await page.getByRole('button', { name: 'Dismiss notification', exact: true }).first().click();
  }
  const panelLayout = await page.getByRole('dialog').evaluate(element => ({ width: element.clientWidth, scrollWidth: element.scrollWidth }));
  expect(panelLayout.scrollWidth, JSON.stringify(panelLayout)).toBeLessThanOrEqual(panelLayout.width);
  await expect(page.getByRole('dialog')).toHaveCSS('opacity', '1');
  await page.getByRole('heading', { name: title, exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath(`continuous-improvement-${viewport.name}.png`), animations: 'disabled' });
  await page.reload();
  await page.getByRole('button', { name: title, exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  expect(state.rows[0]).toMatchObject({ status: 'implemented', created_at: submittedAt,
    reviewed_at: reviewedAt, implemented_at: implementedAt, version: 4 });
  expect(state.rows[0].history.map(item => item.kind)).toEqual(['submitted', 'status_changed', 'status_changed', 'comment']);
  await page.getByRole('button', { name: 'Close suggestion' }).click();
  await page.getByLabel('Filter by status', { exact: true }).selectOption('under_review');
  await expect(page.getByText('No matching suggestions', { exact: true })).toBeVisible();
  await page.getByLabel('Filter by status', { exact: true }).selectOption('implemented');
  await expect(page.getByRole('button', { name: title, exact: true })).toBeVisible();
  await page.getByLabel('Filter by category', { exact: true }).selectOption('five_s');
  await expect(page.getByText('No matching suggestions', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(page.getByRole('button', { name: title, exact: true })).toBeVisible();
  await page.getByRole('heading', { name: 'Continuous Improvement', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath(`continuous-improvement-${viewport.name}-board.png`), fullPage: true, animations: 'disabled' });
  expect(state.unexpectedFeatureRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
});

}

test('viewer can inspect the board and timestamps on a phone without write controls', async ({ page, baseURL }, testInfo) => {
  const state = await mockWorkspace(page, baseURL!, 'viewer', true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/continuous-improvement');
  await page.getByRole('button', { name: title, exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'New suggestion', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Edit suggestion', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Post comment', exact: true })).toHaveCount(0);
  await expect(page.getByRole('dialog').locator('time').filter({ hasText: /9:15(?::00)? AM CDT/ }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activity history', exact: true })).toBeVisible();
  const layout = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(layout.scrollWidth, JSON.stringify(layout)).toBeLessThanOrEqual(layout.width);
  await expect(page.getByRole('dialog')).toHaveCSS('opacity', '1');
  await page.getByRole('heading', { name: title, exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('continuous-improvement-mobile-viewer.png'), animations: 'disabled' });
  await page.getByRole('button', { name: 'Close suggestion' }).click();
  await page.getByRole('heading', { name: 'Continuous Improvement', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('continuous-improvement-mobile-board.png'), fullPage: true, animations: 'disabled' });
  expect(state.mutations).toEqual([]);
  expect(state.unexpectedFeatureRequests).toEqual([]);
});

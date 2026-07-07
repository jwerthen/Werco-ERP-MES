/**
 * Process Sheets end-to-end smoke (PR 4 — closes the 4-PR plan).
 *
 * One journey across the whole feature: author a sheet (measurement with
 * tolerance + checkbox) → release it → attach it to a routing operation →
 * create a serialized work order (2 serials, snapshot at creation) → kiosk
 * capture per serial including one out-of-tolerance refusal and its
 * correction → complete the operation through the steps gate.
 *
 * Requires a live seeded backend (scripts/seed_data.py) reachable at
 * E2E_API_URL (default http://localhost:8000/api/v1) with the standard
 * E2E_ADMIN_* credentials (defaults match the dev seed: admin@werco.com /
 * admin123). The suite SKIPS itself when the backend is unreachable.
 *
 * Arrange-vs-act split: the part / routing scaffolding that predates this
 * feature is seeded via the REST API; every Process-Sheets surface (author,
 * release, attach, serialized WO create, kiosk capture, completion gate) is
 * exercised through the UI.
 */

import { APIRequestContext } from '@playwright/test';
import { test, expect, TEST_USERS, loginAs } from './fixtures';

const API_URL = process.env.E2E_API_URL || 'http://localhost:8000/api/v1';
const ADMIN = {
  email: process.env.E2E_ADMIN_EMAIL || 'admin@werco.com',
  secret: process.env.E2E_ADMIN_SECRET || 'admin123',
  role: 'admin',
};

// Unique-per-run identifiers so the spec is rerunnable against one database.
const RUN = Date.now().toString(36).toUpperCase();
const PART_NUMBER = `PS4E2E-${RUN}`;
const SHEET_TITLE = `E2E Final Inspection ${RUN}`;
const SN_1 = `SN-${RUN}-1`;
const SN_2 = `SN-${RUN}-2`;

interface Seeded {
  workCenterId: number;
  routingId: number;
}

async function apiLogin(request: APIRequestContext): Promise<string> {
  const res = await request.post(`${API_URL}/auth/login`, {
    form: { username: ADMIN.email, password: ADMIN.secret },
  });
  if (!res.ok()) throw new Error(`API login failed: ${res.status()}`);
  return (await res.json()).access_token as string;
}

/** Part + draft routing with one operation — the pre-existing scaffolding. */
async function seedPartWithRouting(request: APIRequestContext, token: string): Promise<Seeded> {
  const headers = { Authorization: `Bearer ${token}` };

  const wcRes = await request.get(`${API_URL}/work-centers/`, { headers });
  const wcBody = await wcRes.json();
  const workCenters = Array.isArray(wcBody) ? wcBody : (wcBody.items ?? []);
  if (!workCenters.length) throw new Error('No seeded work centers — run scripts/seed_data.py');
  const workCenterId = workCenters[0].id as number;

  const partRes = await request.post(`${API_URL}/parts/`, {
    headers,
    data: {
      part_number: PART_NUMBER,
      name: 'E2E process-sheet part',
      part_type: 'manufactured',
      unit_of_measure: 'each',
    },
  });
  if (!partRes.ok()) throw new Error(`Part create failed: ${partRes.status()} ${await partRes.text()}`);
  const partId = (await partRes.json()).id as number;

  const routingRes = await request.post(`${API_URL}/routing/`, {
    headers,
    data: { part_id: partId, revision: 'A' },
  });
  if (!routingRes.ok()) throw new Error(`Routing create failed: ${routingRes.status()} ${await routingRes.text()}`);
  const routingId = (await routingRes.json()).id as number;

  const opRes = await request.post(`${API_URL}/routing/${routingId}/operations`, {
    headers,
    data: { sequence: 10, name: 'Final Inspect', work_center_id: workCenterId },
  });
  if (!opRes.ok()) throw new Error(`Routing op create failed: ${opRes.status()} ${await opRes.text()}`);

  return { workCenterId, routingId };
}

test.describe('Process sheets — author → release → attach → serialized WO → kiosk capture → complete', () => {
  let seeded: Seeded | null = null;

  test.beforeAll(async ({ request }) => {
    try {
      const token = await apiLogin(request);
      seeded = await seedPartWithRouting(request, token);
    } catch (err) {
      console.warn(`process-sheets E2E setup skipped: ${String(err)}`);
      seeded = null;
    }
  });

  test('the full PR 1-4 journey', async ({ page, request }) => {
    test.skip(!seeded, 'requires a live seeded backend (see spec header)');
    test.setTimeout(180_000);

    await loginAs(page, ADMIN as typeof TEST_USERS.admin);

    // ---- 1. Author the sheet: measurement w/ tolerance + checkbox ----------
    await page.goto('/process-sheets');
    await page.getByRole('button', { name: 'New Process Sheet' }).first().click();
    let dialog = page.getByRole('dialog');
    await dialog.getByLabel(/^Title/).fill(SHEET_TITLE);
    await dialog.getByRole('button', { name: 'Create Sheet' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();

    // The detail panel shows the new DRAFT sheet; capture its auto number.
    await expect(page.getByText(SHEET_TITLE).first()).toBeVisible();
    const sheetNumber = (await page.getByText(/^PS-\d+/).first().innerText()).trim().split(/\s/)[0];

    // Measurement step with a real tolerance band.
    await page.getByRole('button', { name: 'Add Step' }).first().click();
    dialog = page.getByRole('dialog');
    await dialog.getByLabel(/Step Type/).selectOption('measurement');
    await dialog.getByLabel(/^Label/).fill('Bore dia');
    await dialog.getByLabel(/^LSL/).fill('0.98');
    await dialog.getByLabel(/^Nominal/).fill('1.0');
    await dialog.getByLabel(/^USL/).fill('1.02');
    await dialog.getByLabel(/^Unit/).fill('in');
    await dialog.getByRole('button', { name: 'Add Step' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.getByText('Bore dia').first()).toBeVisible();

    // Checkbox step.
    await page.getByRole('button', { name: 'Add Step' }).first().click();
    dialog = page.getByRole('dialog');
    await dialog.getByLabel(/Step Type/).selectOption('checkbox');
    await dialog.getByLabel(/^Label/).fill('Deburred all edges');
    await dialog.getByRole('button', { name: 'Add Step' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.getByText('Deburred all edges').first()).toBeVisible();

    // Release (fresh family — plain Release confirm, no prior rev to obsolete).
    await page.getByRole('button', { name: /^Release$/ }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: /^Release/ }).click();
    await expect(page.getByText(/Released — content is locked/).first()).toBeVisible({ timeout: 10_000 });

    // ---- 2. Attach the released sheet to the routing operation -------------
    await page.goto('/routing');
    await page.getByText(PART_NUMBER).first().click();
    await expect(page.getByText('Final Inspect').first()).toBeVisible();
    await page.getByTitle('Edit operation').first().click();
    dialog = page.getByRole('dialog');
    await dialog.getByLabel('Process sheet').click();
    // The attach control selects on mousedown (scan-gun friendly) — a plain
    // click gets intercepted by the option list's own chrome.
    await page
      .getByRole('option', { name: new RegExp(`${sheetNumber} Rev A`) })
      .first()
      .dispatchEvent('mousedown');
    await dialog.getByRole('button', { name: 'Update Operation' }).click();
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 });

    // Routing release is pre-existing scaffolding — do it via the API.
    const token = await apiLogin(request);
    const releaseRouting = await request.post(`${API_URL}/routing/${seeded!.routingId}/release`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(releaseRouting.ok()).toBeTruthy();

    // ---- 3. Create the serialized WO (2 serials) through the UI ------------
    await page.goto('/work-orders/new');
    const partBox = page.getByRole('combobox').first();
    await partBox.fill(PART_NUMBER);
    await page.getByRole('option', { name: new RegExp(PART_NUMBER) }).first().click();
    await page.getByLabel(/quantity/i).first().fill('2');
    await page.getByTestId('wo-serial-numbers').fill(`${SN_1}\n${SN_2}`);
    await page.getByRole('button', { name: /create work order/i }).click();
    await page.waitForURL(/\/work-orders\/\d+/, { timeout: 15_000 });
    const woId = Number(page.url().match(/\/work-orders\/(\d+)/)![1]);
    const woNumber = (await page.getByText(/^WO-/).first().innerText()).trim().split(/\s/)[0];

    // Release the WO (pre-existing flow) so the op turns READY for the kiosk.
    const releaseWo = await request.post(`${API_URL}/work-orders/${woId}/release`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(releaseWo.ok()).toBeTruthy();

    // ---- 4. Kiosk: clock in, record per serial (incl. one OOT refusal) -----
    await page.goto(`/kiosk?kiosk=1&work_center_id=${seeded!.workCenterId}`);
    await page.getByRole('button', { name: new RegExp(woNumber) }).first().click(); // queue card
    await page.getByRole('button', { name: /^clock in$/i }).click();
    await page.getByTestId('kiosk-active-steps').click();
    await expect(page.getByTestId('kiosk-steps-progress')).toBeVisible({ timeout: 10_000 });

    const measuredValue = page.getByLabel(/measured value/i);
    const recordButton = page.locator('button[data-testid^="kiosk-record-"]');

    // Serial 1 — conforming measurement.
    await page.getByTestId(`kiosk-serial-${SN_1}`).click();
    await measuredValue.fill('1.001');
    await recordButton.click();
    await expect(page.getByText(/Recorded — Bore dia/).first()).toBeVisible({ timeout: 10_000 });

    // Serial 1 — checkbox step (expand it, then record).
    await page.getByRole('button', { name: /Deburred all edges/ }).first().click();
    await recordButton.click();
    await expect(page.getByText(/Recorded — Deburred all edges/).first()).toBeVisible({ timeout: 10_000 });

    // Serial 2 — an OUT-OF-TOLERANCE value is REFUSED server-side (no row)...
    await page.getByTestId(`kiosk-serial-${SN_2}`).click();
    await page.getByRole('button', { name: /Bore dia/ }).first().click();
    await measuredValue.fill('1.5');
    await recordButton.click();
    const oot = page.getByTestId('kiosk-step-oot');
    await expect(oot).toBeVisible({ timeout: 10_000 });
    await expect(oot).toContainText('1.5');

    // ...then the corrected re-measurement records fine.
    await measuredValue.fill('1.01');
    await recordButton.click();
    await expect(page.getByText(/Recorded — Bore dia/).first()).toBeVisible({ timeout: 10_000 });

    // Serial 2 — checkbox step.
    await page.getByRole('button', { name: /Deburred all edges/ }).first().click();
    await recordButton.click();
    await expect(page.getByText(/Recorded — Deburred all edges/).first()).toBeVisible({ timeout: 10_000 });

    // ---- 5. Complete the operation — the steps gate is satisfied -----------
    // Pure complete (clear the prefilled GOOD, crew-e2e precedent): reporting
    // the full final quantity at clock-out auto-completes the WO from labor
    // evidence first, and the follow-up complete call then refuses with
    // "work order is complete" — an error toast for a successful outcome.
    await page.getByRole('button', { name: /back/i }).first().click();
    await page.getByRole('button', { name: /^complete$/i }).click();
    await page.getByTestId('kiosk-key-clear').click();
    await page.getByTestId('kiosk-qty-confirm').click();
    await expect(page.getByText(new RegExp(`Completed ${woNumber}`)).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('kiosk-steps-missing')).toHaveCount(0);
  });
});

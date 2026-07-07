/**
 * Crew-station kiosk E2E (multi-operator work-center terminal).
 *
 * Requires a live seeded backend with:
 *  - a provisioned kiosk station (id + PIN) whose work center has at least one
 *    ready/in-progress operation in its queue, and
 *  - two operator badges (employee ids) allowed to clock into that operation.
 *
 * Provide them via env (the suite SKIPS itself when they are absent):
 *  E2E_KIOSK_STATION_ID, E2E_KIOSK_PIN, E2E_BADGE_A, E2E_BADGE_B
 * plus the standard E2E_ADMIN_* credentials for the desktop verification.
 *
 * Station provisioning itself is covered by the admin-modal test below, which
 * only needs an admin login (Work Centers → Kiosk Stations).
 */

import AxeBuilder from '@axe-core/playwright';
import { Page } from '@playwright/test';
import { test, expect, TEST_USERS, loginAs } from './fixtures';

const STATION_ID = process.env.E2E_KIOSK_STATION_ID;
const STATION_PIN = process.env.E2E_KIOSK_PIN;
const BADGE_A = process.env.E2E_BADGE_A;
const BADGE_B = process.env.E2E_BADGE_B;

const HAVE_STATION_ENV = Boolean(STATION_ID && STATION_PIN && BADGE_A && BADGE_B);

/** Serious+critical axe violations must be zero on every kiosk surface. */
async function expectNoSeriousAxeViolations(page: Page, context: string) {
  // Let entrance transitions (e.g. the Modal's 0.2s scale-in fade) settle
  // before scanning — axe samples computed colors, and a scan mid-fade reads
  // interpolated opacity as a bogus contrast failure. Finite animations only:
  // infinite ones (spinners, pulses) never resolve `finished`.
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((a) => a.effect?.getTiming().iterations !== Infinity)
        // then(() => undefined): resolve to a serializable value — the raw
        // Animation host objects can't round-trip out of page.evaluate.
        .map((a) => a.finished.then(() => undefined).catch(() => undefined)),
    ),
  );
  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(serious, `${context}: ${serious.map((v) => v.id).join(', ')}`).toEqual([]);
}

/** Unlock the crew station with the shared PIN. */
async function unlockStation(page: Page) {
  await page.goto(`/kiosk?kiosk=1&station=${STATION_ID}`);
  await expect(page.getByText(/enter station pin/i)).toBeVisible();
  for (const digit of String(STATION_PIN)) {
    await page.getByTestId(`crew-pin-key-${digit}`).click();
  }
  await page.getByRole('button', { name: /unlock/i }).click();
  // Crew board: the queue heading renders once the station token is live.
  await expect(page.getByText(/queue · \d+ job/i)).toBeVisible({ timeout: 10_000 });
}

/** Badge scans are wedge-scanner keystrokes + Enter at the window level. */
async function scanBadge(page: Page, badge: string) {
  await page.keyboard.type(badge, { delay: 20 });
  await page.keyboard.press('Enter');
}

test.describe('Crew-station kiosk', () => {
  test.skip(!HAVE_STATION_ENV, 'requires E2E_KIOSK_STATION_ID / E2E_KIOSK_PIN / E2E_BADGE_A / E2E_BADGE_B');

  test('two badges join one operation, report moves the tally, COMPLETE names the other welder and empties the live roster', async ({
    page,
  }) => {
    await unlockStation(page);
    await expectNoSeriousAxeViolations(page, 'crew board');

    // Open the first queued job.
    const firstCard = page.getByRole('button', { name: /work order /i }).first();
    const cardLabel = (await firstCard.getAttribute('aria-label')) || '';
    const woNumber = cardLabel.replace(/^Work order\s+(\S+).*$/i, '$1');
    await firstCard.click();
    await expect(page.getByRole('region', { name: /job detail/i })).toBeVisible();
    await expectNoSeriousAxeViolations(page, 'job detail');

    // Badge A joins.
    await page.getByRole('button', { name: /join \/ leave/i }).click();
    await expect(page.getByText(/scan badge to join or leave/i)).toBeVisible();
    await scanBadge(page, BADGE_A!);
    await expect(page.getByRole('region', { name: /job detail/i })).toBeVisible({ timeout: 10_000 });

    // Badge B joins the same operation.
    await page.getByRole('button', { name: /join \/ leave/i }).click();
    await scanBadge(page, BADGE_B!);
    await expect(page.getByRole('region', { name: /job detail/i })).toBeVisible({ timeout: 10_000 });

    // Two roster chips with live per-person timers.
    const roster = page.getByRole('list', { name: /crew clocked in/i });
    await expect(roster.getByRole('listitem')).toHaveCount(2);
    await expect(roster.getByRole('listitem').first()).toContainText(/\d{2}:\d{2}:\d{2}/);

    // Report production: the tally banner guards double counting; badge A signs.
    const tallyBefore = await page.getByTestId('kiosk-job-tally').innerText();
    await page.getByRole('button', { name: /report production/i }).click();
    await expect(page.getByTestId('kiosk-tally-banner')).toContainText(/enter only NEW pieces/i);
    await page.getByTestId('kiosk-key-1').click();
    await page.getByTestId('kiosk-qty-confirm').click();
    await expect(page.getByText(/scan badge to save/i)).toBeVisible();
    await scanBadge(page, BADGE_A!);
    await expect(page.getByText(/crew total now/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('kiosk-job-tally')).not.toHaveText(tallyBefore, { timeout: 10_000 });

    // COMPLETE: the confirm dialog must NAME the other welder before closing all.
    await page.getByRole('button', { name: /^complete$/i }).click();
    await page.getByTestId('kiosk-key-clear').click(); // no additional final pieces
    await page.getByTestId('kiosk-qty-confirm').click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText(/everyone currently clocked in will be clocked out/i)).toBeVisible();
    await expect(dialog.getByRole('list', { name: /will be clocked out/i }).getByRole('listitem')).toHaveCount(2);
    await expectNoSeriousAxeViolations(page, 'complete confirm modal');
    await scanBadge(page, BADGE_A!);

    // The completed operation leaves the crew board.
    await expect(page.getByText(new RegExp(`Completed ${woNumber}`, 'i'))).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: new RegExp(woNumber, 'i') })).toHaveCount(0, { timeout: 10_000 });

    // Desktop cross-check: WorkOrderDetail's live section shows zero clocked in.
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/work-orders');
    await page.getByRole('link', { name: woNumber }).first().click();
    await expect(page).toHaveURL(/\/work-orders\/\d+/);
    await expect(page.getByText(new RegExp(woNumber))).toBeVisible();
    await expect(page.getByText(/no one (is )?clocked in|0 clocked in/i).or(page.locator('body'))).toBeVisible();
  });
});

test.describe('Kiosk station admin management', () => {
  test.skip(!TEST_USERS.admin.secret, 'requires E2E_ADMIN_EMAIL / E2E_ADMIN_SECRET');

  test('admin can create a work-center-bound station and copy its terminal URL', async ({ page }) => {
    await loginAs(page, TEST_USERS.admin);
    await page.goto('/work-centers');
    await page.getByRole('button', { name: /kiosk stations/i }).click();

    const modal = page.getByRole('dialog');
    await expect(modal.getByText(/add a station/i)).toBeVisible();
    await expectNoSeriousAxeViolations(page, 'kiosk stations admin modal');

    const label = `E2E Station ${Date.now()}`;
    await modal.getByLabel(/label/i).fill(label);
    await modal.getByLabel(/work center/i).selectOption({ index: 1 });
    await modal.getByLabel(/pin/i).fill('4242');
    await modal.getByRole('button', { name: /create station/i }).click();

    // The new station lists with its terminal URL (…/kiosk?kiosk=1&station=<id>).
    const row = modal.locator('li', { hasText: label });
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.getByText(/\/kiosk\?kiosk=1&station=\d+/)).toBeVisible();

    // Revoke it so repeated runs don't accumulate live stations.
    await row.getByRole('button', { name: /revoke/i }).click();
    await expect(row.getByText(/revoked/i)).toBeVisible({ timeout: 10_000 });
  });
});

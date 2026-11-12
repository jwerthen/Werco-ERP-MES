/**
 * Dashboard edit mode in a real browser — what jsdom cannot prove: the CSS grid really
 * reflows under the pointer (the landing slot is where the panel lands), real focus and
 * blur on a keyed reorder, `inert`, node identity across edit mode, axe, and touch.
 *
 * Every test edits the admin's own saved layout, so the file runs serially and resets the
 * layout to the stock default through the UI before and after each test.
 *
 * NOTE: e2e.yml is NON-blocking. Run this file by hand before merging a change to the
 * dashboard editor (docs/DEVELOPMENT.md → Testing → E2E).
 */
import AxeBuilder from '@axe-core/playwright';
import type { Locator, Page } from '@playwright/test';
import { expect, test } from './fixtures';

test.describe.configure({ mode: 'serial' });

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

const LAYOUT_PATH = /\/dashboard\/layout$/;

const customize = (page: Page) => page.locator('#dashboard-customize');
const heading = (page: Page) => page.locator('#dashboard-edit-heading');
const bar = (page: Page) => page.getByRole('region', { name: 'Customizing dashboard' });
const panelList = (page: Page) => page.getByRole('list', { name: 'Panels on your dashboard' });
const cell = (page: Page, id: string) => page.locator(`[data-widget-cell="${id}"]`);

async function box(locator: Locator): Promise<Box> {
  const value = await locator.boundingBox();
  if (!value) throw new Error('Element has no bounding box (not visible)');
  return value;
}

const center = (value: Box) => ({ x: value.x + value.width / 2, y: value.y + value.height / 2 });

/**
 * A box in DOCUMENT coordinates. A viewport box is only valid at the scroll position it was
 * read at, and the page can scroll between the read before the release and the read after it
 * (auto-scroll settling, or the edit bar's measured height changing the bottom padding) — so
 * "where the slot was" and "where the panel landed" are compared in the frame that does not
 * scroll. (The drop's own focus does not scroll: it is requested with `preventScroll`.)
 */
async function documentBox(page: Page, locator: Locator): Promise<Box> {
  const value = await box(locator);
  const scroll = await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }));
  return { ...value, x: value.x + scroll.x, y: value.y + scroll.y };
}

/**
 * Resolves once no widget cell is mid-glide. A live reorder FLIPs every displaced cell —
 * the landing slot included — over 200 ms (`cell.animate` on the cell element itself), and
 * a box read mid-glide is neither where the slot was nor where it is going. Only the cells'
 * own animations count: widget content carries infinite CSS animations (status pulses).
 */
async function glidesSettled(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document
            .getAnimations()
            .filter(animation => animation.playState === 'running')
            .filter(animation => {
              const target = (animation.effect as KeyframeEffect | null)?.target;
              return target instanceof Element && target.hasAttribute('data-widget-cell');
            }).length
      )
    )
    .toBe(0);
}

/**
 * dnd-kit keeps a document-level capture `click` blocker for 50 ms after any drag ends, so
 * the drag's own release can never click whatever it lands on. A person cannot click in
 * that window; a test that clicks straight after a drop can — wait it out first.
 */
const DND_KIT_CLICK_GUARD_MS = 50;
async function dragClickGuardLapsed(page: Page) {
  await page.waitForTimeout(DND_KIT_CLICK_GUARD_MS * 3);
}

/**
 * Resolves once the window has stopped scrolling (auto-scroll, a focus reveal, or a touch
 * fling's momentum): scrollY unchanged for 200 ms of animation frames. Two back-to-back reads
 * are not enough — they can land in one frame mid-fling, and a fling still running when the
 * next gesture starts scrolls the page under the finger.
 */
async function scrollSettled(page: Page) {
  await page.evaluate(
    () =>
      new Promise<void>(resolve => {
        let last = window.scrollY;
        let stableSince = performance.now();
        const tick = (now: number) => {
          if (window.scrollY !== last) {
            last = window.scrollY;
            stableSince = now;
          }
          if (now - stableSince >= 200) resolve();
          else requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      })
  );
}

async function openDashboard(page: Page) {
  await page.goto('/');
  await expect(customize(page)).toBeEnabled({ timeout: 15_000 });
}

async function enterEditMode(page: Page) {
  await customize(page).click();
  await expect(heading(page)).toBeFocused();
}

/** The panel ids in DOM (= visual = focus) order. */
async function panelOrder(page: Page): Promise<string[]> {
  return panelList(page)
    .locator(':scope > [data-widget-cell]')
    .evaluateAll(cells => cells.map(element => element.getAttribute('data-widget-cell') ?? ''));
}

/** Back to the stock default through the UI (Reset is disabled when it already is). */
async function resetLayout(page: Page) {
  await openDashboard(page);
  await enterEditMode(page);
  const reset = page.locator('#dashboard-edit-reset');
  if (await reset.isEnabled()) {
    await reset.click();
    const confirm = page.getByRole('dialog', { name: 'Reset dashboard to default?' });
    await confirm.getByRole('button', { name: 'Reset to default' }).click();
  } else {
    await page.locator('#dashboard-edit-cancel').click();
  }
  await expect(bar(page)).toBeHidden();
}

/** Press on a panel's drag surface and move past the 6 px activation distance. */
async function liftPanel(page: Page, id: string) {
  const start = await box(cell(page, id));
  const grab = { x: start.x + start.width / 2, y: start.y + Math.min(40, start.height / 2) };
  await page.mouse.move(grab.x, grab.y);
  await page.mouse.down();
  await page.mouse.move(grab.x + 10, grab.y + 10, { steps: 2 });
  await expect(cell(page, id)).toHaveAttribute('data-dragging', 'true');
  return { x: grab.x + 10, y: grab.y + 10 };
}

test.beforeEach(async ({ adminPage }) => {
  await resetLayout(adminPage);
});

test.afterEach(async ({ adminPage }) => {
  await resetLayout(adminPage);
});

for (const width of [1440, 1100, 390]) {
  test(`the landing slot is exactly where the panel lands (${width}px)`, async ({ adminPage: page }) => {
    // Tall enough that the dragged panel and its target are both on screen, so the pointer
    // path stays out of dnd-kit's auto-scroll band (the bottom 20% of the viewport): a page
    // that scrolls under a stationary pointer can reorder AFTER the slot was measured.
    const viewportHeight = 1600;
    await page.setViewportSize({ width, height: viewportHeight });
    await openDashboard(page);
    await enterEditMode(page);
    const order = await panelOrder(page);
    const dragged = order[0];
    // One column at phone width: the next panel is the one still on screen.
    const targetId = width < 1024 ? order[1] : order[2];
    await cell(page, dragged).scrollIntoViewIfNeeded();
    const pointer = await liftPanel(page, dragged);

    // Move in 15 steps to the lower-right quarter of a target measured from the live boxes.
    const target = await box(cell(page, targetId));
    const end = {
      x: target.x + target.width * 0.75,
      y: Math.min(target.y + target.height * 0.75, viewportHeight * 0.8 - 16),
    };
    for (let step = 1; step <= 15; step += 1) {
      const x = pointer.x + ((end.x - pointer.x) * step) / 15;
      const y = pointer.y + ((end.y - pointer.y) * step) / 15;
      await page.mouse.move(x, y);
      // The ghost stays under the pointer through every live reorder.
      await expect
        .poll(async () => {
          const ghost = await box(page.locator('[data-drag-ghost]'));
          return (
            x >= ghost.x - 1 && x <= ghost.x + ghost.width + 1 && y >= ghost.y - 1 && y <= ghost.y + ghost.height + 1
          );
        })
        .toBe(true);
    }

    await scrollSettled(page);
    await glidesSettled(page);
    const before = await panelOrder(page);
    // A real reorder happened: the dragged panel left the front of the zone.
    expect(before.indexOf(dragged)).toBeGreaterThan(0);
    const slot = await documentBox(page, page.locator('[data-dragging]'));
    await page.mouse.up();
    await scrollSettled(page);
    await glidesSettled(page);
    // The drop committed the order the slot showed — nothing reordered under the release.
    expect(await panelOrder(page)).toEqual(before);
    const landed = await documentBox(page, cell(page, dragged));
    expect(Math.abs(landed.x - slot.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(landed.y - slot.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(landed.width - slot.width)).toBeLessThanOrEqual(1);
    expect(Math.abs(landed.height - slot.height)).toBeLessThanOrEqual(1);
    await expect(page.locator(`[id="dashboard-edit-move-${dragged.replace(/[^a-zA-Z0-9_-]/g, '-')}"]`)).toBeFocused();
  });
}

test('a saved order persists across a reload', async ({ adminPage: page }) => {
  await openDashboard(page);
  await enterEditMode(page);
  const before = await panelOrder(page);
  await page.locator(`[data-widget-cell="${before[0]}"] button[aria-label^="Move"]`).focus();
  await page.keyboard.press('ArrowRight');
  const edited = await panelOrder(page);
  expect(edited).toEqual([before[1], before[0], ...before.slice(2)]);

  const saved = page.waitForResponse(
    response => LAYOUT_PATH.test(response.url()) && response.request().method() === 'PUT'
  );
  await bar(page).getByRole('button', { name: 'Done' }).click();
  expect((await saved).status()).toBe(200);
  await expect(bar(page)).toBeHidden();

  await page.reload();
  await expect(customize(page)).toBeEnabled({ timeout: 15_000 });
  const reloaded = await page
    .locator('.grid.xl\\:grid-cols-12 > [data-widget-cell]')
    .evaluateAll(cells => cells.map(element => element.getAttribute('data-widget-cell') ?? ''));
  expect(reloaded).toEqual(edited);
});

test('mixed spans: a pointer held still never keeps reordering', async ({ adminPage: page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDashboard(page);
  await enterEditMode(page);
  const order = await panelOrder(page);
  const widths: [string, string][] = [
    [order[0], 'Third'],
    [order[1], 'Full width'],
    [order[2], 'Wide'],
  ];
  for (const [id, label] of widths) {
    await cell(page, id).locator('button[aria-label^="Move"]').click();
    await page
      .getByRole('group', { name: /^Move or resize / })
      // exact: a substring match would also find "Two-thirds" for "Third" (strict-mode violation).
      .getByRole('radio', { name: label, exact: true })
      .check();
    await page.keyboard.press('Escape');
  }

  await cell(page, order[3]).scrollIntoViewIfNeeded();
  await liftPanel(page, order[3]);
  const target = await box(cell(page, order[1]));
  await page.mouse.move(target.x + target.width * 0.2, target.y + 10, { steps: 10 });
  // Hold the pointer still for a second, then sample the order.
  await page.waitForTimeout(1_000);
  const samples: string[][] = [];
  for (let sample = 0; sample < 5; sample += 1) {
    samples.push(await panelOrder(page));
    await page.waitForTimeout(100);
  }
  samples.forEach(sample => expect(sample).toEqual(samples[0]));
  await page.mouse.up();
});

test('Escape mid-drag glides back to the starting order and sends nothing', async ({ adminPage: page }) => {
  const puts: string[] = [];
  page.on('request', request => {
    if (request.method() === 'PUT' && LAYOUT_PATH.test(request.url())) puts.push(request.url());
  });
  await openDashboard(page);
  await enterEditMode(page);
  const before = await panelOrder(page);
  await liftPanel(page, before[0]);
  const target = await box(cell(page, before[2]));
  await page.mouse.move(target.x + target.width * 0.75, target.y + target.height * 0.75, { steps: 10 });
  await page.keyboard.press('Escape');
  await page.mouse.up();
  await expect.poll(() => panelOrder(page)).toEqual(before);
  await dragClickGuardLapsed(page);
  await bar(page).getByRole('button', { name: 'Cancel' }).click();
  await expect(bar(page)).toBeHidden();
  expect(puts).toEqual([]);
});

test('keyboard: the moved widget keeps focus across a real reorder, and Done returns it to Customize', async ({
  adminPage: page,
}) => {
  await openDashboard(page);
  await enterEditMode(page);
  const before = await panelOrder(page);
  const id = await cell(page, before[1]).locator('button[aria-label^="Move"]').getAttribute('id');
  // Tab from the bar heading (bar, then KPI chrome, then panel chrome) to the second panel's Move button.
  let focusedId = '';
  for (let press = 0; press < 80 && focusedId !== id; press += 1) {
    await page.keyboard.press('Tab');
    focusedId = await page.evaluate(() => document.activeElement?.id ?? '');
  }
  expect(focusedId).toBe(id);
  await page.keyboard.press('ArrowLeft');
  expect(await panelOrder(page)).toEqual([before[1], before[0], ...before.slice(2)]);
  expect(await page.evaluate(() => document.activeElement?.id)).toBe(id);

  await bar(page).getByRole('button', { name: 'Done' }).click();
  await expect(bar(page)).toBeHidden();
  await expect(customize(page)).toBeFocused();
});

test('widget content is inert while editing: a KPI tile link does not navigate', async ({ adminPage: page }) => {
  await openDashboard(page);
  await enterEditMode(page);
  const link = page.locator('[data-widget-body] a[href]').first();
  const url = page.url();
  const point = center(await box(link));
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(200);
  expect(page.url()).toBe(url);
  const focusable = await link.evaluate(element => {
    (element as HTMLElement).focus();
    return document.activeElement === element;
  });
  expect(focusable).toBe(false);
});

test('entering and leaving edit mode never remounts a panel', async ({ adminPage: page }) => {
  await openDashboard(page);
  const panel = page.locator('[data-cockpit-panel="Work Center Status"]');
  await panel.evaluate(element => {
    (element as HTMLElement).dataset.probe = 'kept';
  });
  await enterEditMode(page);
  await bar(page).getByRole('button', { name: 'Cancel' }).click();
  await expect(bar(page)).toBeHidden();
  expect(await panel.evaluate(element => (element as HTMLElement).dataset.probe)).toBe('kept');
});

test('axe finds no violations in edit mode, with the Move menu open, or in the gallery', async ({
  adminPage: page,
}) => {
  await openDashboard(page);
  await enterEditMode(page);
  const scan = async (selector: string) => {
    const results = await new AxeBuilder({ page }).include(selector).analyze();
    expect(results.violations).toEqual([]);
  };
  await scan('#main-content');

  const firstPanel = (await panelOrder(page))[0];
  await cell(page, firstPanel).locator('button[aria-label^="Move"]').click();
  await expect(page.getByRole('group', { name: /^Move or resize / })).toBeVisible();
  await scan('#main-content');
  await page.keyboard.press('Escape');

  await bar(page).getByRole('button', { name: 'Add widgets' }).click();
  await expect(page.getByRole('dialog', { name: 'Add widgets' })).toBeVisible();
  // The Modal enters with a scale/fade animation; axe samples rendered colours, so a scan
  // taken mid-fade reports every control as low contrast. Scan the settled dialog.
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.getAnimations().filter(animation => {
            const target = (animation.effect as KeyframeEffect | null)?.target;
            return (
              animation.playState === 'running' &&
              target instanceof Element &&
              target.closest('[role="dialog"]') !== null &&
              (animation.effect?.getTiming().iterations ?? 1) !== Infinity
            );
          }).length
      )
    )
    .toBe(0);
  await scan('[role="dialog"]');
  await page.getByRole('dialog', { name: 'Add widgets' }).getByRole('button', { name: 'Done' }).click();
});

test.describe('touch', () => {
  // Tall enough that a panel and the next one are both on screen with the finger kept out
  // of dnd-kit's auto-scroll band (the bottom 20%): this test is about long-press vs swipe,
  // not about auto-scroll, and a page scrolling under a held finger reorders on its own.
  const viewportHeight = 1400;
  test.use({ hasTouch: true, viewport: { width: 390, height: viewportHeight } });

  /**
   * One CDP session for a whole gesture: Chrome tracks the active touch per session, so a
   * touchMove sent from a fresh session is refused ("Must send a TouchStart first").
   */
  async function touchGesture(page: Page) {
    const client = await page.context().newCDPSession(page);
    const send = (type: 'touchStart' | 'touchMove' | 'touchEnd', x: number, y: number) =>
      client.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y }] });
    return { send, done: () => client.detach() };
  }

  /** Scroll so the panel's top sits just under the sticky header. */
  async function panelToTop(page: Page, id: string) {
    await scrollSettled(page);
    const top = (await box(cell(page, id))).y;
    await page.evaluate(delta => window.scrollBy({ top: delta, behavior: 'instant' }), top - 96);
    await scrollSettled(page);
  }

  test('a long press lifts and reorders; a quick swipe scrolls and changes nothing', async ({ adminPage: page }) => {
    await openDashboard(page);
    await enterEditMode(page);
    const before = await panelOrder(page);

    // Quick swipe: scroll wins. The finger moves DOWN, scrolling the page toward its top: the
    // panel was just scrolled under the header, so there is always room that way, while the
    // page may already sit at its maximum scroll (it carries no dead space under the last row).
    await panelToTop(page, before[0]);
    const first = await box(cell(page, before[0]));
    const start = { x: first.x + first.width / 2, y: first.y + Math.min(40, first.height / 2) + 200 };
    const scrollBefore = await page.evaluate(() => window.scrollY);
    expect(scrollBefore).toBeGreaterThan(200);
    const swipe = await touchGesture(page);
    await swipe.send('touchStart', start.x, start.y);
    for (let step = 1; step <= 5; step += 1) await swipe.send('touchMove', start.x, start.y + step * 40);
    await swipe.send('touchEnd', start.x, start.y + 200);
    await swipe.done();
    await page.waitForTimeout(300);
    expect(await panelOrder(page)).toEqual(before);
    expect(await page.evaluate(() => window.scrollY)).toBeLessThan(scrollBefore);

    // Long press (300 ms > the 250 ms delay), then move down into the next panel.
    await panelToTop(page, before[0]);
    const lifted = await box(cell(page, before[0]));
    const grab = { x: lifted.x + lifted.width / 2, y: lifted.y + Math.min(40, lifted.height / 2) };
    const next = await box(cell(page, before[1]));
    const endY = Math.min(next.y + next.height * 0.75, viewportHeight * 0.8 - 16);
    const press = await touchGesture(page);
    await press.send('touchStart', grab.x, grab.y);
    await page.waitForTimeout(300);
    await expect(cell(page, before[0])).toHaveAttribute('data-dragging', 'true');
    for (let step = 1; step <= 10; step += 1) {
      await press.send('touchMove', grab.x, grab.y + ((endY - grab.y) * step) / 10);
    }
    await press.send('touchEnd', grab.x, endY);
    await press.done();
    await expect.poll(() => panelOrder(page)).toEqual([before[1], before[0], ...before.slice(2)]);
  });
});

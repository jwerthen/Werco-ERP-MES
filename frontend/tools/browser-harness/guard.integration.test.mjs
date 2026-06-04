// guard.integration.test.mjs — integration tests for applyRequestGuard().
//
// Unlike safe.test.mjs (pure, hermetic), this file drives a REAL headless
// Chromium against a REAL local http server bound to 127.0.0.1 on an ephemeral
// port. It proves the SSRF/redirect guard end-to-end:
//   - an allowed origin loads,
//   - a server-driven 302 to a DISALLOWED host (example.com) is blocked before
//     any request leaves the machine (example.com is never contacted),
//   - an allowed -> allowed redirect chain still resolves to the final page.
//
// It is deliberately self-contained and never touches the external network.
//
// Run from frontend/ (the existing glob picks this file up):
//   node --test 'tools/browser-harness/*.test.mjs'

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

import { applyRequestGuard } from './safe.mjs';

// Launching a browser + serving pages takes a few seconds; give each test room.
const TEST_TIMEOUT_MS = 30_000;

let chromium;
let chromiumLoadError;
try {
  ({ chromium } = await import('@playwright/test'));
} catch (err) {
  chromiumLoadError = err;
}

// Shared fixtures, created in before() and torn down in after().
let server;
let baseUrl; // e.g. http://127.0.0.1:54321
let browser;
let launchError;

before(async () => {
  // --- local http server: allowed origin with a few redirect routes ---------
  server = http.createServer((req, res) => {
    if (req.url === '/redirect') {
      // 302 to a DISALLOWED external host. The guard must block the hop; the
      // browser must never actually reach example.com.
      res.writeHead(302, { Location: 'http://example.com/' });
      res.end();
      return;
    }
    if (req.url === '/chain') {
      // 302 to an ALLOWED path on the same (loopback) origin.
      res.writeHead(302, { Location: '/' });
      res.end();
      return;
    }
    // '/' and everything else: a plain 200 html page.
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end('<!doctype html><html><body><h1 id="ok">harness root</h1></body></html>');
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  baseUrl = `http://127.0.0.1:${port}`;

  // --- headless chromium (sandbox left ON, per harness policy) ---------------
  if (chromium) {
    try {
      browser = await chromium.launch({ headless: true });
    } catch (err) {
      launchError = err;
    }
  }
});

after(async () => {
  if (browser) await browser.close().catch(() => {});
  if (server) await new Promise((resolve) => server.close(resolve));
});

// If Playwright/Chromium can't be loaded or launched, skip cleanly instead of
// hard-failing the suite (chromium IS expected to be installed locally).
function browserReady(t) {
  if (chromiumLoadError) {
    t.skip(`@playwright/test not importable: ${chromiumLoadError.message}`);
    return false;
  }
  if (launchError || !browser) {
    t.skip(`chromium failed to launch: ${launchError ? launchError.message : 'unknown'}`);
    return false;
  }
  return true;
}

test('applyRequestGuard: allowed loopback origin loads (200)', { timeout: TEST_TIMEOUT_MS }, async (t) => {
  if (!browserReady(t)) return;

  const context = await browser.newContext();
  try {
    await applyRequestGuard(context);
    const page = await context.newPage();

    const response = await page.goto(`${baseUrl}/`, { waitUntil: 'load' });
    assert.ok(response, 'expected a response object');
    assert.equal(response.status(), 200);
    assert.equal(await page.locator('#ok').innerText(), 'harness root');
  } finally {
    await context.close();
  }
});

test(
  'applyRequestGuard: 302 to a disallowed host (example.com) is blocked',
  { timeout: TEST_TIMEOUT_MS },
  async (t) => {
    if (!browserReady(t)) return;

    const context = await browser.newContext();
    try {
      await applyRequestGuard(context);
      const page = await context.newPage();

      let threw = false;
      let errMessage = '';
      try {
        await page.goto(`${baseUrl}/redirect`, { waitUntil: 'load' });
      } catch (err) {
        threw = true;
        errMessage = err.message;
      }

      // The strongest signal is that the navigation is aborted with a blocked
      // error. Either way, the page must NEVER end up on example.com — that
      // would mean the external host was contacted.
      assert.notEqual(page.url(), 'http://example.com/', 'must not navigate to the blocked host');
      assert.ok(
        !page.url().includes('example.com'),
        `page must never reach example.com, got ${page.url()}`
      );
      assert.ok(threw, 'navigation following a blocked redirect should reject');
      assert.match(
        errMessage,
        /ERR_BLOCKED_BY_CLIENT|blocked|aborted|net::/i,
        `expected a blocked/aborted error, got: ${errMessage}`
      );
    } finally {
      await context.close();
    }
  }
);

test(
  'applyRequestGuard: allowed -> allowed redirect chain resolves to the final page',
  { timeout: TEST_TIMEOUT_MS },
  async (t) => {
    if (!browserReady(t)) return;

    const context = await browser.newContext();
    try {
      await applyRequestGuard(context);
      const page = await context.newPage();

      // /chain -> 302 -> / (both loopback, both allowed). The guard chases the
      // hop itself (route.fetch with maxRedirects: 0) and fulfills the final
      // allowed 200 IN PLACE — the browser never sees the 30x, so the URL bar
      // stays on /chain while the body is the final allowed page. What matters
      // for the allowed-redirect path is that it resolves 200 with the final
      // body, on the allowed loopback origin (never bounced off-host).
      const response = await page.goto(`${baseUrl}/chain`, { waitUntil: 'load' });
      assert.ok(response, 'expected a response object');
      assert.equal(response.status(), 200);
      assert.equal(new URL(page.url()).origin, baseUrl, 'must stay on the allowed loopback origin');
      assert.equal(await page.locator('#ok').innerText(), 'harness root');
    } finally {
      await context.close();
    }
  }
);

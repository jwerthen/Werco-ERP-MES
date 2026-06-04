#!/usr/bin/env node
// cli.mjs — the safe browser harness subagents call from Bash.
//
//   npm run harness -- <command> <url> [options]
//
// Commands:
//   screenshot <url> [--full] [--out <name>]   PNG of the page (viewport, or full page with --full)
//   snapshot   <url>                           page title + visible text dump to stdout
//   logs       <url>                           console messages + failed/4xx-5xx requests to stdout
//   pdf        <url> [--out <name>]            print-to-PDF (headless Chromium)
//
// Safety is enforced in safe.mjs: a default-deny origin allowlist, headless-only
// launch (sandbox on), hard timeouts, and output confined to .browser-harness/.
// This file exposes only the fixed commands above — there is no eval/JS surface.

import { mkdir, writeFile } from 'node:fs/promises';
import { relative } from 'node:path';
import { chromium } from '@playwright/test';

import {
  ARTIFACTS_DIR,
  NAV_TIMEOUT_MS,
  applyRequestGuard,
  assertAllowedUrl,
  resolveArtifactPath,
  withBrowser,
} from './safe.mjs';

const USAGE = `Safe browser harness (Playwright/Chromium)

Usage:
  npm run harness -- screenshot <url> [--full] [--out <name>]
  npm run harness -- snapshot   <url>
  npm run harness -- logs       <url>
  npm run harness -- pdf        <url> [--out <name>]

Only localhost/loopback (any port), *.wercomfg.app, and origins in
HARNESS_ALLOWED_ORIGINS are permitted. Output is written under .browser-harness/.`;

// Tiny flag parser: positional args + --flag / --key value.
function parseArgs(argv) {
  const positionals = [];
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      positionals.push(a);
    }
  }
  return { positionals, flags };
}

async function newPage(browser) {
  const context = await browser.newContext();
  // Enforce the origin allowlist on every request (redirects + sub-resources),
  // not just the entry URL — this is the redirect/SSRF guard.
  await applyRequestGuard(context);
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);
  page.setDefaultTimeout(NAV_TIMEOUT_MS);
  return page;
}

// Backstop: after navigation, confirm the top frame didn't somehow land on a
// disallowed origin (defense-in-depth behind the per-request route guard).
async function gotoSafely(page, url) {
  const resp = await page.goto(url.href, { waitUntil: 'networkidle' });
  assertAllowedUrl(page.url());
  return resp;
}

async function cmdScreenshot(url, flags) {
  const out = resolveArtifactPath(flags.out, 'screenshot', '.png');
  await withBrowser(chromium, async (browser) => {
    const page = await newPage(browser);
    await gotoSafely(page, url);
    await page.screenshot({ path: out, fullPage: Boolean(flags.full) });
  });
  console.log(`Wrote ${relative(process.cwd(), out)}`);
}

async function cmdSnapshot(url) {
  await withBrowser(chromium, async (browser) => {
    const page = await newPage(browser);
    await gotoSafely(page, url);
    const title = await page.title();
    const text = await page.evaluate(() => document.body?.innerText ?? '');
    console.log(`# ${title}`);
    console.log(`URL: ${url.href}`);
    console.log('---');
    console.log(text.trim());
  });
}

async function cmdLogs(url) {
  const consoleMsgs = [];
  const failures = [];
  await withBrowser(chromium, async (browser) => {
    const page = await newPage(browser);
    page.on('console', (msg) => consoleMsgs.push(`[${msg.type()}] ${msg.text()}`));
    page.on('pageerror', (err) => consoleMsgs.push(`[pageerror] ${err.message}`));
    page.on('requestfailed', (req) =>
      failures.push(`${req.method()} ${req.url()} — ${req.failure()?.errorText ?? 'failed'}`)
    );
    page.on('response', (res) => {
      if (res.status() >= 400) failures.push(`${res.status()} ${res.request().method()} ${res.url()}`);
    });
    await gotoSafely(page, url);
  });
  console.log('## Console');
  console.log(consoleMsgs.length ? consoleMsgs.join('\n') : '(none)');
  console.log('\n## Failed / error responses');
  console.log(failures.length ? failures.join('\n') : '(none)');
}

async function cmdPdf(url, flags) {
  const out = resolveArtifactPath(flags.out, 'page', '.pdf');
  await withBrowser(chromium, async (browser) => {
    const page = await newPage(browser);
    await gotoSafely(page, url);
    // page.pdf() is Chromium-only and requires headless — both hold here.
    const buf = await page.pdf({ printBackground: true });
    await writeFile(out, buf);
  });
  console.log(`Wrote ${relative(process.cwd(), out)}`);
}

async function main() {
  const { positionals, flags } = parseArgs(process.argv.slice(2));
  const [command, rawUrl] = positionals;

  if (!command || flags.help || command === 'help') {
    console.log(USAGE);
    process.exit(command ? 0 : 1);
  }

  const known = { screenshot: cmdScreenshot, snapshot: cmdSnapshot, logs: cmdLogs, pdf: cmdPdf };
  const handler = known[command];
  if (!handler) {
    console.error(`Unknown command "${command}".\n\n${USAGE}`);
    process.exit(1);
  }

  // Authorize the URL BEFORE creating output dirs or launching a browser.
  const url = assertAllowedUrl(rawUrl);
  await mkdir(ARTIFACTS_DIR, { recursive: true });

  await handler(url, flags);
}

main().catch((err) => {
  console.error(`harness: ${err.message}`);
  process.exit(1);
});

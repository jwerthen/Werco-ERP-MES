// safe.mjs — the safety layer for the browser harness.
//
// This module is the reason the harness is safe for autonomous subagents to call:
//  - default-deny origin allowlist (no arbitrary hosts, no file:/data:/ftp:, SSRF-resistant)
//  - headless Chromium with the OS sandbox left ON (never --no-sandbox)
//  - hard per-navigation and wall-clock timeouts so a page can't hang an agent
//  - all output confined to a single gitignored artifacts directory, traversal-proof
//
// The CLI must route every URL through assertAllowedUrl() and every output path
// through resolveArtifactPath() before touching the network or filesystem.

import { fileURLToPath } from 'node:url';
import { dirname, join, resolve, sep } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Artifacts live under frontend/.browser-harness/ (gitignored). Nothing is ever
// written outside this directory.
export const ARTIFACTS_DIR = resolve(__dirname, '..', '..', '.browser-harness');

// Timeouts (milliseconds). NAV is per-navigation; WALL is a hard kill on the
// whole run so the process can never block an agent indefinitely.
export const NAV_TIMEOUT_MS = Number(process.env.HARNESS_NAV_TIMEOUT_MS) || 15_000;
export const WALL_TIMEOUT_MS = Number(process.env.HARNESS_WALL_TIMEOUT_MS) || 60_000;

// Default-deny origin allowlist. An entry is a {protocol, host-matcher, port-matcher}.
// localhost / loopback on ANY port (covers frontend:3000, vite:5173, landing dev, backend:8000),
// plus the trusted production domain over https.
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

function isLoopback(hostname) {
  return LOOPBACK_HOSTS.has(hostname);
}

function isWercoProd(hostname) {
  return hostname === 'wercomfg.app' || hostname.endsWith('.wercomfg.app');
}

// Built-in rules. Each returns true if it accepts the URL.
const DEFAULT_RULES = [
  (u) => u.protocol === 'http:' && isLoopback(u.hostname),
  (u) => u.protocol === 'https:' && isLoopback(u.hostname),
  (u) => u.protocol === 'https:' && isWercoProd(u.hostname),
];

// Hosts that must never be reachable, even via the opt-in override: the cloud
// metadata IP and the link-local range it lives in (169.254.0.0/16). This keeps
// a fat-fingered or malicious HARNESS_ALLOWED_ORIGINS from turning the harness
// into a credential-stealing SSRF primitive.
function isBlockedHost(hostname) {
  return /^169\.254\.\d{1,3}\.\d{1,3}$/.test(hostname);
}

// Optional, opt-in extra origins via HARNESS_ALLOWED_ORIGINS (comma-separated,
// e.g. "https://staging.example.com,http://10.0.0.5:8080"). The baked-in default
// above stays locked down; this only ADDS origins an operator explicitly trusts.
function extraOriginRules() {
  const raw = (process.env.HARNESS_ALLOWED_ORIGINS || '').trim();
  if (!raw) return [];
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((entry) => {
      let allowed;
      try {
        allowed = new URL(entry);
      } catch {
        throw new Error(`HARNESS_ALLOWED_ORIGINS contains an invalid origin: "${entry}"`);
      }
      if (isBlockedHost(allowed.hostname)) {
        throw new Error(`HARNESS_ALLOWED_ORIGINS may not include link-local/metadata host: "${entry}"`);
      }
      return (u) =>
        u.protocol === allowed.protocol &&
        u.hostname === allowed.hostname &&
        // If the configured origin pins a port, require an exact match; otherwise any port.
        (allowed.port === '' || u.port === allowed.port);
    });
}

/**
 * Parse and authorize a URL against the allowlist. Throws on anything not
 * explicitly permitted. Returns the parsed URL on success.
 */
export function assertAllowedUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') {
    throw new Error('A URL argument is required.');
  }

  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error(`Not a valid absolute URL: "${rawUrl}"`);
  }

  // Hard reject non-web schemes up front (file:, data:, ftp:, chrome:, about:, javascript:, ...).
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`Blocked scheme "${url.protocol}". Only http/https are allowed.`);
  }

  const rules = [...DEFAULT_RULES, ...extraOriginRules()];
  if (rules.some((rule) => rule(url))) {
    return url;
  }

  throw new Error(
    `Origin not allowed: "${url.origin}". The harness only navigates to localhost/loopback ` +
      `(any port), *.wercomfg.app, or origins explicitly listed in HARNESS_ALLOWED_ORIGINS.`
  );
}

// Inline, no-network schemes that sub-resources legitimately use (data: URIs,
// blob: objects, about:blank). These never leave the machine, so they're allowed
// for sub-requests even though they're rejected for the top-level entry URL.
const INLINE_REQUEST_SCHEMES = new Set(['data:', 'blob:', 'about:']);

/**
 * Authorize an INDIVIDUAL request (top-level navigation, redirect hop, or
 * sub-resource). Inline schemes pass; everything else must clear the same origin
 * allowlist as the entry URL. Throws if not allowed.
 */
export function assertAllowedRequest(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error(`Not a valid request URL: "${rawUrl}"`);
  }
  if (INLINE_REQUEST_SCHEMES.has(url.protocol)) return url;
  return assertAllowedUrl(rawUrl);
}

/**
 * Authorize a WebSocket handshake (ws:/wss:). WebSockets are an outbound channel
 * just like HTTP, so they get the SAME origin allowlist — we map ws->http and
 * wss->https and reuse it. Throws if not allowed.
 */
export function assertAllowedWebSocket(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error(`Not a valid WebSocket URL: "${rawUrl}"`);
  }
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    throw new Error(`Blocked WebSocket scheme "${url.protocol}". Only ws/wss are allowed.`);
  }
  const httpEquivalent = `${url.protocol === 'wss:' ? 'https:' : 'http:'}//${url.host}${url.pathname}`;
  assertAllowedUrl(httpEquivalent);
  return url;
}

/**
 * Enforce the allowlist on EVERY request the page makes — not just the entry URL.
 * This is what makes the harness redirect- and SSRF-safe: Playwright follows 30x
 * redirects and loads sub-resources internally, and each of those requests is
 * routed through here and aborted if it targets a disallowed origin. Blocked
 * requests are logged to stderr so the caller can see what was refused.
 *
 * @param {import('@playwright/test').BrowserContext} context
 */
export async function applyRequestGuard(context) {
  await context.route('**/*', async (route) => {
    const reqUrl = route.request().url();
    try {
      assertAllowedRequest(reqUrl);
    } catch (err) {
      console.error(`harness: blocked request to ${reqUrl} — ${err.message}`);
      return route.abort('blockedbyclient');
    }

    // Chase redirects ourselves (maxRedirects: 0) instead of letting the browser
    // auto-follow them. The browser does NOT route server-driven 30x hops through
    // this handler, so a plain route.continue() would let an allowed page bounce
    // the browser to an arbitrary host. By validating each Location before we
    // issue the next hop, no request ever leaves the machine for a disallowed
    // origin. The browser only ever sees the final, allowed response.
    try {
      let response = await route.fetch({ maxRedirects: 0 });
      for (let hops = 0; response.status() >= 300 && response.status() < 400 && hops < 10; hops++) {
        const location = response.headers()['location'];
        if (!location) break;
        const target = new URL(location, response.url()).href;
        assertAllowedRequest(target);
        response = await route.fetch({ url: target, maxRedirects: 0 });
      }
      return await route.fulfill({ response });
    } catch (err) {
      console.error(`harness: blocked redirect/request from ${reqUrl} — ${err.message}`);
      return route.abort('blockedbyclient');
    }
  });

  // WebSocket handshakes are a separate channel that context.route() does NOT
  // cover, so guard them too: a loaded page could otherwise open a socket to an
  // arbitrary host and exfiltrate. Allowed sockets are proxied to the server;
  // disallowed ones are closed before connecting. Older Playwright builds lack
  // routeWebSocket — degrade gracefully rather than crash.
  if (typeof context.routeWebSocket === 'function') {
    await context.routeWebSocket('**/*', (ws) => {
      try {
        assertAllowedWebSocket(ws.url());
        ws.connectToServer();
      } catch (err) {
        console.error(`harness: blocked websocket to ${ws.url()} — ${err.message}`);
        ws.close();
      }
    });
  }
}

/**
 * Sanitize a caller-supplied output name and resolve it to an absolute path that
 * is guaranteed to live inside ARTIFACTS_DIR. Traversal (../), absolute paths,
 * and odd characters are stripped/rejected.
 */
export function resolveArtifactPath(name, fallbackBase, ext) {
  const base = (name && String(name)) || fallbackBase;
  // Keep only safe characters; collapse everything else. This alone defeats
  // traversal, but we re-verify the resolved path below as defense-in-depth.
  const safe = base.replace(/[^A-Za-z0-9_-]/g, '_').replace(/^_+/, '').slice(0, 120) || fallbackBase;
  const filename = safe.endsWith(ext) ? safe : `${safe}${ext}`;
  const full = resolve(ARTIFACTS_DIR, filename);

  const root = ARTIFACTS_DIR.endsWith(sep) ? ARTIFACTS_DIR : ARTIFACTS_DIR + sep;
  if (full !== ARTIFACTS_DIR && !full.startsWith(root)) {
    throw new Error(`Refusing to write outside the artifacts directory: "${name}"`);
  }
  return full;
}

/**
 * Launch a locked-down headless Chromium and run `fn(browser)`. The browser is
 * always closed, and the whole operation is bounded by WALL_TIMEOUT_MS.
 *
 * @param {import('@playwright/test').BrowserType} chromium
 */
export async function withBrowser(chromium, fn) {
  // NOTE: we deliberately do NOT pass --no-sandbox. Chrome's sandbox stays on.
  const launchPromise = chromium.launch({ headless: true });

  let browser;
  let wallTimer;
  const wall = new Promise((_, reject) => {
    wallTimer = setTimeout(
      () => reject(new Error(`Harness wall-clock timeout after ${WALL_TIMEOUT_MS}ms`)),
      WALL_TIMEOUT_MS
    );
    // Don't let the timer itself keep the event loop alive.
    if (typeof wallTimer.unref === 'function') wallTimer.unref();
  });

  try {
    browser = await Promise.race([launchPromise, wall]);
    return await Promise.race([fn(browser), wall]);
  } finally {
    clearTimeout(wallTimer);
    // If launch lost the race, still make sure we close whatever resolved.
    const b = browser || (await launchPromise.catch(() => null));
    if (b) await b.close().catch(() => {});
  }
}

export { join };

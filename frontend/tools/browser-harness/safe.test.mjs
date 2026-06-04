// safe.test.mjs — hermetic safety tests for the browser harness.
//
// These cover the SAFETY behavior of safe.mjs: the default-deny origin
// allowlist (assertAllowedUrl) and the traversal-proof artifact path
// resolver (resolveArtifactPath). They are pure ESM, need no browser and
// no running dev server.
//
// Run from frontend/ with (quote the glob so the shell doesn't expand it):
//   node --test 'tools/browser-harness/*.test.mjs'
// or target this file directly:
//   node --test tools/browser-harness/safe.test.mjs
//
// We use Node's built-in test runner instead of Jest because this project's
// Jest config (jest.config.js) is jsdom + ts-jest scoped to src/**/*.{ts,tsx}.
// The harness is plain .mjs living outside src/, so node --test imports the
// real module directly with zero transform/config wrangling.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sep } from 'node:path';

import {
  assertAllowedUrl,
  assertAllowedRequest,
  assertAllowedWebSocket,
  resolveArtifactPath,
  ARTIFACTS_DIR,
  NAV_TIMEOUT_MS,
  WALL_TIMEOUT_MS,
} from './safe.mjs';

// ---------------------------------------------------------------------------
// assertAllowedUrl — origins that MUST be allowed
// ---------------------------------------------------------------------------

const ALLOWED = [
  'http://localhost:3000',
  'http://127.0.0.1:5173',
  'http://[::1]:8000',
  'https://wercomfg.app',
  'https://app.wercomfg.app',
  'http://localhost', // any/no port
  'http://localhost:9999', // any port
];

for (const raw of ALLOWED) {
  test(`assertAllowedUrl allows ${raw}`, () => {
    const url = assertAllowedUrl(raw);
    assert.ok(url instanceof URL, 'should return a parsed URL');
    // origin round-trips (host/scheme preserved)
    assert.equal(url.protocol, new URL(raw).protocol);
    assert.equal(url.hostname, new URL(raw).hostname);
  });
}

// ---------------------------------------------------------------------------
// assertAllowedUrl — origins/schemes that MUST be rejected
// ---------------------------------------------------------------------------

const REJECTED = [
  'https://example.com',
  'http://evil.com',
  'file:///etc/passwd',
  'data:text/html,x',
  'ftp://localhost', // loopback host, but disallowed scheme
  'https://notwercomfg.app.evil.com', // suffix trick — not *.wercomfg.app
  'javascript:alert(1)',
];

for (const raw of REJECTED) {
  test(`assertAllowedUrl rejects ${raw}`, () => {
    assert.throws(() => assertAllowedUrl(raw), Error);
  });
}

test('assertAllowedUrl rejects loopback-lookalike hostnames', () => {
  // wercomfg.app.evil.com / localhost.evil.com style: the trusted token is a
  // prefix, not the registrable domain.
  assert.throws(() => assertAllowedUrl('https://wercomfg.app.evil.com'), Error);
  assert.throws(() => assertAllowedUrl('http://localhost.evil.com'), Error);
});

test('assertAllowedUrl rejects empty / non-string / unparseable input', () => {
  assert.throws(() => assertAllowedUrl(''), Error);
  assert.throws(() => assertAllowedUrl(undefined), Error);
  assert.throws(() => assertAllowedUrl(null), Error);
  assert.throws(() => assertAllowedUrl(12345), Error);
  assert.throws(() => assertAllowedUrl('not a url'), Error);
});

// ---------------------------------------------------------------------------
// assertAllowedUrl — HARNESS_ALLOWED_ORIGINS env opt-in
// ---------------------------------------------------------------------------

test('HARNESS_ALLOWED_ORIGINS adds an explicitly trusted origin', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    // Not allowed by default...
    assert.throws(() => assertAllowedUrl('https://staging.example.com'), Error);

    process.env.HARNESS_ALLOWED_ORIGINS = 'https://staging.example.com';
    const url = assertAllowedUrl('https://staging.example.com');
    assert.equal(url.hostname, 'staging.example.com');

    // The baked-in default-deny still rejects everything else.
    assert.throws(() => assertAllowedUrl('https://example.com'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

test('HARNESS_ALLOWED_ORIGINS pins the port when one is configured', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'http://10.0.0.5:8080';
    // Exact pinned port is allowed.
    assert.ok(assertAllowedUrl('http://10.0.0.5:8080') instanceof URL);
    // A different port on the same host is NOT allowed.
    assert.throws(() => assertAllowedUrl('http://10.0.0.5:9090'), Error);
    assert.throws(() => assertAllowedUrl('http://10.0.0.5'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

test('HARNESS_ALLOWED_ORIGINS with no port allows any port on that host', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'https://staging.example.com';
    assert.ok(assertAllowedUrl('https://staging.example.com:8443') instanceof URL);
    assert.ok(assertAllowedUrl('https://staging.example.com') instanceof URL);
    // Protocol must still match.
    assert.throws(() => assertAllowedUrl('http://staging.example.com'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

test('HARNESS_ALLOWED_ORIGINS with an invalid entry throws on use', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'not-a-valid-origin';
    assert.throws(() => assertAllowedUrl('http://localhost:3000'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

// ---------------------------------------------------------------------------
// assertAllowedRequest — per-request guard (inline schemes + origin allowlist)
// ---------------------------------------------------------------------------

// Inline, no-network schemes legitimately used by sub-resources are allowed
// even though they're rejected as a top-level entry URL.
for (const raw of [
  'data:text/html,<p>hi</p>',
  'data:image/png;base64,iVBORw0KGgo=',
  'blob:http://localhost:3000/550e8400-e29b-41d4-a716-446655440000',
  'about:blank',
]) {
  test(`assertAllowedRequest allows inline scheme ${JSON.stringify(raw.slice(0, 32))}`, () => {
    const url = assertAllowedRequest(raw);
    assert.ok(url instanceof URL, 'should return a parsed URL, not throw');
  });
}

test('assertAllowedRequest defers to the origin allowlist for http/https', () => {
  // Allowed origins pass through.
  assert.ok(assertAllowedRequest('http://localhost:3000/api') instanceof URL);
  assert.ok(assertAllowedRequest('https://app.wercomfg.app/x') instanceof URL);
  // Disallowed origins are rejected exactly like assertAllowedUrl.
  assert.throws(() => assertAllowedRequest('https://example.com/track.js'), Error);
  assert.throws(() => assertAllowedRequest('http://evil.com/'), Error);
});

test('assertAllowedRequest rejects file:// and ftp:// even on a loopback host', () => {
  assert.throws(() => assertAllowedRequest('file:///etc/passwd'), Error);
  assert.throws(() => assertAllowedRequest('ftp://localhost/secret'), Error);
});

test('assertAllowedRequest rejects invalid / unparseable input', () => {
  assert.throws(() => assertAllowedRequest('not a url'), Error);
  assert.throws(() => assertAllowedRequest(''), Error);
  assert.throws(() => assertAllowedRequest(undefined), Error);
});

// ---------------------------------------------------------------------------
// Link-local / cloud-metadata guard — must NOT be whitelistable
// ---------------------------------------------------------------------------

test('HARNESS_ALLOWED_ORIGINS may not whitelist the cloud-metadata IP', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'http://169.254.169.254';
    // Both entry points must throw — the override is rejected outright, so the
    // metadata endpoint can never become reachable via the harness.
    assert.throws(() => assertAllowedUrl('http://169.254.169.254'), Error);
    assert.throws(() => assertAllowedRequest('http://169.254.169.254/latest/meta-data/'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

test('HARNESS_ALLOWED_ORIGINS may not whitelist any 169.254.x.x link-local host', () => {
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'http://169.254.1.1:8080';
    assert.throws(() => assertAllowedUrl('http://localhost:3000'), Error);
    assert.throws(() => assertAllowedUrl('http://169.254.1.1:8080'), Error);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

test('a blocked link-local entry does not poison a valid override in the same list', () => {
  // The whole override is rejected if ANY entry is link-local, so a poisoned
  // list fails closed rather than silently allowing the good entry.
  const saved = process.env.HARNESS_ALLOWED_ORIGINS;
  try {
    process.env.HARNESS_ALLOWED_ORIGINS = 'https://staging.example.com,http://169.254.169.254';
    assert.throws(() => assertAllowedUrl('https://staging.example.com'), Error);

    // A clean override (no link-local) still works.
    process.env.HARNESS_ALLOWED_ORIGINS = 'https://staging.example.com';
    assert.ok(assertAllowedUrl('https://staging.example.com') instanceof URL);
  } finally {
    if (saved === undefined) delete process.env.HARNESS_ALLOWED_ORIGINS;
    else process.env.HARNESS_ALLOWED_ORIGINS = saved;
  }
});

// ---------------------------------------------------------------------------
// resolveArtifactPath — traversal-proof confinement
// ---------------------------------------------------------------------------

const root = ARTIFACTS_DIR.endsWith(sep) ? ARTIFACTS_DIR : ARTIFACTS_DIR + sep;

for (const name of ['../../evil', '/etc/passwd', 'a/b/c']) {
  test(`resolveArtifactPath confines ${JSON.stringify(name)} inside ARTIFACTS_DIR`, () => {
    const full = resolveArtifactPath(name, 'fallback', '.png');
    assert.ok(
      full.startsWith(root),
      `expected ${full} to be inside ${root}`
    );
    // No path separators survive sanitization, so no nested dirs / escape.
    assert.ok(!full.slice(root.length).includes(sep), 'no traversal segments remain');
  });
}

test('resolveArtifactPath keeps a clean name and appends the ext', () => {
  const full = resolveArtifactPath('good-name', 'fallback', '.png');
  assert.equal(full, `${root}good-name.png`);
});

test('resolveArtifactPath sanitizes dots in the name before appending ext', () => {
  // The "." is not a safe character, so it is collapsed to "_" during
  // sanitization (this is part of the traversal-proofing). The resulting
  // name no longer ends in the ext, so the ext is appended.
  const full = resolveArtifactPath('already.png', 'fallback', '.png');
  assert.equal(full, `${root}already_png.png`);
});

test('resolveArtifactPath falls back when name is undefined', () => {
  const full = resolveArtifactPath(undefined, 'fallback-base', '.png');
  assert.equal(full, `${root}fallback-base.png`);
});

test('resolveArtifactPath falls back when sanitization empties the name', () => {
  // All characters get stripped -> empty -> fallbackBase.
  const full = resolveArtifactPath('../../', 'fallback-base', '.pdf');
  assert.equal(full, `${root}fallback-base.pdf`);
});

// ---------------------------------------------------------------------------
// Exported timeout constants — sane, finite, hard bounds
// ---------------------------------------------------------------------------

test('timeout constants are positive finite numbers', () => {
  assert.equal(typeof NAV_TIMEOUT_MS, 'number');
  assert.equal(typeof WALL_TIMEOUT_MS, 'number');
  assert.ok(Number.isFinite(NAV_TIMEOUT_MS) && NAV_TIMEOUT_MS > 0);
  assert.ok(Number.isFinite(WALL_TIMEOUT_MS) && WALL_TIMEOUT_MS > 0);
});

// ---------------------------------------------------------------------------
// assertAllowedWebSocket — ws/wss handshakes get the same origin allowlist
// ---------------------------------------------------------------------------

test('assertAllowedWebSocket allows ws/wss to allowed origins', () => {
  for (const u of ['ws://localhost:5173/', 'ws://127.0.0.1/hmr', 'wss://app.wercomfg.app/socket']) {
    assert.doesNotThrow(() => assertAllowedWebSocket(u), `expected allowed: ${u}`);
  }
});

test('assertAllowedWebSocket rejects sockets to disallowed hosts', () => {
  for (const u of ['ws://attacker.com/x', 'wss://169.254.169.254/', 'wss://evil.example/exfil']) {
    assert.throws(() => assertAllowedWebSocket(u), /Origin not allowed/, `expected blocked: ${u}`);
  }
});

test('assertAllowedWebSocket rejects non-ws schemes', () => {
  for (const u of ['http://localhost/', 'https://app.wercomfg.app/', 'file:///etc/passwd']) {
    assert.throws(() => assertAllowedWebSocket(u), /Blocked WebSocket scheme/);
  }
});

test('assertAllowedWebSocket rejects invalid input', () => {
  assert.throws(() => assertAllowedWebSocket('not a url'), /valid WebSocket URL/);
});

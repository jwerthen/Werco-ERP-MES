# Browser Harness Runbook

## What this is

A safe, fixed-surface CLI that lets subagents drive a headless Chromium against a
running app to capture screenshots, text snapshots, console/network logs, and PDFs.
It uses the Playwright already installed in `frontend/` (`@playwright/test`) — no new
dependencies. It is **read-only and unauthenticated** in v1, and exposes only four
fixed subcommands (no eval/arbitrary-JS surface).

It lives at `frontend/tools/browser-harness/` (`cli.mjs` + `safe.mjs`) and is invoked
through the `harness` npm script in `frontend/package.json`.

## Why it exists

It is a Bash CLI fallback to the `preview_*` tools the frontend-engineer uses for
in-IDE verification. Use it when the preview tools aren't available — for the
`landing/` marketing site, or to capture artifacts from any local service (Vite on
`5173`, backend on `8000`, etc.). It is **distinct from the Playwright E2E tests** in
`frontend/e2e/`: those assert behavior in CI; the harness just observes a running app.

## Safety model

The safety layer (`safe.mjs`) is what makes the harness safe for autonomous use:

- **Default-deny origin allowlist.** Only `http`/`https` to localhost/loopback
  (`localhost`, `127.0.0.1`, `[::1]`) on **any** port, plus `https` to `*.wercomfg.app`,
  are permitted. Every other host and every non-web scheme (`file:`, `data:`, `ftp:`,
  `chrome:`, …) is rejected **before** any navigation. Extra origins can be opted in
  via `HARNESS_ALLOWED_ORIGINS`; the baked-in defaults stay locked down.
- **Per-request enforcement (redirect / SSRF safe).** The allowlist is enforced on
  **every** request the page makes — the entry URL, each redirect hop, and every
  sub-resource — not just the first URL. Chromium does not route server-driven 30x
  redirects through the guard, so the harness chases redirects itself (`maxRedirects: 0`)
  and validates each `Location` before issuing the next hop. A page can therefore never
  bounce the browser to an arbitrary host, and no request ever leaves the machine for a
  disallowed origin (e.g. the `169.254.169.254` cloud-metadata endpoint). Link-local /
  metadata hosts (`169.254.0.0/16`) are rejected even when added via
  `HARNESS_ALLOWED_ORIGINS`.
- **Headless + sandbox on.** Chromium launches headless and the OS sandbox is left
  enabled — the harness never passes `--no-sandbox`.
- **Hard timeouts.** A per-navigation timeout (`HARNESS_NAV_TIMEOUT_MS`, default 15000)
  and a wall-clock timeout (`HARNESS_WALL_TIMEOUT_MS`, default 60000) bound every run.
  The browser is always closed in a `finally` block.
- **Sandboxed output.** Artifacts are written only to `frontend/.browser-harness/`
  (gitignored). `--out` names are sanitized to `[A-Za-z0-9_-]` and are
  path-traversal-proof.
- **WebSocket guard.** `ws:`/`wss:` handshakes are validated against the same origin
  allowlist (a separate channel `route()` does not cover); allowed sockets connect,
  disallowed ones are closed before connecting.
- **No eval surface.** Only the four fixed subcommands below exist; there is no way to
  inject arbitrary page script through the CLI.
- **Read-only / unauthenticated.** v1 does not log in or mutate state.

> **Third-party assets are blocked by default.** Because every sub-resource must clear
> the allowlist, external assets a page pulls from other origins — Google Fonts, CDN
> scripts, analytics — are refused, so a screenshot may render with fallback fonts. This
> is intentional (default-deny). To capture them faithfully, opt the specific origins in,
> e.g. `HARNESS_ALLOWED_ORIGINS=https://fonts.googleapis.com,https://fonts.gstatic.com`.

## Commands

Invoke through npm from `frontend/` (note the `--` separating npm args from harness args):

```bash
# Screenshot the running SPA (Vite dev server serves on 5173)
npm run harness -- screenshot http://localhost:5173

# Full-page screenshot with a custom output name
npm run harness -- screenshot http://localhost:5173 --full --out dashboard

# Page title + visible text to stdout
npm run harness -- snapshot http://localhost:5173

# Console messages + failed / 4xx-5xx responses to stdout
npm run harness -- logs http://localhost:5173

# Print-to-PDF (Chromium)
npm run harness -- pdf https://www.wercomfg.app --out landing
```

| Command | Arguments | Output |
|---------|-----------|--------|
| `screenshot <url>` | `[--full]` `[--out <name>]` | PNG (viewport, or full page with `--full`) to `.browser-harness/` |
| `snapshot <url>` | — | Page title + visible text to stdout |
| `logs <url>` | — | Console messages + failed/4xx-5xx responses to stdout |
| `pdf <url>` | `[--out <name>]` | PDF to `.browser-harness/` |

> Note: the Vite dev server serves on `5173`, even though `playwright.config.ts`
> `baseURL` says `3000`. Point the harness at the port the dev server actually uses.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HARNESS_ALLOWED_ORIGINS` | (none) | Comma-separated extra origins to allow, e.g. `https://staging.example.com,http://10.0.0.5:8080`. A pinned port requires an exact match; omit the port to allow any. Adds to — never replaces — the built-in allowlist. |
| `HARNESS_NAV_TIMEOUT_MS` | `15000` | Per-navigation timeout in milliseconds. |
| `HARNESS_WALL_TIMEOUT_MS` | `60000` | Wall-clock timeout for the whole run in milliseconds. |

## Relationship to other tooling

- **`preview_*` tools** — the primary path for frontend verification in the IDE. The
  harness is the Bash CLI fallback when those tools aren't available.
- **Playwright E2E (`frontend/e2e/`, `npm run test:e2e`)** — automated behavioral tests
  run in CI. The harness is for ad-hoc observation, not assertions.

# Security Advisory Posture (CI `Security Scanning`)

This document records how the CI `Security Scanning` job
(`.github/workflows/ci-cd.yml`) treats dependency advisories, and the
justification for any gate that is not hard-blocking.

## Policy

- Prefer fixing over tolerating: apply any patch/minor upgrade that clears an
  advisory before accepting it.
- **Frontend `npm audit` is a HARD gate** (must pass) — run through the
  allowlist-aware wrapper `frontend/scripts/audit-check.mjs`. Any high/critical
  advisory fails CI unless it carries a documented not-applicable entry.
- **Backend `pip-audit` is ADVISORY** (non-blocking, `continue-on-error: true`)
  — see below.

## Backend (`pip-audit`) — advisory, not blocking

The backend scan step runs `pip-audit -r requirements.txt -r requirements-dev.txt`
(PyPA/OSV database). It is **scoped to this app's resolved dependency set** via
the `-r` flags, preceded by an `actions/setup-python` step.

This replaced the deprecated `safety check`. The old step ran `pip install
safety; safety check` with **no `-r` and no prior install of `requirements.txt`**,
and the job had no `setup-python` — so it scanned the **GitHub runner's ambient
system Python** (the runner image's pre-installed packages + safety's own
dependency tree), not this app's dependencies. That produced ~44 phantom
advisories across ~15 packages the app never installs (e.g. `configobj 5.0.8`,
stale runner-bundled `cryptography 41.0.7`, `pyopenssl 23.2.0`, `requests
2.31.0`). The new invocation scans the real dependency tree, so its output is
meaningful instead of noise. **Keep the `-r` flags** — dropping them regresses to
scanning the ambient runner env.

The step does **not** fail the job (`continue-on-error: true`). Rationale: the
public advisory database is a daily-moving target — new CVEs are published
continuously against already-pinned versions — so a hard gate makes every
unrelated PR's CI flap red for reasons outside that PR's scope. The scan output
stays visible in the job log as an informational signal. This advisory posture is
unchanged from the `safety` era; only the scan target and tool changed.

Backend dependency-CVE remediation is handled deliberately as **ongoing security
hygiene** (tracked separately), not as a per-PR blocker: bump affected packages on
a reviewed cadence, run the full backend test suite, and document anything that
cannot be safely upgraded here.

### Remediated CVEs (surfaced by pip-audit / OSV)

The switch to `pip-audit` surfaced two genuine app-dependency CVEs that safety's
database had missed; both are fixed:

- **`python-multipart` 0.0.26 → 0.0.27** — fixes **CVE-2026-42561**.
- **`starlette` 0.52.1 → 1.2.1** — fixes **CVE-2026-48710 /
  GHSA-86qp-5c8j-p5mr / PYSEC-2026-161** ("BadHost": Starlette did not validate the
  HTTP Host header, letting an attacker inject path segments into the host portion
  and poison `request.url.path`, bypassing path-based security checks). This
  required bumping **FastAPI 0.128.4 → 0.136.3**, because FastAPI only dropped its
  `starlette<1.0.0` cap at 0.133.0 (0.134.0+ requires `starlette>=0.46.0`).
  `starlette==1.2.1` is now pinned explicitly in `requirements.txt`.
  - **Application-level defense-in-depth (added on top of the upgrade):**
    `TrustedHostMiddleware` is registered **outermost** in `app/main.py` with an
    explicit `Host`-header allowlist via the `ALLOWED_HOSTS` setting (default `*`
    = validation disabled for dev; set explicit hosts in production). A request
    whose `Host` is not allowlisted is rejected with **HTTP 400** before any
    path-based security logic (CSRF exemptions, rate-limit selection, the
    read-only platform-admin write guard) runs. See
    [Trusted Hosts](ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header).

Validation: full backend suite **388 passed**, mypy clean (194 files), app boots,
and `pip-audit` on the resolved environment reports **"No known vulnerabilities
found"**.

### Background: ecdsa (IDs 64459 / 64396, CVE-2024-23342)

`ecdsa` (transitive via `python-jose[cryptography]`) has "Minerva" timing
side-channel advisories with **no fixed release** (`Affected spec: >=0`). **The
active scanner no longer flags it** — pip-audit/OSV does not report `ecdsa`, so
there is **no active suppression** and no `--ignore` is needed. Retained here as
rationale for why it would be safe to ignore *if* it were flagged: this app
signs/verifies JWTs with **HS256 (HMAC) exclusively** (`app/core/config.py`
`ALGORITHM="HS256"`, `app/core/security.py`); the ECDSA (ES256/384/512) code path
is never used. Revisit only if we adopt an EC JWT algorithm or remove
`python-jose`.

## Frontend (`npm run audit:ci`) — hard gate, allowlist-aware

The CI step `Run npm audit (Frontend)` runs `npm run audit:ci` →
`node scripts/audit-check.mjs` (was: a bare `npm audit --audit-level=high`).

**It is still a hard gate.** Any **high** or **critical** advisory fails the job
(exit 1) unless its GHSA id is listed in `frontend/scripts/audit-allowlist.json`.
The wrapper exists so that one documented, non-applicable advisory cannot
red-line every unrelated PR, while a genuinely new high/critical still blocks.

Properties worth knowing:

- **No npm dependency.** Plain Node ESM — a security gate should not add
  supply-chain surface. Needs only Node + `package-lock.json`; **no `npm ci`**
  (npm audit resolves the tree from the lockfile), hence the job installs nothing.
- **Fails closed.** A failed/unparseable audit, a registry error, or a finding
  whose advisory id cannot be resolved is a FAILURE, never a silent pass.
- **Resolves transitive advisories.** `vulnerabilities[].via[]` holds either
  advisory objects or *strings* naming another vulnerable package. Both are
  walked (deduped, cycle-guarded). This is load-bearing: `react-router-dom` has
  `via: ["react-router"]` and no advisory object of its own.
- **Stale entries warn, never fail.** An allowlist entry matching no current
  advisory prints a non-fatal `WARNING` telling you to delete it. There is
  **deliberately no time-based expiry** — this gate already goes red with no code
  change when advisories publish; a second surprise-failure mechanism would be
  worse than the problem. `reviewed` is a human review date, not an enforced one.

### Running it locally

```bash
cd frontend && npm run audit:ci     # identical to CI
```

### Adding an allowlist entry

Only after confirming no non-breaking upgrade clears the advisory. Add to the
`advisories` array in `frontend/scripts/audit-allowlist.json`:

| field | purpose |
| --- | --- |
| `id` | GHSA id, exactly as in the advisory URL (required) |
| `package` / `severity` / `title` / `url` | identification for the reviewer |
| `reason` | **the justification** — string or array of lines (required) |
| `remove_when` | the condition that retires the entry |
| `reviewed` | date last reviewed (informational) |

**The `reason` rule:** it must be a concrete, checkable argument that the
vulnerable code path *does not exist in this app* — which feature the CVE
requires and the evidence we never use it. "It is noisy", "no fix available", or
"the upgrade is a big migration" are **not** acceptable reasons on their own; if
a real advisory applies to us, the answer is to fix it or accept a red build.
A reviewer must be able to judge the suppression on sight.

### Removing one

Delete the entry when the advisory is fixed (or when the run warns it is stale)
and re-run `npm run audit:ci`.

### Current suppressions

- **GHSA-qwww-vcr4-c8h2** (`react-router`, high) — "RSC Mode CSRF Bypass Allows
  Action Execution Before 400 Response", vulnerable `>=7.12.0 <8.3.0`.
  Reachable **only in React Router's RSC (React Server Components) mode**. This
  app has no RSC and no server in front of the router — verified: no
  `react-router/rsc` / `unstable_RSC` / `RSCErrorHandler` imports; no
  `createStaticHandler` / `StaticRouter` / `renderToString` /
  `renderToPipeableStream`; no `express` / `@react-router/node` /
  `@react-router/serve`; a plain client-side `<BrowserRouter>` in
  `frontend/src/App.tsx`; and a client-only `vite build` with no ssr config.
  No server action exists to execute, so there is no CSRF boundary to bypass.
  Also flagged transitively on `react-router-dom`.
  *Remove when* react-router reaches `>=8.3.0` — which requires the v8 migration
  that **drops `react-router-dom`** (no v8 of that package exists; it folds into
  `react-router`), i.e. rewriting imports across ~59 pages.

> **Never run `npm audit fix --force` here.** It resolves `react-router-dom`
> **down** to 7.11.0 and reintroduces four advisories patched in 7.18.0.

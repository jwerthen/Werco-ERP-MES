# Security Advisory Posture (CI `Security Scanning`)

This document records how the CI `Security Scanning` job
(`.github/workflows/ci-cd.yml`) treats dependency advisories, and the
justification for any gate that is not hard-blocking.

## Policy

- Prefer fixing over tolerating: apply any patch/minor upgrade that clears an
  advisory before accepting it.
- **Frontend `npm audit` is ADVISORY on PRs, BLOCKING nightly** (changed
  2026-07-28) — run through the allowlist-aware wrapper
  `frontend/scripts/audit-check.mjs`. The same command runs in two places:
  `continue-on-error` inside ci-cd.yml's `Security Scanning` job, and
  hard-failing in `.github/workflows/dependency-audit.yml` on a nightly
  schedule. See "Why the frontend gate moved" below.
- **Backend `pip-audit` is ADVISORY** on PRs (non-blocking,
  `continue-on-error: true`) and blocking in the same nightly workflow — see below.
  Exactly one advisory is suppressed (`ecdsa` / PYSEC-2026-1325) via an explicit
  `--ignore-vuln` flag on the command. **There is no backend allowlist file** —
  the flag plus this document is the whole record.

## Known open advisories (as of 2026-07-29)

The first run of the new nightly `dependency-audit.yml` surfaced six backend
advisories that were already present on `main` — invisible until now because
`pip-audit` had only ever run as `continue-on-error`. Both packages in that first
run are now settled: **`pypdf` is fixed** (6.10.2 → 6.14.2, see Remediated below)
and **`ecdsa` / PYSEC-2026-1325 is an accepted, documented suppression** (see
below). That is the gate working, not a misconfiguration.

**The nightly is still red, and the `pypdf` fix did not change that.** The CI
job itself reports **`Found 10 known vulnerabilities, ignored 1 in 4 packages`**
— the suppressed `ecdsa` plus four *other* packages flagged since that first
run. Do not read "pypdf fixed" as "nightly green".

| Package | Pinned | Advisories | Fixed in |
|---|---|---|---|
| `starlette` | 1.2.1 | PYSEC-2026-248, PYSEC-2026-249 | 1.3.0 / 1.3.1 |
| `python-multipart` | 0.0.27 | PYSEC-2026-3036, PYSEC-2026-3037, PYSEC-2026-3040 | 0.0.30 / 0.0.31 |
| `bleach` | 6.3.0 | GHSA-8rfp-98v4-mmr6, GHSA-gj48-438w-jh9v; GHSA-g75f-g53v-794x | 6.4.0 / — |
| `pydantic-settings` | 2.12.0 | GHSA-4xgf-cpjx-pc3j | 2.14.2 |

New advisories published against already-pinned versions is the normal behavior
of a moving database, not a regression on `main` — it is the same reason the PR
gates are advisory and the nightly is where the red lands.

> **Take the counts from a CI run, not from a local audit.** `pip-audit -r` is
> scoped to what the two requirements files actually declare. Auditing a local
> environment instead — even a freshly built one — picks up packages that are
> merely *present*, and reports advisories the gate will never show you.
> `setuptools` 79.0.1 / PYSEC-2026-3447 is the live example: it appears in a
> local venv as build tooling, it is pinned in **neither** requirements file, and
> it is **absent** from the CI job's output. It is not part of this app's audited
> surface and needs no bump. (Auditing `backend/.venv311` directly is worse
> still — it has drifted and carries dev extras, and reports roughly three times
> the real finding count.)

**These four will be fixed, not suppressed, and they are out of scope for this
change.** Every one of them except `bleach`'s GHSA-g75f-g53v-794x has a fix
available, and this file's own [`reason` rule](#adding-an-allowlist-entry) rejects
"no fix available" and "it is noisy" as justifications on their own. An advisory
with a fix gets the fix. Treat them as the next hygiene pass, each with its own
verification — a batch bump behind one green suite is exactly how a silent
behavior change ships:

- **`starlette` 1.2.1 → 1.3.x** is the same class of upgrade that already forced
  the FastAPI bump documented below (0.128.4 → 0.136.3, because FastAPI capped
  `starlette<1.0.0` until 0.133.0). Check FastAPI's current cap *before* pinning.
- **`python-multipart`** sits on the file-upload path — every multipart endpoint
  in the app parses through it. Exercise the upload paths, not just the suite.
- **`bleach`** is **not** dev-only: it is pinned in `requirements.txt` (not
  `requirements-dev.txt`) and `app/core/sanitization.py` does
  `from bleach import clean`, so it is shipped input-sanitization surface. What
  still needs determining is whether the flagged code paths are the ones we
  call, and what to do about GHSA-g75f-g53v-794x, which has no fix.
  (`setuptools` used to be listed here; it is not in the gate's scope at all —
  see the note above.)

## Backend (`pip-audit`) — advisory on PRs, blocking nightly

The backend scan step runs `pip-audit -r requirements.txt -r requirements-dev.txt
--ignore-vuln PYSEC-2026-1325` (PyPA/OSV database). It is **scoped to this app's
resolved dependency set** via the `-r` flags, preceded by an
`actions/setup-python` step; the `--ignore-vuln` id is the one accepted
suppression, justified below.

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

**How a backend suppression is enforced.** An accepted advisory is silenced with
`--ignore-vuln <ID>` on the `pip-audit` command — currently
`--ignore-vuln PYSEC-2026-1325` — in **both** places it runs: the blocking
nightly in `dependency-audit.yml` and the advisory copy in `ci-cd.yml`. Keep the
two in sync; a flag on only one of them means the nightly and the PR scan
disagree about what is acceptable.

**There is no backend allowlist file, and that is a real gap to work around.**
The frontend resolves suppressions through `frontend/scripts/audit-check.mjs` +
`audit-allowlist.json`, which carries structured `reason` / `remove_when` /
`reviewed` fields per entry and prints a warning when an entry goes stale.
`pip-audit` has no equivalent — it takes a bare id on the command line. So a
backend suppression is exactly two things: the workflow flag, and its entry in
this document. It carries no justification of its own and it will never tell you
it has gone stale. **An id in `--ignore-vuln` with no entry here is undocumented
by construction — add both or neither.**

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

Validation (2026-07-28, covering the two bumps above): full backend suite
**388 passed**, mypy clean (194 files), app boots, and `pip-audit` on the resolved
environment reported **"No known vulnerabilities found"**. Those numbers and that
clean result belong to that change and are not a current claim — see "Known open
advisories" for today's state.

The nightly gate then surfaced a third package, since fixed:

- **`pypdf` 6.10.2 → 6.14.2** (2026-07-29) — clears **GHSA-jm82-fx9c-mx94** and
  **CVE-2026-59935 / 59936 / 59937 / 59938**, the whole set from the nightly's
  first run. `pypdf` no longer appears in pip-audit output at all. Prioritized
  ahead of the rest of the backlog because this app parses **attacker-supplied**
  PDFs — RFQ package upload, laser-nest PDF import, PO document upload, QMS
  standard upload — so a PDF-parser advisory is reachable from untrusted input
  rather than theoretical.
  - **Four minor versions, no API break.** 6.11.0–6.14.2 are security hardening
    end to end: infinite-loop guards for inline images / outlines / text
    extraction, `MAX_DECLARED_STREAM_LENGTH` extended to streams *without* a
    declared length, XMP size and element-count limits, a general requested-
    image-size limit, and multi-hop cyclic `/Pages` detection. Nothing in the
    public API was renamed, removed, or deprecated. The one behavior change —
    6.12.0's "avoid excessive whitespace in **layout mode** text extraction" —
    does not apply here: every call site uses a bare `page.extract_text()`, and
    the app never uses layout mode.
  - **Validated against the real consumers with real PDFs.** The checks build
    text-bearing PDFs with reportlab rather than pypdf-built blank pages — a
    blank page proves nothing about extraction — and run them through
    `pdf_service._extract_native_text`, the `qms_standards` inline
    `PdfReader(io.BytesIO(...))` + 50-char floor, and
    `laser_nest_pdf_split_service.get_pdf_page_count` / `split_pdf_segments`,
    asserting segment text **survives the split** because those segments are
    what feed AI extraction. 11/11 passed — and the same checks produced
    **byte-identical output on 6.10.2 and 6.14.2**: same extracted character
    counts, same resource-amplification ratio. The hardening changed no output
    this app depends on.
  - **Validation:** full backend suite **3757 passed, 2 xfailed**, coverage
    80.97%; `black` / `isort` / `flake8` / `mypy` (325 files) all clean. New
    regression coverage lives in
    `backend/tests/services/test_pypdf_real_text_extraction.py`.
  - **Post-change scan, for the record:** the `pip-audit (Backend)` job reports
    `Found 10 known vulnerabilities, ignored 1 in 4 packages` — `pypdf` absent,
    `ecdsa` suppressed, and the four packages above remaining.

### Current backend suppression: ecdsa / PYSEC-2026-1325 ("Minerva", CVE-2024-23342)

**This is the same defect this file previously carried as a "Background" note
under safety IDs 64459 / 64396** — the P-256 "Minerva" timing side-channel in
`ecdsa` (transitive via `python-jose[cryptography]`). No code changed; what
changed is that
pip-audit/OSV **now flags it**, under the new id **PYSEC-2026-1325**. The claim
that previously stood here — that the active scanner no longer flags it, so there
is no active suppression — is false as of 2026-07-29 and is replaced by this
entry. Enforced with `--ignore-vuln PYSEC-2026-1325`.

- **Affected: `ecdsa` >= 0.6 through 0.19.2 — i.e. every release. No fixed
  version exists and none is planned**; upstream considers side-channel
  resistance out of scope. This is the one advisory shape the `reason` rule
  cannot dispose of by upgrading, so it has to be argued on reachability.
- **What the defect touches:** the vulnerable API is
  `ecdsa.SigningKey.sign_digest()`. It affects signature *generation*, key
  generation, and ECDH — **not** verification.
- **Transitive only:** `pip show ecdsa` → `Required-by: python-jose`, pulled by
  `python-jose[cryptography]==3.5.0`. Nothing in `app/` imports it.
- **The module is never even imported — this is the load-bearing part.**
  `jose/backends/__init__.py` reaches `from jose.backends.ecdsa_backend import
  ECDSAECKey as ECKey` *only* inside an `except ImportError:` fallback for the
  cryptography backend. `cryptography>=46.0.4` is pinned **directly** in
  `requirements.txt`, independently of the jose extra, so that fallback can never
  fire. Verified at runtime: `jose.backends.ECKey` resolves to
  `jose.backends.cryptography_backend.CryptographyECKey`, and after
  `import jose.jwt` the string `'ecdsa'` is **not in `sys.modules`**.
- **Layered on top, the algorithm argument** — unchanged, and still the core of
  it: this app signs and verifies JWTs with **HS256 (HMAC) exclusively**.
  `app/core/config.py` declares `ALGORITHM: str = "HS256"`, `app/core/security.py`
  is the single `from jose import ...` site, and every encode/decode passes
  `settings.ALGORITHM`. There is **zero** `ES256` / `ES384` / `ES512` / `ECDSA` /
  `SECP256` usage anywhere in `app/`. Even if the module were loaded, no EC
  signing path is reachable.
- **This rationale is executable, not prose.**
  `backend/tests/test_jose_ec_backend_guard.py` asserts that the cryptography
  backend won, that `ALGORITHM == "HS256"`, and — in a hermetic subprocess — that
  importing the app's real jose surface loads no `ecdsa` module. Dropping the
  `[cryptography]` extra, unpinning `cryptography`, or switching to an EC
  algorithm **fails that test loudly** instead of silently inheriting this
  suppression. That guard is why this entry is safe to leave standing; don't
  delete it while the suppression stands.
- **One honest caveat:** `ALGORITHM` is a `Settings` field, so it is
  env-overridable. Setting it to an `ES*` value would put EC signing on the live
  auth path, and no CI test can see a production env var. What the guard covers is
  the *code* drifting there — a changed default, a dropped extra, an added EC
  algorithm. The env override is a decision someone has to make deliberately, and
  this entry is the reason not to make it.

*Revisit only if* we adopt an EC JWT algorithm or remove `python-jose`.

## Frontend (`npm run audit:ci`) — allowlist-aware, advisory on PRs

The CI step `Run npm audit (Frontend)` runs `npm run audit:ci` →
`node scripts/audit-check.mjs` (was: a bare `npm audit --audit-level=high`).

**The command still hard-fails** — any **high** or **critical** advisory exits 1
unless its GHSA id is listed in `frontend/scripts/audit-allowlist.json`. What
changed on 2026-07-28 is *where that failure blocks*.

### Why the frontend gate moved

The `Security Scanning` job sits in the `needs` chain of `deploy-production`.
While this step was hard-blocking there, a newly-published advisory could
red-line unrelated PRs **and** block production deploys with zero code change.
That happened twice (axios, 2026-07-20, PR #138; react-router). The advisory
database is a daily-moving target and does not belong on the critical path of
shipping unrelated work — the same reasoning that already made `pip-audit`
advisory.

So the gate moved rather than disappeared:

| Where | Behavior |
|---|---|
| `ci-cd.yml` → `Security Scanning` | runs, reports, `continue-on-error: true` — never blocks a PR or a deploy |
| `dependency-audit.yml` (nightly 08:00 UTC + manual + on audit-tooling PRs) | same command, **hard-fails** |

A red nightly run is the real signal: triage it by upgrading the dependency or
adding a justified allowlist entry. To restore PR-blocking behavior, delete
`continue-on-error` from the ci-cd.yml step.

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

- **GHSA-mh99-v99m-4gvg** (`brace-expansion`, high) — "DoS via unbounded
  expansion length causing an out-of-memory process crash", vulnerable `<=5.0.7`.
  **Development-only transitive dependency — it ships in nothing.** Verified:
  `npm ls brace-expansion --all --omit=dev` resolves to **empty**. Every path in
  is lint/test tooling — eslint 9.x and `@eslint/config-array` / `@eslint/eslintrc`,
  `eslint-plugin-react`, `eslint-plugin-jsx-a11y` (all via `minimatch` 3.x),
  `@typescript-eslint/*` (via `minimatch` 9.x), and
  `jest` / `ts-jest` / `@jest/transform` / `test-exclude` / `babel-plugin-istanbul`.
  The `vite build` client bundle contains none of it.
  Exploitation needs an **attacker-controlled** glob fed to minimatch; the only
  patterns these tools expand are the repo's own developer-authored globs in
  `eslint.config` / `jest.config` / npm scripts — no runtime, user, or request
  input reaches them. CI does run eslint/jest on PR branches, so a PR author can
  influence those globs, but that is not an escalation: a PR that can edit
  `jest.config` can already exhaust the runner with an infinite loop. Blast radius
  is a crashed ephemeral CI job, never the shipped app or production data.
  *Remove when* any consumer upgrade pulls `brace-expansion >= 5.0.8`.

> **Do not add a blanket `brace-expansion` override.** `5.0.8` is the only patched
> release, and its API is incompatible with the `minimatch` 3.x that eslint's
> plugin set pins. Tested: `overrides: { "brace-expansion": "^5.0.8" }` does clear
> the advisory, but `npm run lint` then dies with
> `TypeError: expand is not a function` in `@eslint/config-array`. npm's own
> `fixAvailable` suggestions for this advisory are semver-major and include a
> nonsensical **downgrade to jest 25**.

> **Never run `npm audit fix --force` here.** It resolves `react-router-dom`
> **down** to 7.11.0 and reintroduces four advisories patched in 7.18.0.

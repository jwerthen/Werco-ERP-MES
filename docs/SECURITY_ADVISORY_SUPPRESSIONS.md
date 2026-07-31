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

## Known open advisories (as of 2026-07-30)

**None outstanding.** Every backend advisory the nightly has surfaced is now
fixed by upgrade — or, in `bleach`'s case, by **deleting the dependency** — rather
than suppressed, with the single argued exception of `ecdsa` / PYSEC-2026-1325,
which has no fixed version in any release and is justified on reachability
further down.

That took three passes, all recorded under Remediated below. The first run of the
nightly `dependency-audit.yml` surfaced six advisories that were already present
on `main` — invisible until then because `pip-audit` had only ever run as
`continue-on-error` — which settled into `pypdf` (fixed, 6.10.2 → 6.14.2) and
`ecdsa` (suppressed). Four more packages were flagged against already-pinned
versions in the days after, and those were cleared together on 2026-07-30:
`starlette`, `python-multipart`, `bleach`, and `pydantic-settings`. The gate
finding all of this was the gate working, not a misconfiguration.

**`bleach` is no longer a dependency of this app** (removed 2026-07-30, later the
same day as its 6.4.0 bump). It is out of pip-audit's scope entirely, and the
`bleach` entries below are kept as history — see
[bleach removed](#bleach-removed--escape-at-the-sink-2026-07-30) for the current
state.

**The nightly is GREEN — confirmed by a CI run, not inferred.** A manual
`workflow_dispatch` of `dependency-audit.yml` against the bump branch
([run 30561938722](https://github.com/jwerthen/Werco-ERP-MES/actions/runs/30561938722),
2026-07-30) reports:

```
No known vulnerabilities found, 1 ignored
```

Both jobs passed — `pip-audit (Backend)` and `npm audit (Frontend)`. The "1
ignored" is `ecdsa` / PYSEC-2026-1325, the single documented suppression below.

Worth recording *why* a CI run was needed rather than the local number: the local
resolve reported `Found 2 known vulnerabilities, ignored 1 in 1 package`, the
extra package being `setuptools` 79.0.1 / PYSEC-2026-3447 — which is declared in
neither requirements file and so is invisible to the gate's `-r` scope. The local
audit and the real gate disagreed, exactly as the note below warns, and the gate
is the one that counts.

> **Take the counts from a CI run, not from a local audit.** `pip-audit -r` is
> scoped to what the two requirements files actually declare. Auditing a local
> environment instead — even a freshly built one — picks up packages that are
> merely *present*, and reports advisories the gate will never show you.
> `setuptools` 79.0.1 / PYSEC-2026-3447 is the live example: it appears in a
> local venv as build tooling, it is pinned in **neither** requirements file, and
> it is **absent** from the CI job's output. It is not part of this app's audited
> surface and needs no bump. This was demonstrated empirically on PR #168, where
> the CI job reported findings across **4** packages while the same resolve in a
> local venv reported **5** — `setuptools` was the entire difference. (Auditing
> `backend/.venv311` directly is worse still — it has drifted and carries dev
> extras, and reports roughly three times the real finding count.)

Expect this section to have entries again. New advisories published against
already-pinned versions is the normal behavior of a moving database, not a
regression on `main` — it is the same reason the PR gates are advisory and the
nightly is where the red lands. The standing rule when they land: **an advisory
with a fix gets the fix.** This file's own [`reason` rule](#adding-an-allowlist-entry)
rejects "no fix available" and "it is noisy" as justifications on their own, and a
batch bump behind one green suite is exactly how a silent behavior change ships —
verify each package on its own terms, against the code paths this app actually
calls.

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
  This is where `starlette` became an explicit pin in `requirements.txt` (it is
  `1.3.1` today — see below).
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
  - **Post-change scan, for the record:** the `pip-audit (Backend)` job reported
    `Found 10 known vulnerabilities, ignored 1 in 4 packages` — `pypdf` absent,
    `ecdsa` suppressed, and four other packages still outstanding — the four
    cleared next.

The remaining four were then cleared together (2026-07-30). **All four are
pin-only — zero code changes** — and neither `pydantic` (stays **2.12.5**) nor
`fastapi` (stays **0.136.3**) had to move with them: pydantic-settings 2.14.2
requires only `pydantic>=2.7.0`, identical to 2.12.0's requirement, and FastAPI
0.136.3 requires `starlette>=0.46.0` with **no upper cap**. `pip check` clean.

- **`starlette` 1.2.1 → 1.3.1** — clears **PYSEC-2026-249 / CVE-2026-54283** and
  **PYSEC-2026-248 / CVE-2026-54282**. Unlike the 0.52.1 → 1.2.1 bump above, this
  one did **not** cascade into FastAPI; the `starlette<1.0.0` cap is long gone.
  - **This is the one behavior change in the batch, and it is worth knowing.**
    1.3.1 newly **enforces** form limits on `application/x-www-form-urlencoded`.
    That *is* PYSEC-2026-249 (CVSS 7.5, availability): `max_fields` and
    `max_part_size` were honored for multipart but **silently ignored** for
    urlencoded, because `Request.form()` built `FormParser(headers, stream)` with
    no limits. FastAPI calls `await request.form()` with no arguments, so the app
    inherits Starlette's defaults either way.
  - **Reachable pre-auth — this justifies the bump on its own.**
    `POST /api/v1/auth/login` is unauthenticated and takes
    `OAuth2PasswordRequestForm` (`app/api/endpoints/auth.py:98`), i.e. an
    unbounded urlencoded parse that runs before any credential check. The route's
    5/min per-IP limit (`AUTH_RATE_LIMITS` in `app/main.py`) caps *how often* that
    parse happens; it does not bound a single oversized body.
  - **Boundaries measured by A/B-running both versions against the real app**,
    not read off a changelog:
    - urlencoded **field count** — ≤1000 unchanged, >1000 → **400** (new)
    - urlencoded **single field size** — <1 MiB unchanged, ≥1 MiB → **400** (new)
    - **multipart** — bit-for-bit unchanged at every size; 1500 files → 400 on
      *both* versions, confirming those limits already existed in 1.2.1
  - **No legitimate traffic is affected, and that was verified rather than
    assumed.** The frontend has exactly **one** urlencoded body sender — the login
    call at `frontend/src/services/api.ts:514`; every other `URLSearchParams` in
    the codebase builds a query string. The backend has exactly **one**
    `OAuth2PasswordRequestForm`. Two fields: `username` and `password`.
  - **PYSEC-2026-248 / CVE-2026-54282** is authority confusion when rebuilding
    `request.url` from a path lacking a leading `/`. It gets a mention because
    this app keys ~9 security decisions off `request.url.path` (CSRF exemptions,
    the carrier-webhook body-cap skip, rate-limit selection, the kiosk-scope path
    fence in `api/deps.py`, the read-only platform-admin write guard). Triggering
    it needs an ASGI server that delivers a path not starting with `/`, which
    uvicorn/gunicorn do not do for HTTP/1.1 — so it was defense-in-depth here, not
    a live exposure. It is the residual of the same family as CVE-2026-48710
    above, the reason `TrustedHostMiddleware` exists.

- **`python-multipart` 0.0.27 → 0.0.31** — clears **PYSEC-2026-3036** and
  **PYSEC-2026-3037** (fixed in 0.0.30) plus **PYSEC-2026-3040** (0.0.31). This is
  the file-upload path: every multipart endpoint in the app parses through it, so
  the A/B harness exercised real uploads rather than trusting the suite alone.
  - **One new numeric limit in the whole range:** `MAX_BOUNDARY_LENGTH = 256`,
    enforced in `MultipartParser.__init__`, on the live upload path. Measured
    against real clients: httpx 32 bytes, Chrome/Edge 37, Safari 38, Firefox 41 —
    roughly 6× headroom. Boundary length is client-chosen and unrelated to payload
    size, so the multi-MB RFQ/nest PDFs and ZIP nest packages are unaffected.
  - **The multipart header limits (8 headers / 4224 bytes) are *not* new** — they
    shipped in 0.0.27 and have been live since.
  - **Body-size gates are unchanged by this bump:** the 20MB cap in
    `qms_standards.py`, the 50MB `LASER_UPLOAD_MAX_BYTES` in `work_orders.py`, and
    nginx's `client_max_body_size 50M`. (These were the *only* ones when this entry
    was written. A fourth — the JSON body cap, then named
    `MAX_SANITIZED_JSON_BODY_BYTES` and now `MAX_JSON_BODY_BYTES` — was added on
    2026-07-30; see **The JSON body-size cap** below. That section also records
    that the nginx line governs the compose stack only and does **not** protect the
    Railway-served API.)
  - **One caveat worth recording:** Starlette constructs the parser *outside* its
    `try/except MultiPartException`, and `FormParserError` is not a
    `MultiPartException` — so a >256-byte boundary surfaces as a **500** rather
    than a clean 4xx. Attacker-only (no real client comes close), no availability
    impact, no legitimate traffic affected.

- **`pydantic-settings` 2.12.0 → 2.14.2** — clears **GHSA-4xgf-cpjx-pc3j**. Two
  minor versions with no cascade: 2.14.2 declares `pydantic>=2.7.0`, the same
  floor 2.12.0 declared, so `pydantic` stays at 2.12.5. `app/core/config.py`
  instantiates `settings = Settings()` at module import, so every test run parses
  the whole settings surface through the new version — a green suite is direct
  evidence here, not incidental.

- **`bleach` 6.3.0 → 6.4.0 — SUPERSEDED. bleach was removed from the tree later
  the same day; see [bleach removed](#bleach-removed--escape-at-the-sink-2026-07-30).**
  This entry is retained for the "quiet scanner" lesson, which outlives the
  package. Its two live obligations are both discharged: there was never a
  suppression flag to remove, and `backend/tests/test_bleach_linkify_guard.py`
  was deleted along with the dependency it guarded — replaced by
  `tests/test_frontend_no_raw_html_render_guard.py`, which asserts bleach is
  neither pinned nor imported. **Do not act on the "keep this guard" instruction
  below; it applied to a dependency that no longer exists.**

  The bump itself cleared **GHSA-8rfp-98v4-mmr6** and **GHSA-gj48-438w-jh9v**.
  It did **not** fix **GHSA-g75f-g53v-794x** (the `linkify` ReDoS) — and
  pip-audit stopped reporting that one anyway.
  - **The scanner going quiet is a database artifact, not a fix.** Verified
    directly: the OSV API returns **0 vulns for bleach 6.4.0** and all three for
    6.3.0; the linkify advisory's OSV record carries an explicit affected-version
    **list of exactly `["6.3.0"]`** — a list, not a range — published 2026-06-16,
    *after* bleach was archived (2026-06-10) and after 6.4.0 shipped as the final
    release the project will ever have. Diffing installed 6.3.0 against installed
    6.4.0: `handle_email_addresses` is byte-identical, and the only change in
    `build_email_re` is cosmetic (a `.format()` call reflowed onto one line). **The
    regex is unchanged.**
  - **No suppression entry and no `--ignore-vuln` flag is needed** — there is
    nothing to suppress; the scanner is already silent. `--ignore-vuln
    PYSEC-2026-1325` (`ecdsa`) remains the only flag on the command, unchanged.
    Do not add one for this, and do not read its absence as "the risk was fixed".
  - **Why the app is safe: the vulnerable code path does not exist here.** The
    only bleach import in `app/` is `app/core/sanitization.py:1` →
    `from bleach import clean`. `parse_email` is not even a `clean()` parameter;
    module-level `clean()` constructs a `Cleaner` with no `filters`, so
    `LinkifyFilter` is never instantiated; there are **zero** `linkify` references
    anywhere in `app/`.
  - **The input *was* attacker-controlled — say so plainly.** The `sanitize_input`
    middleware (`app/main.py`) ran on every JSON-bodied POST/PUT/PATCH **before
    route-level auth**. The safety here rested entirely on the code path not
    existing, **not** on the input being safe. (The "no body-size cap" this entry
    originally noted alongside that was fixed — see **The JSON body-size cap**
    below — but a size cap bounds *cost*, not reachability, and changed nothing
    about the linkify argument.)
  - ~~**`backend/tests/test_bleach_linkify_guard.py` is now the only remaining
    protection.**~~ *(Retired with the dependency — the guard is deleted, and
    nothing can introduce `linkify()` in an app that does not install bleach.)*
  - **bleach was permanently unmaintained security-relevant surface**: archived
    upstream, 6.4.0 is the terminal release, and it sat under a global
    request-body middleware. Any future bleach advisory has no fix *by
    construction* — the only responses left are reachability arguments like this
    one. **That is the argument that ultimately removed it.** Replacing it was
    carried here as an open follow-up; the replacement investigation concluded
    "keep bleach" and was overturned the same day by the removal — see
    **Replacing bleach** for the measured library comparison (still the reason a
    swap was not the answer) and **bleach removed** for what shipped.

**Validation (2026-07-30, covering all four bumps):** full backend suite
**3757 passed, 2 xfailed**, coverage 80.97%; `pip check` clean; and the A/B harness
run across both venvs (old pins vs new) produced byte-identical results everywhere
except the one intended urlencoded-limit change documented above. One test-config
change rode along: starlette 1.3.x reclassified its deprecations under
`StarletteDeprecationWarning`, which subclasses `UserWarning` rather than
`DeprecationWarning`, so `pytest.ini`'s existing `ignore::DeprecationWarning`
stopped matching and the suite jumped from 14 to 155 warnings. A targeted,
**message-scoped** `filterwarnings` entry in `backend/pytest.ini` restores it —
message-scoped on purpose, since a blanket `ignore::UserWarning` would swallow
unrelated warnings.

### Replacing bleach — investigated 2026-07-30, decision: no replacement library

> **Status: the "keep bleach" half of this decision was superseded the same day.**
> The library comparison below still stands and is *why* the sanitizer was removed
> rather than swapped — every candidate destroyed operator text, so there was no
> safe library to move to. The conclusion that therefore we keep bleach did not
> survive: the middleware was deleted instead, which was already listed here as
> one of the two things that would change the answer. Current state:
> [bleach removed](#bleach-removed--escape-at-the-sink-2026-07-30).

`bleach` is archived, 6.4.0 is terminal, and it sat under a global request-body
middleware — so "move sanitization to a maintained library (`nh3` is the usual
successor)" was the follow-up this file carried. **It was actually done, with measured
differential corpora, and the answer is no.** Three candidates were evaluated against a
**113-input corpus** of real shop text; all three were rejected on behavior. This is a
decision, not inertia.

**`nh3` — rejected. It destroys operator text, and no configuration fixes it.**
`nh3` beats bleach on maintenance, wheels, parser quality and speed. It fails on the one
axis that decides it: output. Measured against bleach 6.4.0, **14 of 113 inputs diverge
irreconcilably**, and the divergence is silent truncation:

| Input | bleach 6.4.0 | nh3 |
|---|---|---|
| `Runout<TIR spec on OP30 - see Bob` | `Runout&lt;TIR spec on OP30 - see Bob` | `Runout` |
| `Check OD<ID before press fit, log in QMS` | escapes `<`, keeps every word | `Check OD` |
| `Tolerance <MIN> per print, inspect 100%` | `Tolerance  per print, inspect 100%` | `Tolerance ` |
| `Qty <set> at OP20 - verify with gage` | `Qty  at OP20 - verify with gage` | `Qty ` |

Two destruction families, both in html5ever's tree builder, **neither configurable**:

1. **MathML/SVG element names are parsed as foreign content and dropped along with the
   rest of the string.** A dictionary scan found **98 ordinary English words** that
   trigger it — `min max mean set text list line path filter degree use view stop switch
   and or not true false sum times limit matrix vector circle template none cos sin tan
   log` …
2. **`<` immediately followed by a letter, with no closing `>`, discards to end of
   input.** (`<` followed by a digit, space, `.`, `=`, `-` or `_` is safe on both.)

**Why this is decisive rather than a papercut: `sanitize_input` rewrote
`request._body`, so the sanitizer's output was what got persisted.** Silently storing an
NCR narrative as `"Runout"` is an AS9100D / ISO 9001 records-integrity defect with **no
recovery path** — the original bytes are gone before anything writes a row. And
manufacturing text is precisely the corpus that triggers it: `<`-as-"less than" beside a
tolerance word is ordinary shop shorthand.

**Read that argument once more, because it generalizes.** It says a sanitizer on a
persistence path is judged on what it *destroys*, and that this corpus is full of
angle brackets that mean "less than" rather than "start of tag". Applied to nh3 it
rejected nh3. Applied to bleach it rejects bleach — bleach's own column above turns
`Tolerance <MIN> per print` into `Tolerance  per print`, which is the same
records-integrity defect at smaller amplitude. The investigation stopped one step
short of that; the removal below is that step.

Worth recording that the *predicted* blocker was the wrong one. The expected problem was
nh3 deleting `<script>` inner text — which turned out to be the one thing that **is**
configurable (`clean_content_tags=set()`). The blockers were families nobody predicted,
which is itself the argument against swapping a sanitizer that sits on a persistence
path.

**Supply-chain footnote:** `nh3` statically links the Rust `ammonia` crate into its
wheel, so **pip-audit / OSV cannot see ammonia advisories at all**. That is the same
"scanner is silent, nothing was actually fixed" failure mode this file already documents
for bleach's GHSA-g75f-g53v-794x, relocated rather than solved. Adopting nh3 would mean
tracking ammonia releases by hand.

**The other two candidates:**

- **`html-sanitizer`** — lxml-based and designed around allowlists for rich text. A poor
  fit for this app's "strip everything" use, and it adds native surface.
- **A hand-rolled stdlib sanitizer** — rejected on principle: writing your own HTML
  sanitizer is a classic way to introduce the vulnerability you were trying to avoid.

**What keeping bleach accepts:** unmaintained, security-relevant surface on a pre-auth
path, where the next advisory will have no fix available.

**What would change the answer:**

- a maintained sanitizer that is **byte-equivalent to bleach 6.4.0 on this corpus** — the
  golden-corpus test was the acceptance criterion, and it was executable; or
- the middleware being **removed** — retire blanket input sanitization in favor of
  output-encoding at render time, at which point the library choice stops mattering.
  **This is the one that happened**, hours later. See below.

*(The two tests that held this decision in place —
`backend/tests/test_bleach_linkify_guard.py` and the golden-corpus characterization
test that froze bleach's output — were deleted with the dependency. A
characterization test whose subject no longer runs pins nothing.)*

### bleach removed — escape at the sink (2026-07-30)

**Current state, and it supersedes both sections above.** There is no ingest-time HTML
sanitization in this backend and **`bleach` is not a dependency**. `app/core/sanitization.py`
is deleted. The middleware that called it, `sanitize_input`, is renamed
`limit_json_body_size` and now does nothing but enforce the size cap.

This was not a risk acceptance. The sanitizer was removed because it protected nothing,
covered less than it claimed, and corrupted quality records. Five findings, each checked
against the code:

1. **The SPA cannot execute stored HTML.** React escapes text nodes on output, and
   `frontend/src/` contains **zero** `dangerouslySetInnerHTML` and **zero** `innerHTML`
   writes. A `<script>` persisted in a part note rendered as those literal characters.
   The middleware was providing no XSS protection for the only consumer that could have
   needed it.
2. **Exactly one backend sink interprets markup, and it is now escaped at render.**
   `reportlab.platypus.Paragraph` parses a mini-HTML dialect via `paraparser`; every
   interpolation into one (`quote_pdf_service.py`, `coc_pdf_service.py`) goes through
   the new `pdf_escape` in `app/services/pdf_text.py`. The other candidate sinks were
   audited and are safe on their own: HTML email renders through a Jinja2 `Environment`
   with `autoescape=select_autoescape(['html','xml'])`, `label_service` draws with
   `canvas.drawString` (which parses nothing), and reportlab `Table` cells take plain
   strings.
3. **It was corrupting AS9100D records.** ASME Y14.5 drawing notation is
   angle-bracketed — `<REF>`, `<TYP>`, `<MMC>`, `<BASIC>`, `<MIN>` — so an inspection
   note reading `Dim is 2.500 <REF> per print` was **silently persisted** as
   `Dim is 2.500  per print`. Same for work instructions and BOM/PO notes. This is the
   nh3 objection above turned on bleach itself.
4. **It never covered what it claimed.** `sanitize_dict` recursed into nested dicts but
   **not into dicts inside lists**, and the middleware only rewrote bodies that parsed
   to a top-level `dict` — so a top-level JSON array skipped it entirely. Between them
   that is every BOM line, PO line and routing operation the API accepts. A control with
   those holes was never the thing standing between this app and stored XSS.
5. **It failed open.** The whole sanitize block sat inside `except Exception: log a
   warning` — any sanitizer error let the raw body through with a log line nobody reads.

**The rule now: store the operator's bytes verbatim; escape where they are interpreted.**
Do not reintroduce an HTML sanitizer for input handling. If you add a sink that
interprets markup, escape at that sink — `pdf_escape` is the pattern.

**Three tests enforce this**, and they are the reason the argument stays true rather than
decaying into a comment:

- `backend/tests/test_frontend_no_raw_html_render_guard.py` — fails if
  `dangerouslySetInnerHTML` or an `innerHTML` write appears in `frontend/src/`, and
  separately if `bleach` is pinned in `requirements.txt` or imported anywhere in `app/`.
  Its failure message points here. A failure means finding 1 above stopped being true.
- `backend/tests/test_pdf_text_escaping.py` — pins `pdf_escape` behavior and the
  escaping of the `Paragraph` sites **that exist today**, by building both PDFs with
  hostile values and reading the text back out.
- `backend/tests/test_escape_at_sink_guards.py` — covers the sites that **don't exist
  yet**, which is where the argument was actually thin. A structural `ast` scan of every
  module under `backend/app/` (including untracked ones, so a brand-new PDF service is
  visible) fails if any value is spliced into a reportlab `Paragraph` without
  `pdf_escape` — via f-string, `%`, `.format()`, `+`, or a local variable built from
  one. The same file guards the *other* unenforced half: no caller may pass a non-`None`
  `body=` to `EmailService.send_email`, because that path assigns `html_body = body` and
  attaches it as `MIMEText(html_body, "html")` with no escaping. Jinja2 `autoescape`
  covers only the `template=` path, so the raw-`body` path is one innocuous-looking call
  away from being a live HTML-injection sink in outbound mail. Nothing uses it today;
  the two ARQ relay hops that forward `body` verbatim (`worker.send_email_job` →
  `email_jobs.send_email_task`) are permitted only because their own call sites are
  scanned too, and the chain originates at a literal `body=None` in
  `notification_dispatch._enqueue_email`.

**What this accepts, stated plainly:** persisted strings may now contain raw markup,
including `<script>`. That is safe only for as long as findings 1 and 2 hold, which is
exactly what those three tests check. A future Markdown renderer, rich-text field, or
charting library wired through `innerHTML` is an XSS sink against deliberately
unsanitized data — the fix is to escape at that new sink, **not** to put body mutation
back.

**Supply-chain result:** one archived, permanently-unmaintained, security-relevant
package is out of the tree, on a pre-auth path, where by construction the next advisory
would have had no fix.

#### Known regression on legacy records — correct forward, no backfill

The removed middleware did not only strip tags, it **entity-encoded**. Verified against
bleach 6.4.0: `"Smith & Sons"` was persisted as `"Smith &amp; Sons"`, `"OD<ID"` as
`"OD&lt;ID"`. So **every row written through the JSON API while that middleware was live
holds entity text in the database** — and customer names containing `&` are common.

Before this change the PDF builders passed those raw into `Paragraph`, which parsed the
entities back, so a Certificate of Conformance rendered `Smith & Sons` — *accidentally*
correct. `pdf_escape` now escapes them a second time, so a re-issued CoC or quote prints
`Smith &amp; Sons` literally.

Two things bound how bad this is. The data was **already corrupt** — the SPA has always
displayed `Smith &amp; Sons` for those rows, because React escapes too — so the PDF was
the only view that concealed it. This change makes existing corruption *visible and
consistent* rather than creating it. But it is a real regression on customer-facing
compliance artifacts, introduced by a change whose stated purpose is records integrity,
and it is recorded here rather than discovered later.

**Decision: correct forward, no backfill** — the same posture as the receiving
`NOT_REQUIRED` inspection-status correction. When an affected CoC or quote is re-issued,
fix the source record. Do **not** "fix" this inside `pdf_escape`: `html.unescape`
over-decodes (`&notanentity;` → `¬anentity;`, an HTML5 legacy semicolon-less match), and
a targeted unescape of `&amp;`/`&lt;`/`&gt;` would silently destroy any legitimately
typed entity. This is a data defect and belongs in the data.

*A scoped one-time repair* — unescaping those three entities across only the columns the
two PDF builders read — is defensible as its own reviewed change. It mutates historical
records on live multi-tenant data, so it needs migration review, not a drive-by.

### The JSON body-size cap (added 2026-07-30 as the bleach DoS fix; retained)

The replacement investigation did not find a replacement. It found a live production
denial-of-service, and the cap it produced **outlived the sanitizer that motivated it**.

`bleach.clean` is **quadratic** in adversarial input, and the middleware had **no
body-size cap**:

| Body | Time |
|---|---|
| `"<a " * n` — 24 KB | 0.10s |
| — 48 KB | 0.35s (3.4×) |
| — 96 KB | 1.23s (3.5×) |
| — 192 KB | 4.72s (3.8×) |
| benign text — 128 KB | 0.005s |

Exposure was confirmed rather than assumed: **no app-level body cap existed anywhere**;
the middleware runs **before route-level auth dependencies**, so the cost was reachable
**pre-auth**; and the `client_max_body_size 50M` in `nginx/nginx.prod.conf` **does not
protect the API** — the backend image ships no nginx and Railway serves uvicorn directly
(that line governs the compose stack only). This was live in production.

**Fixed** by a new setting plus two size gates — deliberately library-independent, which
is why the whole thing survived the sanitizer's removal untouched:

- **`MAX_JSON_BODY_BYTES`** (`app/core/config.py`), `int`, default **262144**
  (256 KB), env-overridable. Renamed from `MAX_SANITIZED_JSON_BODY_BYTES` when the
  sanitizer went; the old name still works as a deprecated alias. See
  [Request Body Size](ENVIRONMENT_VARIABLES.md#request-body-size-json).
- Two gates in `limit_json_body_size` (`app/main.py`, formerly `sanitize_input`): a
  **`Content-Length` pre-read check** (an oversized request is never buffered) and a
  **post-read `len(body)` check**, for chunked transfer-encoding or a header that lies.
  Over the cap → **HTTP 413**, with CORS headers applied by hand, matching the adjacent
  `csrf_protection` precedent (this middleware is the outer layer, so a short-circuited
  response never passes back through `CORSMiddleware`).

**Why the cap is still here now that bleach is gone.** The quadratic-CPU justification
retired with the sanitizer, but the gate did not, and the rationale is no longer
library-specific: the middleware still runs ahead of every route's auth dependency, so
without a cap an **unauthenticated** caller decides how many bytes the app buffers into
memory and hands to `json.loads`, and how large the resulting Python object graph gets.
Bounding that is ordinary request-size hygiene, and the numbers below are now historical
— they record the DoS that motivated the cap, not today's cost.

**Measured result (2026-07-30, with bleach still installed):** worst case at the cap was
**9.1 CPU-seconds**; anything larger was rejected in **0.0005s**, with bleach never
running. Raising the setting raised that worst case **quadratically**. With bleach gone
that quadratic term is gone with it; the cost of a body at the cap is now just parse +
allocation, which is linear.

**Scope verified:** multipart / `UploadFile` paths are untouched (every CSV/XLSX bulk
import, RFQ package, laser-nest ZIP/PDF, PO document) and keep their own per-endpoint
caps, as are the carrier webhooks, which skip this middleware entirely so their HMAC
verifies against raw bytes — and so a carrier, which cannot recover from a 413 the way a
UI client can, is never rejected by it. Sizing was measured against real payloads — a
170-nest laser import is 183 KB and a 1000-line-item BOM create is 201 KB, both under the
cap; the known ceiling is a BOM create above roughly 1300 line items, which is why the
value is a setting and not a constant.

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

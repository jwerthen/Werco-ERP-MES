# Security Advisory Posture (CI `Security Scanning`)

This document records how the CI `Security Scanning` job
(`.github/workflows/ci-cd.yml`) treats dependency advisories, and the
justification for any gate that is not hard-blocking.

## Policy

- Prefer fixing over tolerating: apply any patch/minor upgrade that clears an
  advisory before accepting it.
- **Frontend `npm audit --audit-level=high` is a HARD gate** (must pass). All
  known advisories are currently resolved via non-breaking lockfile upgrades.
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

## Frontend (`npm audit --audit-level=high`) — hard gate

No suppressions. All advisories were resolved with non-breaking patch/minor
upgrades via `npm audit fix` (lockfile only; no `package.json` range changes).
If a future advisory has no non-breaking fix, document it here and narrow the
gate explicitly rather than dropping `--audit-level`.

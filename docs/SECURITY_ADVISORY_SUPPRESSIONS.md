# Security Advisory Posture (CI `Security Scanning`)

This document records how the CI `Security Scanning` job
(`.github/workflows/ci-cd.yml`) treats dependency advisories, and the
justification for any gate that is not hard-blocking.

## Policy

- Prefer fixing over tolerating: apply any patch/minor upgrade that clears an
  advisory before accepting it.
- **Frontend `npm audit --audit-level=high` is a HARD gate** (must pass). All
  known advisories are currently resolved via non-breaking lockfile upgrades.
- **Backend `safety check` is ADVISORY** (non-blocking, `continue-on-error: true`)
  — see below.

## Backend (`safety check`) — advisory, not blocking

The backend `safety check` step runs but does **not** fail the job
(`continue-on-error: true`). Rationale: the free `safety` advisory database is a
daily-moving target — new CVEs are published continuously against already-pinned
versions — so a hard gate makes every unrelated PR's CI flap red for reasons
outside that PR's scope. The scan output stays visible in the job log as an
informational signal.

Backend dependency-CVE remediation is handled deliberately as **ongoing security
hygiene** (tracked separately), not as a per-PR blocker: bump affected packages
(e.g. `cryptography`, `requests`, `pyopenssl`, `configobj`) on a reviewed
cadence, run the full backend test suite, and document anything that cannot be
safely upgraded here.

### Background: ecdsa (IDs 64459 / 64396)

`ecdsa` (transitive via `python-jose[cryptography]`) has "Minerva" timing
side-channel advisories with **no fixed release** (`Affected spec: >=0`). Not
exploitable here: this app signs/verifies JWTs with **HS256 (HMAC) exclusively**
(`app/core/config.py` `ALGORITHM="HS256"`, `app/core/security.py`); the ECDSA
(ES256/384/512) code path is never used. Revisit only if we adopt an EC JWT
algorithm or remove `python-jose`.

## Frontend (`npm audit --audit-level=high`) — hard gate

No suppressions. All advisories were resolved with non-breaking patch/minor
upgrades via `npm audit fix` (lockfile only; no `package.json` range changes).
If a future advisory has no non-breaking fix, document it here and narrow the
gate explicitly rather than dropping `--audit-level`.

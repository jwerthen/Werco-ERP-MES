# Security Advisory Suppressions

This document records every security advisory that the CI `Security Scanning`
job (`.github/workflows/ci-cd.yml`) suppresses, with the justification for each.
Suppressions are deliberate, narrowly scoped, and reviewed — do not add to this
list without recording the same level of detail.

## Policy

- Prefer fixing over suppressing. Apply any patch/minor upgrade that clears an
  advisory before considering a suppression.
- Only suppress an advisory when **no fixed release exists** (or the only fix is
  a breaking major bump that is not yet feasible) **and** the vulnerable code
  path is provably not exercised by this application.
- Every suppression must name the advisory ID, the package, why it is not
  exploitable here, and what would let us drop the suppression.
- Re-review on every dependency bump that touches the affected package.

## Backend (`safety check`)

The backend step runs `safety check -i <id> ...`. The full unsuppressed scan is
clean except for the IDs below.

### ecdsa — IDs `64459` and `64396`

- **Package:** `ecdsa` (currently 0.19.2), a transitive dependency of
  `python-jose[cryptography]`.
- **Advisories:**
  - `64459` (CVE-2024-23342) — "Minerva" timing side-channel in pure-Python
    ECDSA signature operations.
  - `64396` — related side-channel / pyasn1 advisory.
- **Why suppressed:** Both advisories report `Affected spec: >=0` — the `ecdsa`
  maintainers do **not** provide a fixed release for the pure-Python side-channel,
  so there is no upgrade that clears them. More importantly, the vulnerable code
  path is never reached: this application signs and verifies all JWTs with
  **HS256** (HMAC-SHA256) exclusively — see `app/core/config.py`
  (`ALGORITHM = "HS256"`) and `app/core/security.py`. `ecdsa` is only used by
  `python-jose` for the ES256/ES384/ES512 (elliptic-curve) algorithms, which this
  app does not use. The timing attack is against ECDSA private-key operations that
  never execute here.
- **What would let us drop it:** Switching JWT signing to an EC algorithm (we do
  not plan to), or `ecdsa` shipping a fixed release, or removing `python-jose` in
  favor of a JWT library that does not pull in `ecdsa`.

## Frontend (`npm audit --audit-level=high`)

No suppressions. All advisories were resolved with non-breaking
patch/minor upgrades via `npm audit fix` (lockfile only; no `package.json`
range changes). If a future advisory has no non-breaking fix, document it here
and narrow the gate explicitly rather than dropping `--audit-level`.

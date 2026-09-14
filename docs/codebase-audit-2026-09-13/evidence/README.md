# Reproduction and verification evidence

These are audit artifacts for commit a4a085a, not fixes or newly installed CI tests. They reproduce the pre-fix behavior using synthetic data. Application behavior may change when the defects are repaired.

- [Security reproduction](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/security_repro.py) and [recorded output](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/security-repro-results.txt): real application routes/middleware and an isolated in-memory SQLite fixture; file storage is mocked.
- [Backend domain reproduction](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/backend_domain_repros.py) and [recorded output](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/backend_domain_repros_pinned.jsonl): real planning/wrapper functions with in-memory transactions and injected failures.
- [Client session reproduction](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/frontend-session-repro.cjs) and [recorded output](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/frontend-session-repro-results.txt): actual transpiled client with controlled transport/storage.
- [Permission reproduction](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/frontend-permissions-repro.cjs) and [recorded output](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/frontend-permissions-repro-results.txt): actual frontend utility against backend defaults.
- [Repository measurements](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/metrics.json) and [measurement details](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/metrics-detail.json).
- [Verification summary](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/verification.json) and [dependency scan output](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/dependency-audits.txt).

Run frontend harnesses from the repository root using Node and the installed frontend TypeScript dependency. Run Python harnesses from backend/ with PYTHONPATH=. and an environment installed from the backend runtime/development requirements. The security harness loads tests/conftest.py. Read setup before execution. These probes deliberately simulate invalid requests and failures in test data.

The Hook and mypy probe files are intentionally invalid examples used only to demonstrate missing checks; they are not production components.

The full frontend/backend suite logs and temporary dependency installation remain in /tmp/werco-audit-2026-09-13 for this local session. The durable summary records commands, scope, outcomes, and limitations.


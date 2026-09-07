# Backend and integration validation

Local validation ran on September 7, 2026 using isolated SQLite fixtures and fake email transports.

- Full backend coverage run: **7,116 passed, one integration failure, 86.09% coverage** in 499.69 seconds; the configured 78% coverage gate passed. The single failure was the MCP documentation guard: a new route handler named `status` accidentally made that common field name a generated tool-name claim.
- The six new delivery handlers now use specific document-delivery names. HTTP paths are unchanged. After this fix, **all 198 MCP tests passed**, and a separate **46-test delivery/documentation slice passed**. The fix preserves the guard and its assertions.
- Full Black, isort, Flake8 and mypy checks passed. Mypy checked 379 source files. Bandit reported zero medium/high severity issues. The final route-name edit also passed Black, Flake8 and mypy.
- Migrations 091–094 have one Alembic head (`094_document_deliveries`). Their combined SQLite upgrade, reverse downgrade and re-upgrade passed with foreign keys enabled, representative records in all four tables and preceding parent records retained. PostgreSQL offline SQL was generated for all three phases, including RLS and constraints. No actual PostgreSQL migration or contention test is claimed.
- Independent integration review checked model exports, router registration, frontend API paths/types, source permissions and duplicate-send recovery. It found a Purchasing history-access mismatch for managers without approval permission; the opening gate and composer mutation controls were corrected and regression-tested.

Full-run log: `/tmp/werco-next-final-backend.log`. MCP replay: `/tmp/werco-next-mcp-all.log`. Delivery/documentation replay: `/tmp/werco-next-mcp-delivery-rerun.log`. Migration chain checker and SQL: `/tmp/werco-next-migration-chain-check.py`, `/tmp/werco-next-four-migrations-postgres.sql`.

The protected PR's CI runs the entire backend suite again against the final commit. Its result is the final release gate, rather than treating the first local run as fully green.

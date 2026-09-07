# Independent test-engineer review

September 7, 2026. Reviewed the root cycle-count workspace and production agent's planning/material readiness/job-timeline changes after reading `.claude/agents/test-engineer.md`. The reviewer implemented customer/import/search changes, not these production/count services.

No remaining product blocker was found in this bounded review. Checked company and actor scoping, read-only preview behavior, material/stock date boundaries, shared supply use, deterministic history cursors, private audit/event payload exclusion, count observation locking, review fingerprints, and replay refusal. This is not an exhaustive security audit or real PostgreSQL contention certification.

Five additional test cases strengthen concrete uncovered behavior:

- Stock still usable today must block a +7-day shift when it expires before the new start.
- A calendar closure that delays first work beyond stock expiry also blocks the reviewed plan.
- Supplemental NCR telemetry excludes foreign/deleted parents and foreign events, hides mis-parented actor identities, and never exposes event payloads or costs.
- A second physical observation after adjustment review invalidates posting and leaves stock/ledger unchanged.
- Assignment old/new values survive a transaction close as a committed company-scoped audit record.

The existing soft-delete customer sweep now separately proves that PUT refuses a tombstone and that a historical active-but-deleted fixture stays absent from picker results. Its old expectation that PUT could reactivate the tombstone was intentionally replaced; the read-side assertion remains intact.

Final focused run: **55 passed in 29.18s**, covering the entire soft-delete sweep, customer preservation/audit suites, planning/material suite, timeline suite, and cycle-count workspace suite. Command from `backend/`:

```sh
pytest tests/api/test_soft_delete_read_sweep.py tests/api/test_customers.py tests/api/test_customers_audit_persistence.py tests/api/test_job_planning_materials.py tests/api/test_work_order_timeline.py tests/api/test_cycle_count_workspace.py --no-cov -q
```

The five new cases pass against the existing frozen product logic; no production/count implementation changes were needed. SQLite fixtures validate behavior and transaction boundaries; root's full CI run retains the repository coverage threshold and separate PostgreSQL migration/privilege checks.

"""
Database lock helpers used to serialize hot read-modify-write paths such
as number generation (work order, PO, NCR, receipt) where two concurrent
requests could otherwise generate the same number.

Uses Postgres advisory locks scoped to the current transaction. They are
released automatically when the transaction commits or rolls back, so no
cleanup is required. On non-Postgres dialects (e.g. SQLite in tests) this
is a no-op; those environments aren't concurrent enough for races to
matter and Postgres-only SQL would break the test suite.
"""
from __future__ import annotations

import zlib

from sqlalchemy import text
from sqlalchemy.orm import Session


def _stable_key(name: str) -> int:
    """Map an arbitrary namespace string to a stable 32-bit int."""
    return zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF


def acquire_generator_lock(db: Session, namespace: str, company_id: int | None = None) -> None:
    """
    Acquire a transaction-scoped advisory lock for `namespace` (and
    optionally a company_id). Blocks until the lock is granted, which
    serializes the critical section against concurrent requests that
    pass the same namespace/company pair.

    Example:
        acquire_generator_lock(db, "work_order_number", company_id)
        number = generate_work_order_number(db, company_id)
        db.add(work_order)
        db.commit()  # releases the lock
    """
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect != "postgresql":
        # No-op on dialects that don't support advisory locks (e.g. SQLite).
        return

    key1 = _stable_key(namespace)
    key2 = int(company_id or 0)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
        {"k1": key1, "k2": key2},
    )

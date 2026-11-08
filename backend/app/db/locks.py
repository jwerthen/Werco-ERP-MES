"""
Database lock helpers used to serialize hot read-modify-write paths such
as number generation (work order, PO, NCR, receipt) where two concurrent
requests could otherwise generate the same number -- plus
``is_lost_lock_race``, which recognizes the error Postgres raises on the
transaction it rolls back when two writers' locks conflict.

Uses Postgres advisory locks scoped to the current transaction. They are
released automatically when the transaction commits or rolls back, so no
cleanup is required. On non-Postgres dialects (e.g. SQLite in tests) this
is a no-op; those environments aren't concurrent enough for races to
matter and Postgres-only SQL would break the test suite.
"""

from __future__ import annotations

import zlib

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

# The SQLSTATEs Postgres raises on the transaction it ROLLS BACK to settle a lock conflict:
# ``deadlock_detected`` (40P01, one side of a lock cycle chosen as the victim) and
# ``serialization_failure`` (40001). Both mean "another writer won; nothing of yours committed",
# so the caller's correct advice is to refresh and retry -- never "your input is invalid".
LOST_LOCK_RACE_SQLSTATES = frozenset({"40P01", "40001"})


def is_lost_lock_race(exc: BaseException) -> bool:
    """True when ``exc`` is the DBAPI error of a transaction Postgres rolled back to break a lock race.

    Judged on the driver error's ``pgcode`` alone (``LOST_LOCK_RACE_SQLSTATES``), so every other
    ``OperationalError`` -- a dropped connection, a statement timeout, a NOWAIT miss -- keeps
    whatever answer its caller already gives it. SQLite raises neither code, so this is never
    true on the test backend unless a test raises one on purpose.
    """
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(exc.orig, "pgcode", None) in LOST_LOCK_RACE_SQLSTATES


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

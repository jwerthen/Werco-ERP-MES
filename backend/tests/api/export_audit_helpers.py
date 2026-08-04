"""Read committed ``EXPORT`` audit rows from OUTSIDE the app's own session.

Shared by every test that asserts a bulk export was recorded
(``test_export_gate_and_audit.py`` for ``/api/v1/exports/*``,
``test_custom_report_tenant_isolation_batch11.py`` for the analytics
custom-report export).

Why this exists rather than ``db_session.query(AuditLog)``:

``AuditService.log`` only **flushes**. In production ``get_db``'s teardown closes
the request session without committing, so a handler that logs and never commits
writes nothing at all -- the disclosure silently goes unrecorded. That is the
exact mistake this helper is built to catch, and reading the row back through
``db_session`` cannot catch it: the ``client`` fixture hands the app the very
same session the test holds, and the suite's SQLite engine uses ``StaticPool``
(one shared DBAPI connection), so an uncommitted flush is just as visible as a
committed row and the test passes either way.

A second engine gets a second connection to the same database, which by
definition can only see what has been committed.
"""

import os
from contextlib import contextmanager
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog


@contextmanager
def fresh_session():
    """Yield a Session on its own connection, so only COMMITTED rows are visible."""
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def committed_export_rows(resource_type: Optional[str] = None) -> List[AuditLog]:
    """Every committed ``EXPORT`` audit row, oldest first, read from a fresh connection."""
    with fresh_session() as verify:
        query = verify.query(AuditLog).filter(AuditLog.action == "EXPORT")
        if resource_type is not None:
            query = query.filter(AuditLog.resource_type == resource_type)
        rows = query.order_by(AuditLog.id).all()
        for row in rows:
            # Touch every attribute the assertions read while the session is still
            # open; the objects outlive it.
            _ = (
                row.action,
                row.resource_type,
                row.resource_id,
                row.resource_identifier,
                row.description,
                row.user_id,
                row.company_id,
                row.new_values,
                row.old_values,
                row.extra_data,
            )
        verify.expunge_all()
        return rows

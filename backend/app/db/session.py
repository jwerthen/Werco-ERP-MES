"""
Database session module.

Canonical import location for the SQLAlchemy session factory used by the
background-job modules (``app/jobs/*``) and any other code that needs a raw
session outside the FastAPI request lifecycle. The engine and ``SessionLocal``
themselves live in :mod:`app.db.database`; this module re-exports them so the
long-standing ``from app.db.session import SessionLocal`` convention resolves.
"""

import logging

from sqlalchemy.orm import Session

from app.db.database import Base, SessionLocal, engine, get_db

__all__ = ["Base", "SessionLocal", "engine", "get_db", "reset_session_after_failure"]

logger = logging.getLogger(__name__)


def reset_session_after_failure(db: Session, unit: str) -> None:
    """Roll back a session SHARED by a job's loop after one unit of work (a tenant, a shipment) failed.

    A loop that catches one unit's exception and moves on must also reset the session: a database
    error leaves the transaction aborted (Postgres refuses every later statement with 25P02), a
    failed flush leaves it needing a rollback, and a COMMIT the guard refused leaves it "prepared"
    (``app.db.database``). Without this, every later unit in the run fails on the first one's error.

    Never raises: a failed rollback is logged with its traceback and the loop carries on, so the
    next unit fails -- or succeeds -- on its own terms instead of the whole run stopping here.
    """
    try:
        db.rollback()
    except Exception:
        logger.exception("Could not roll back the shared session after %s failed", unit)

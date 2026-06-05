"""
Database session module.

Canonical import location for the SQLAlchemy session factory used by the
background-job modules (``app/jobs/*``) and any other code that needs a raw
session outside the FastAPI request lifecycle. The engine and ``SessionLocal``
themselves live in :mod:`app.db.database`; this module re-exports them so the
long-standing ``from app.db.session import SessionLocal`` convention resolves.
"""

from app.db.database import Base, SessionLocal, engine, get_db

__all__ = ["Base", "SessionLocal", "engine", "get_db"]

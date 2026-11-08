"""Fail-closed handling for REQUIRED audit evidence at the HTTP boundary.

``AuditService.log_required`` raises ``AuditWriteError`` when the tamper-evident row
for a state change cannot be written. A caller that opted in to required evidence must
then undo the WHOLE unit of work -- a mutation that landed without its audit row is the
exact outcome the opt-in exists to prevent -- and tell the client nothing was saved.

Endpoint modules have each hand-rolled that ``except AuditWriteError: db.rollback();
raise HTTPException(503, ...)`` (``laser_nests.py``, ``hank_tasks.py``, ``documents.py``,
...). New call sites use :func:`required_audit` instead, so the rollback can never be
forgotten and the status code cannot drift:

    with required_audit(db, "The audit record for X could not be saved; nothing was changed. Retry."):
        audit.log_required(...)

Scope it to the block that writes the required rows (and anything after it that must
roll back with them), not to the whole handler: an ``HTTPException`` raised inside the
block passes through untouched, but only ``AuditWriteError`` is translated.
"""

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.audit_service import AuditWriteError

REQUIRED_AUDIT_UNAVAILABLE_STATUS = 503
# The text the most required-audit routes already answer with (auth registration, documents,
# job costing, user provisioning, NCR/CAR, FAI). ``app.main`` answers it for every route that
# lets ``AuditWriteError`` escape instead of translating it here.
REQUIRED_AUDIT_UNAVAILABLE_DETAIL = "Unable to save audit record"


@contextmanager
def required_audit(db: Session, detail: str) -> Iterator[None]:
    """Translate ``AuditWriteError`` into ``db.rollback()`` + HTTP 503 with ``detail``.

    ``detail`` is shown to the user verbatim, so it should say that nothing was changed
    and that a retry is safe. Any other exception propagates unchanged (the caller's own
    StaleDataError / IntegrityError handling still applies).
    """
    try:
        yield
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(status_code=REQUIRED_AUDIT_UNAVAILABLE_STATUS, detail=detail) from exc

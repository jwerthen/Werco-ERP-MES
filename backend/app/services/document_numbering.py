"""Shared sequential Document-number generator (PR 4 ledger — dedupe of a 4x copy).

One implementation of the ``PREFIX-YYYYMM-NNNN`` document numbering previously
duplicated across ``api/endpoints/documents.py``, ``services/print_service.py``,
``services/shipping_service.py`` and ``services/process_sheet_service.py``. The four
call sites now delegate here; behavior is the hardened superset of the copies:

- ``document_number`` is a globally-unique column and the scan is intentionally
  UNSCOPED (the numbering space is global), so the advisory lock is global too
  (``company_id=None``). Two concurrent writers can't compute the same number and
  collide on the unique constraint. The lock is transaction-scoped and released on
  the caller's commit.
- The int parse is guarded (a malformed legacy number restarts the month at 1 rather
  than 500ing) — the print/shipping/process-sheet copies already did this.
- Known quirks are SHARED on purpose, exactly as every copy behaved: past 9999 the
  ``%04d`` suffix simply grows a digit (``-10000``), and the month rolls the sequence
  over because the LIKE prefix pins ``YYYYMM``.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.document import Document


def generate_document_number(db: Session, doc_type: str) -> str:
    """Next sequential document number for ``doc_type`` (``PREFIX-YYYYMM-NNNN``)."""
    acquire_generator_lock(db, "document_number")
    prefix = doc_type[:3].upper()
    today = datetime.now().strftime("%Y%m")
    last_doc = (
        db.query(Document)
        .filter(Document.document_number.like(f"{prefix}-{today}-%"))
        .order_by(Document.document_number.desc())
        .first()
    )
    new_num = 1
    if last_doc:
        try:
            new_num = int(last_doc.document_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            new_num = 1
    return f"{prefix}-{today}-{new_num:04d}"

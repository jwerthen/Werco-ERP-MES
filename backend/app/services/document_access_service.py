"""Writes and reviews behind restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

The read rule is :mod:`app.services.document_access`. This module holds what changes or
judges a document's ``access_level``:

* :func:`resolve_upload_access_level` -- the manual upload's level: what the uploader asked
  for, raised (never lowered) to at least the predecessor revision's level, and to
  ``export_controlled`` when the linked part's customer resolves as export-controlled through
  ``export_control_service.build_controlled_index`` (the same index the AI-external
  connector's redaction uses, so "controlled" means one thing in the app).
* :func:`change_document_access_level` -- ``POST /documents/{id}/access-level``.
* :func:`document_access_review` -- ``GET /documents/access-review``: documents whose part's
  customer is export-controlled but whose level is not, plus how many documents have no part
  link and so cannot be judged.

Every OTHER writer of ``documents`` (receiving certificates and labels, carrier labels and
bills of lading, laser-nest drawings, process-sheet step evidence, Hank intake) stays at the
``standard`` default: those are system-generated or floor-captured records, and raising them
is a person's ``POST /documents/{id}/access-level`` (a documented limitation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.export_control import REASON_EXPORT_CONTROLLED_CUSTOMER
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.customer import Customer
from app.models.document import Document, DocumentAccessLevel
from app.models.part import Part
from app.services.audit_service import AuditService
from app.services.document_access import higher_access_level, visible_document_or_404, visible_documents_clause
from app.services.export_control_redaction import Control
from app.services.export_control_service import build_controlled_index

# Why the server raised an upload above what was asked (``DocumentUploadResponse.access_level_raised_because``).
RAISED_BY_PREDECESSOR = "predecessor_revision"
RAISED_BY_EXPORT_CONTROLLED_CUSTOMER = "export_controlled_customer"

ACCESS_LEVEL_UNCHANGED_DETAIL = "The document is already at that access level"


def _export_controlled_part_controls(db: Session, company_id: int) -> Dict[int, Control]:
    """part id -> its control, for the company's parts whose customer resolves as export-controlled.

    UNLINKABLE (no customer, or a name no customer carries) is NOT export-controlled here: the
    raise rule and the review act on a positive finding only.
    """
    index = build_controlled_index(db, company_id)
    return {
        part_id: control
        for part_id, control in index.controlled.get("part", {}).items()
        if control.reason == REASON_EXPORT_CONTROLLED_CUSTOMER
    }


def part_is_export_controlled(db: Session, company_id: int, part_id: Optional[int]) -> bool:
    """True when ``part_id``'s customer resolves as export-controlled (``build_controlled_index``)."""
    if not part_id:
        return False
    return int(part_id) in _export_controlled_part_controls(db, company_id)


@dataclass(frozen=True)
class UploadAccessLevel:
    """The level a manual upload is stored at, and why it is higher than requested (if it is)."""

    requested: str
    access_level: str
    raised_because: Tuple[str, ...] = ()

    @property
    def raised(self) -> bool:
        return self.access_level != self.requested


def resolve_upload_access_level(
    db: Session,
    company_id: int,
    *,
    requested: Optional[str],
    part_id: Optional[int],
    previous: Optional[Document],
) -> UploadAccessLevel:
    """The level a manual upload is stored at. Raise-only: the server never stores below ``requested``.

    * ``requested`` (``None`` = ``standard``);
    * at least ``previous.access_level`` -- a revision is never easier to open than the one it
      supersedes;
    * ``export_controlled`` when the linked part's customer resolves as export-controlled.
    """
    asked = requested or DocumentAccessLevel.STANDARD.value
    level = asked
    because: List[str] = []
    if previous is not None:
        inherited = higher_access_level(level, previous.access_level)
        if inherited != level:
            level = inherited
            because.append(RAISED_BY_PREDECESSOR)
    if level != DocumentAccessLevel.EXPORT_CONTROLLED.value and part_is_export_controlled(db, company_id, part_id):
        level = DocumentAccessLevel.EXPORT_CONTROLLED.value
        because.append(RAISED_BY_EXPORT_CONTROLLED_CUSTOMER)
    return UploadAccessLevel(requested=asked, access_level=level, raised_because=tuple(because))


def change_document_access_level(
    db: Session,
    *,
    company_id: int,
    user: Any,
    document_id: int,
    access_level: str,
    reason: str,
    audit: AuditService,
    authorized_user_count: int,
) -> Tuple[Document, str]:
    """Set a document's ``access_level``; returns ``(document, previous_level)``. Flushes, never commits.

    404 first when the caller may not open the document (so lowering an ``export_controlled``
    one takes an export-authorized person), 409 on a no-op, then a REQUIRED audit row carrying
    the old and new level and the reason -- the caller wraps this in ``required_audit``.
    """
    document = visible_document_or_404(db, company_id, user, document_id, for_update=True)
    previous = document.access_level
    if previous == access_level:
        raise HTTPException(status_code=409, detail=ACCESS_LEVEL_UNCHANGED_DETAIL)
    document.access_level = access_level
    db.flush()
    audit.log_required(
        action="ACCESS_LEVEL_CHANGE",
        resource_type="document",
        resource_id=document.id,
        resource_identifier=document.document_number,
        description=f"Changed access level of document {document.document_number} from {previous} to {access_level}",
        old_values={"access_level": previous},
        new_values={"access_level": access_level},
        extra_data={"reason": reason, "authorized_user_count": authorized_user_count},
    )
    return document, previous


@dataclass
class AccessReviewRow:
    document_id: int
    document_number: str
    revision: Optional[str]
    title: str
    document_type: str
    access_level: str
    part_id: int
    part_number: Optional[str]
    customer_id: Optional[int]
    customer_name: Optional[str]
    classification: str


@dataclass
class AccessReview:
    documents: List[AccessReviewRow] = field(default_factory=list)
    unlinked_document_count: int = 0


def document_access_review(db: Session, company_id: int, user: Any) -> AccessReview:
    """Documents that SHOULD probably be ``export_controlled`` and are not, and the ones nobody can judge.

    Listed: every document of the company that the caller may open, whose part's customer
    resolves as export-controlled (``build_controlled_index``), and whose level is below
    ``export_controlled``. Counted, not listed: documents with no part link and a level below
    ``export_controlled`` -- the index cannot place them, so a person has to. Pure read.
    """
    controlled = _export_controlled_part_controls(db, company_id)
    below = Document.access_level != DocumentAccessLevel.EXPORT_CONTROLLED.value
    rows: List[AccessReviewRow] = []
    if controlled:
        documents = (
            tenant_query(db, Document, company_id)
            .filter(
                below,
                visible_documents_clause(user),
                Document.part_id.in_(sorted(controlled)),
            )
            .order_by(Document.id)
            .all()
        )
        part_ids = sorted({doc.part_id for doc in documents if doc.part_id is not None})
        parts = {
            row.id: row
            for row in (
                tenant_filter(db.query(Part.id, Part.part_number), Part, company_id).filter(Part.id.in_(part_ids)).all()
                if part_ids
                else []
            )
        }
        customer_ids = sorted({cid for cid in (controlled[pid].customer_id for pid in part_ids) if cid is not None})
        # Soft-deleted customers deliberately included: a deleted customer still names its history.
        customers = {
            row.id: row.name
            for row in (
                tenant_filter(db.query(Customer.id, Customer.name), Customer, company_id)
                .filter(Customer.id.in_(customer_ids))
                .all()
                if customer_ids
                else []
            )
        }
        for doc in documents:
            control = controlled[doc.part_id]
            part = parts.get(doc.part_id)
            rows.append(
                AccessReviewRow(
                    document_id=doc.id,
                    document_number=doc.document_number,
                    revision=doc.revision,
                    title=doc.title,
                    document_type=getattr(doc.document_type, "value", doc.document_type),
                    access_level=doc.access_level,
                    part_id=doc.part_id,
                    part_number=part.part_number if part else None,
                    customer_id=control.customer_id,
                    customer_name=customers.get(control.customer_id) if control.customer_id else None,
                    classification=control.classification,
                )
            )
    unlinked = (
        tenant_query(db, Document, company_id)
        .filter(below, visible_documents_clause(user), Document.part_id.is_(None))
        .count()
    )
    return AccessReview(documents=rows, unlinked_document_count=unlinked)

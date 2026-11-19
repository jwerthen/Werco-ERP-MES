"""Writes and reviews behind restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

The read rule is :mod:`app.services.document_access`. This module holds what changes or
judges a document's ``access_level``:

* :func:`resolve_upload_access_level` -- the level a manual upload or a Hank intake filing is
  stored at: what was asked for, raised (never lowered) to at least the predecessor revision's
  level, and to ``export_controlled`` when the linked part's or work order's customer resolves
  as export-controlled through ``export_control_service.build_controlled_index`` (the same
  index the AI-external connector's redaction uses, so "controlled" means one thing in the app).
* :func:`lift_revision_chain` -- a new revision stored above its chain's level lifts every
  earlier revision to it (a chain moves together).
* :func:`change_document_access_level` -- ``POST /documents/{id}/access-level``, applied to the
  whole revision chain.
* :func:`document_access_review` -- ``GET /documents/access-review``: documents whose part's
  customer is export-controlled but whose level is not, plus how many documents have no part
  link and so cannot be judged.

Every OTHER writer of ``documents`` (receiving certificates and labels, carrier labels and
bills of lading, laser-nest drawings, process-sheet step evidence) stays at the ``standard``
default: those are system-generated or floor-captured records, and raising them is a person's
``POST /documents/{id}/access-level`` (a documented limitation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.export_control import REASON_EXPORT_CONTROLLED_CUSTOMER
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.customer import Customer
from app.models.document import Document, DocumentAccessLevel
from app.models.part import Part
from app.services.audit_service import AuditService
from app.services.document_access import (
    access_level_rank,
    can_view_document,
    higher_access_level,
    revision_chain_ids,
    visible_document_or_404,
    visible_documents_clause,
)
from app.services.export_control_redaction import Control
from app.services.export_control_service import build_controlled_index

# Why the server raised an upload above what was asked (``DocumentUploadResponse.access_level_raised_because``).
RAISED_BY_PREDECESSOR = "predecessor_revision"
RAISED_BY_EXPORT_CONTROLLED_CUSTOMER = "export_controlled_customer"

ACCESS_LEVEL_UNCHANGED_DETAIL = "The document is already at that access level"
CHAIN_NOT_VISIBLE_DETAIL = (
    "Some revisions of this document are not available to you, so its access level cannot be lowered. "
    "A revision history changes level together; ask someone who can open every revision."
)


def _export_controlled_controls(db: Session, company_id: int, record_type: str) -> Dict[int, Control]:
    """record id -> its control, for the company's ``record_type`` rows ("part" / "work_order")
    whose customer resolves as export-controlled.

    UNLINKABLE (no customer, or a name no customer carries) is NOT export-controlled here: the
    raise rule and the review act on a positive finding only.
    """
    index = build_controlled_index(db, company_id)
    return {
        record_id: control
        for record_id, control in index.controlled.get(record_type, {}).items()
        if control.reason == REASON_EXPORT_CONTROLLED_CUSTOMER
    }


def _export_controlled_part_controls(db: Session, company_id: int) -> Dict[int, Control]:
    return _export_controlled_controls(db, company_id, "part")


def links_are_export_controlled(
    db: Session, company_id: int, *, part_id: Optional[int], work_order_id: Optional[int]
) -> bool:
    """True when the linked part's or work order's customer resolves as export-controlled.

    Builds the controlled index once, and only when there is a link to judge.
    """
    if not part_id and not work_order_id:
        return False
    index = build_controlled_index(db, company_id)
    for record_type, record_id in (("part", part_id), ("work_order", work_order_id)):
        if not record_id:
            continue
        control = index.controlled.get(record_type, {}).get(int(record_id))
        if control is not None and control.reason == REASON_EXPORT_CONTROLLED_CUSTOMER:
            return True
    return False


def part_is_export_controlled(db: Session, company_id: int, part_id: Optional[int]) -> bool:
    """True when ``part_id``'s customer resolves as export-controlled (``build_controlled_index``)."""
    return links_are_export_controlled(db, company_id, part_id=part_id, work_order_id=None)


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
    work_order_id: Optional[int] = None,
) -> UploadAccessLevel:
    """The level a new document is stored at. Raise-only: the server never stores below ``requested``.

    * ``requested`` (``None`` = ``standard``);
    * at least ``previous.access_level`` -- a revision is never easier to open than the one it
      supersedes;
    * ``export_controlled`` when the linked part's or work order's customer resolves as
      export-controlled.
    """
    asked = requested or DocumentAccessLevel.STANDARD.value
    level = asked
    because: List[str] = []
    if previous is not None:
        inherited = higher_access_level(level, previous.access_level)
        if inherited != level:
            level = inherited
            because.append(RAISED_BY_PREDECESSOR)
    if level != DocumentAccessLevel.EXPORT_CONTROLLED.value and links_are_export_controlled(
        db, company_id, part_id=part_id, work_order_id=work_order_id
    ):
        level = DocumentAccessLevel.EXPORT_CONTROLLED.value
        because.append(RAISED_BY_EXPORT_CONTROLLED_CUSTOMER)
    return UploadAccessLevel(requested=asked, access_level=level, raised_because=tuple(because))


# How many times a chain is re-walked after its locks are granted before the writer gives up.
CHAIN_LOCK_PASSES = 5
CHAIN_CHANGED_DETAIL = "The revision history changed while this change was being made; retry."


def lock_revision_chain(db: Session, company_id: int, document_id: int) -> List[Document]:
    """Row-lock every revision in ``document_id``'s chain, in ascending id order, and return them.

    One lock order for every writer that changes a chain's level (the access-level verb, a new
    revision's upload, an attach that raises), so two of them on one chain queue rather than
    deadlock. Refreshed from the database (``populate_existing``) so the levels judged are the
    locked ones.

    Membership is RE-WALKED after the locks are granted, and any revision found then is locked
    too, until a walk finds nothing new: under READ COMMITTED a revision committed while this
    writer waited (an upload holding the chain through its file save) is invisible to the first
    walk, and a level change that missed it would leave the newest revision at the old level.
    Each walk is a fresh statement, so it sees what committed during the wait. Gives up with
    **409** after :data:`CHAIN_LOCK_PASSES` walks that keep finding new members.
    """
    locked: Dict[int, Document] = {}
    for _ in range(CHAIN_LOCK_PASSES):
        chain_ids = revision_chain_ids(db, company_id, document_id)
        new_ids = [doc_id for doc_id in chain_ids if doc_id not in locked]
        if not new_ids:
            return [locked[doc_id] for doc_id in chain_ids]
        for row in (
            tenant_query(db, Document, company_id)
            .filter(Document.id.in_(new_ids))
            .order_by(Document.id)
            .with_for_update()
            .populate_existing()
            .all()
        ):
            locked[row.id] = row
    raise HTTPException(status_code=409, detail=CHAIN_CHANGED_DETAIL)


def _audit_chain_level(
    audit: AuditService,
    document: Document,
    *,
    previous_level: str,
    level: str,
    reason: str,
    chain_change: Dict[str, Any],
    authorized_user_count: Optional[int] = None,
) -> None:
    extra: Dict[str, Any] = {"reason": reason, "chain_change": chain_change}
    if authorized_user_count is not None:
        extra["authorized_user_count"] = authorized_user_count
    audit.log_required(
        action="ACCESS_LEVEL_CHANGE",
        resource_type="document",
        resource_id=document.id,
        resource_identifier=document.document_number,
        description=f"Changed access level of document {document.document_number} from {previous_level} to {level}",
        old_values={"access_level": previous_level},
        new_values={"access_level": level},
        extra_data=extra,
    )


def lift_revision_chain(
    db: Session,
    *,
    chain: Sequence[Document],
    new_document: Document,
    audit: AuditService,
) -> List[int]:
    """Raise every revision in ``chain`` below ``new_document``'s level up to it. Never lowers.

    A chain moves together, so a new revision stored above its history's level lifts the
    history with it: one required ``ACCESS_LEVEL_CHANGE`` audit row per lifted revision, marked
    ``chain_change`` with the new revision that caused it. Flushes, never commits; ``chain``
    must already be locked (:func:`lock_revision_chain`). Returns the lifted ids.
    """
    level = new_document.access_level
    lifted: List[int] = []
    members = [member for member in chain if member.id != new_document.id]
    for member in members:
        if access_level_rank(member.access_level) >= access_level_rank(level):
            continue
        previous_level = member.access_level
        member.access_level = level
        db.flush()
        _audit_chain_level(
            audit,
            member,
            previous_level=previous_level,
            level=level,
            reason=f"Raised with new revision {new_document.document_number} rev {new_document.revision}",
            chain_change={
                "trigger": "new_revision",
                "new_document_id": new_document.id,
                "chain_size": len(members) + 1,
            },
        )
        lifted.append(member.id)
    return lifted


RAISED_FOR_WORK_ORDER_REASON = "work order customer is export-controlled"


def raise_for_work_order_link(
    db: Session, *, company_id: int, document: Document, work_order_id: int, audit: AuditService
) -> List[int]:
    """The raise rule for a document newly linked to a work order (attach): when the work order's
    customer resolves as export-controlled, raise the document and its revision chain to
    ``export_controlled``. Never lowers.

    The same rule an upload carrying that ``work_order_id`` applies (:func:`resolve_upload_access_level`),
    so the two doors to the same end state store the same level. One required
    ``ACCESS_LEVEL_CHANGE`` row per raised revision, reason ``work order customer is
    export-controlled``, marked ``chain_change``. Flushes, never commits. Returns the raised ids.
    """
    if not links_are_export_controlled(db, company_id, part_id=None, work_order_id=work_order_id):
        return []
    level = DocumentAccessLevel.EXPORT_CONTROLLED.value
    chain = lock_revision_chain(db, company_id, document.id) or [document]
    raised: List[int] = []
    for member in chain:
        if access_level_rank(member.access_level) >= access_level_rank(level):
            continue
        previous_level = member.access_level
        member.access_level = level
        db.flush()
        _audit_chain_level(
            audit,
            member,
            previous_level=previous_level,
            level=level,
            reason=RAISED_FOR_WORK_ORDER_REASON,
            chain_change={
                "trigger": "work_order_link",
                "work_order_id": work_order_id,
                "addressed_document_id": document.id,
                "chain_size": len(chain),
            },
        )
        raised.append(member.id)
    return raised


@dataclass
class AccessLevelChange:
    document: Document
    previous_access_level: str
    changed_document_ids: List[int]
    chain_size: int


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
) -> AccessLevelChange:
    """Set the ``access_level`` of a document AND every revision in its chain. Flushes, never commits.

    In order: **404** when the caller may not open the addressed document (so lowering an
    ``export_controlled`` one takes an export-authorized person); the chain is locked in id
    order; **409** when any revision would be LOWERED and the caller may not open every
    revision; **409** when no revision changes. Then one REQUIRED ``ACCESS_LEVEL_CHANGE`` audit
    row per changed revision, each with the old and new level, the reason and a
    ``chain_change`` marker -- the caller wraps this in ``required_audit``.
    """
    visible_document_or_404(db, company_id, user, document_id)
    chain = lock_revision_chain(db, company_id, document_id)
    by_id = {member.id: member for member in chain}
    document = by_id.get(document_id)
    if document is None or not can_view_document(user, document):
        # Changed (or deleted) between the visibility read and the lock.
        raise HTTPException(status_code=404, detail="Document not found")
    previous = document.access_level
    to_change = [member for member in chain if member.access_level != access_level]
    if not to_change:
        raise HTTPException(status_code=409, detail=ACCESS_LEVEL_UNCHANGED_DETAIL)
    lowering = any(access_level_rank(member.access_level) > access_level_rank(access_level) for member in to_change)
    if lowering and not all(can_view_document(user, member) for member in chain):
        raise HTTPException(status_code=409, detail=CHAIN_NOT_VISIBLE_DETAIL)
    changed: List[int] = []
    for member in to_change:
        member_previous = member.access_level
        member.access_level = access_level
        db.flush()
        _audit_chain_level(
            audit,
            member,
            previous_level=member_previous,
            level=access_level,
            reason=reason,
            chain_change={
                "trigger": "access_level_change",
                "addressed_document_id": document_id,
                "chain_size": len(chain),
            },
            authorized_user_count=authorized_user_count,
        )
        changed.append(member.id)
    return AccessLevelChange(
        document=document,
        previous_access_level=previous,
        changed_document_ids=changed,
        chain_size=len(chain),
    )


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
                visible_documents_clause(user, company_id),
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
        .filter(below, visible_documents_clause(user, company_id), Document.part_id.is_(None))
        .count()
    )
    return AccessReview(documents=rows, unlinked_document_count=unlinked)

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session, joinedload, with_loader_criteria

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.schemas.qms_standard import (
    AutoLinkSummary,
    ClauseAutoEvidenceResponse,
    QMSAuditReadinessSummary,
    QMSClauseBulkCreate,
    QMSClauseCreate,
    QMSClauseResponse,
    QMSClauseUpdate,
    QMSEvidenceCreate,
    QMSEvidenceResponse,
    QMSEvidenceUpdate,
    QMSStandardCreate,
    QMSStandardListResponse,
    QMSStandardResponse,
    QMSStandardUpdate,
)
from app.services.audit_service import AuditService
from app.services.auto_evidence_service import (
    compute_overall_compliance,
    discover_evidence_for_clause,
    is_evaluation_failed,
)
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError
from app.services.qms_extraction_service import extract_clauses_with_llm

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== QMS Standards ==============


@router.get("/", response_model=List[QMSStandardListResponse])
def list_standards(
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all QMS standards with compliance summary counts."""
    query = db.query(QMSStandard).filter(QMSStandard.company_id == company_id, QMSStandard.is_deleted == False)
    if active_only:
        query = query.filter(QMSStandard.is_active == True)

    standards = query.order_by(QMSStandard.name).all()
    results = []
    for std in standards:
        clauses = (
            db.query(QMSClause)
            .filter(
                QMSClause.standard_id == std.id,
                QMSClause.company_id == company_id,
                QMSClause.is_deleted == False,
            )
            .all()
        )
        statuses = [c.compliance_status for c in clauses]
        results.append(
            QMSStandardListResponse(
                id=std.id,
                name=std.name,
                version=std.version,
                description=std.description,
                standard_body=std.standard_body,
                is_active=std.is_active,
                total_clauses=len(clauses),
                compliant_clauses=statuses.count("compliant"),
                partial_clauses=statuses.count("partial"),
                non_compliant_clauses=statuses.count("non_compliant"),
                not_assessed_clauses=statuses.count("not_assessed"),
                created_at=std.created_at,
            )
        )
    return results


@router.post("/", response_model=QMSStandardResponse, status_code=status.HTTP_201_CREATED)
def create_standard(
    data: QMSStandardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new QMS standard."""
    standard = QMSStandard(
        **data.model_dump(),
        created_by=current_user.id,
    )
    standard.company_id = company_id
    db.add(standard)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    audit.log_create("qms_standard", standard.id, standard.name, new_values=standard)
    db.commit()
    db.refresh(standard)
    return standard


@router.post("/{standard_id}/upload-pdf", response_model=List[QMSClauseResponse])
async def upload_pdf_and_extract_clauses(
    standard_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Upload a QMS standard PDF (quality manual, AS9100D, ISO 9001, etc.)
    and automatically extract all clauses using AI.
    """
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    # Validate file
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 20MB.")

    # Extract text from PDF
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        pdf_text = "\n\n".join(pages_text)
    except Exception as e:
        logger.error(f"PDF parsing failed: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {str(e)}")

    if not pdf_text or len(pdf_text.strip()) < 50:
        raise HTTPException(
            status_code=400, detail="Could not extract text from PDF. The file may be scanned/image-based or empty."
        )

    # The prompt, the model call and the JSON parse live in qms_extraction_service
    # (prompt text: QMS_CLAUSE_EXTRACTION_TEMPLATE in app/services/prompts/qms.py).
    # Its exceptions propagate unchanged, so the mapping below is the HTTP contract.
    try:
        extraction = extract_clauses_with_llm(
            pdf_text,
            name=standard.name,
            version=standard.version,
            company_id=company_id,
        )
        extracted = extraction.clauses

    except LLMNotConfiguredError as e:
        detail = (
            "AI extraction library not available"
            if e.reason == "library"
            else "AI extraction not configured (ANTHROPIC_API_KEY missing)"
        )
        raise HTTPException(status_code=500, detail=detail)
    except LLMEgressDisabledError:
        # CUI kill switch is off for this company — a policy decision, not a
        # server error. Surface as 403 with a clear, actionable message.
        raise HTTPException(
            status_code=403,
            detail="AI extraction is disabled for your company (allow_ai_egress is off).",
        )
    except json.JSONDecodeError as e:
        logger.error(f"AI returned invalid JSON: {e}")
        raise HTTPException(
            status_code=500, detail="AI extraction returned invalid data. Try again or use manual entry."
        )
    except Exception as e:
        logger.error(f"AI clause extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"AI extraction failed: {str(e)}")

    # Create clauses in database
    clauses = []
    for i, item in enumerate(extracted):
        clause_number = str(item.get("clause_number", f"{i+1}")).strip()
        title = str(item.get("title", "")).strip()
        description = str(item.get("description", "")).strip()

        if not clause_number or not title:
            continue

        clause = QMSClause(
            standard_id=standard_id,
            company_id=company_id,
            clause_number=clause_number,
            title=title[:500],
            description=description,
            sort_order=i,
        )
        db.add(clause)
        clauses.append(clause)

    db.flush()  # assign clause PKs without committing

    audit.log_create(
        "qms_standard",
        standard.id,
        standard.name,
        description=f"Extracted {len(clauses)} clauses from PDF",
    )

    db.commit()
    for c in clauses:
        db.refresh(c)

    logger.info(
        "Extracted %s clauses from PDF for standard %s using %s (%s)",
        len(clauses),
        standard.name,
        extraction.model,
        extraction.model_selection_reason,
    )
    return clauses


@router.get("/audit-readiness", response_model=QMSAuditReadinessSummary)
def get_audit_readiness(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get audit readiness summary across all active standards."""
    active_standards = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .count()
    )

    clauses = (
        db.query(QMSClause)
        .join(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
            QMSStandard.is_deleted == False,
            QMSClause.company_id == company_id,
            QMSClause.is_deleted == False,
        )
        .all()
    )

    statuses = [c.compliance_status for c in clauses]
    total = len(clauses)
    compliant = statuses.count("compliant")
    partial = statuses.count("partial")
    non_compliant = statuses.count("non_compliant")
    not_assessed = statuses.count("not_assessed")
    not_applicable = statuses.count("not_applicable")

    assessable = total - not_applicable
    compliance_pct = (compliant / assessable * 100) if assessable > 0 else 0.0

    total_evidence = (
        db.query(QMSClauseEvidence)
        .join(QMSClause)
        .join(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
            QMSStandard.is_deleted == False,
            QMSClause.is_deleted == False,
            QMSClauseEvidence.company_id == company_id,
            QMSClauseEvidence.is_deleted == False,
        )
        .count()
    )
    verified_evidence = (
        db.query(QMSClauseEvidence)
        .join(QMSClause)
        .join(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
            QMSStandard.is_deleted == False,
            QMSClause.is_deleted == False,
            QMSClauseEvidence.company_id == company_id,
            QMSClauseEvidence.is_deleted == False,
            QMSClauseEvidence.is_verified == True,
        )
        .count()
    )

    now = datetime.utcnow()
    overdue_reviews = (
        db.query(QMSClause)
        .join(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
            QMSStandard.is_deleted == False,
            QMSClause.company_id == company_id,
            QMSClause.is_deleted == False,
            QMSClause.next_review_date != None,
            QMSClause.next_review_date < now,
        )
        .count()
    )

    return QMSAuditReadinessSummary(
        total_standards=active_standards,
        total_clauses=total,
        compliant=compliant,
        partial=partial,
        non_compliant=non_compliant,
        not_assessed=not_assessed,
        not_applicable=not_applicable,
        compliance_percentage=round(compliance_pct, 1),
        total_evidence_links=total_evidence,
        verified_evidence=verified_evidence,
        unverified_evidence=total_evidence - verified_evidence,
        clauses_needing_review=overdue_reviews,
    )


def _standard_with_clause_tree(db: Session, standard_id: int, company_id: int) -> QMSStandard | None:
    """The live standard of the active company, loaded for a ``QMSStandardResponse``.

    Every response that serializes a standard's clause tree goes through here (``GET`` and the
    ``PUT`` response), so its clauses, their ``sub_clauses`` and their evidence are the active
    company's live rows only. A plain ``db.refresh`` would lazy-load all three unfiltered.
    """
    return (
        db.query(QMSStandard)
        .options(
            joinedload(QMSStandard.clauses).joinedload(QMSClause.evidence_links),
            # Only live, own-company clauses/evidence in the nested payload, sub_clauses included.
            *_clause_tree_criteria(company_id),
        )
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .populate_existing()  # see _clause_tree_criteria
        .first()
    )


@router.get("/{standard_id}", response_model=QMSStandardResponse)
def get_standard(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a QMS standard with all its clauses and evidence."""
    standard = _standard_with_clause_tree(db, standard_id, company_id)
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")
    return standard


@router.put("/{standard_id}", response_model=QMSStandardResponse)
def update_standard(
    standard_id: int,
    data: QMSStandardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a QMS standard."""
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    old_values = {c.key: getattr(standard, c.key) for c in standard.__table__.columns}

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(standard, key, value)

    # Write the row change before the audit row: ``AuditService.log`` takes the global
    # audit-chain lock as its first statement, so an unflushed change would reach the database
    # only after it, while this request held the chain lock (the opposite of the codebase order).
    # A new ``document_id`` takes its key-share lock on the document row here, ahead of the chain
    # lock, which is the order ``DELETE /documents/{id}`` takes them in.
    db.flush()
    audit.log_update("qms_standard", standard.id, standard.name, old_values=old_values, new_values=standard)
    db.commit()
    updated = _standard_with_clause_tree(db, standard.id, company_id)
    if updated is None:  # deleted by a concurrent request between the commit and this reload
        raise HTTPException(status_code=404, detail="QMS standard not found")
    return updated


@router.delete("/{standard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_standard(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete a QMS standard and all its clauses/evidence (Admin only)."""
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    audit.log_delete("qms_standard", standard.id, standard.name, old_values=standard, soft_delete=True)
    standard.soft_delete(current_user.id)
    db.commit()


# ============== QMS Clauses ==============

# One refusal for every parent id that is not a live clause of the active company in the same
# standard: an id that does not exist, another company's clause, a deleted clause and a clause of
# another standard all get these exact bytes, so the answer says nothing about other tenants.
PARENT_CLAUSE_NOT_FOUND = "Parent clause not found"
PARENT_CLAUSE_IS_SELF = "A clause cannot be its own parent clause"
PARENT_CLAUSE_IS_DESCENDANT = "A clause cannot be placed under one of its own sub-clauses"
QMS_CLAUSE_PARENT_LOCK = "qms_clause_parent"


def _clause_tree_criteria(company_id: int) -> list:
    """Loader criteria for every response that serializes a clause tree.

    ``QMSClauseResponse`` renders the self-referential ``sub_clauses`` backref (and each
    sub-clause's ``evidence_links``), which is loaded by ``parent_clause_id`` alone. These
    criteria propagate to those lazy loads, so a sub-clause renders only when it is live and
    belongs to the active company. The company half is what keeps a clause another company
    grafted under one of ours (a ``parent_clause_id`` written before the write-side check
    existed) out of our reads, with its evidence.

    Pair them with ``populate_existing()``. The criteria ride on each instance's load options,
    which a query sets only on an instance it loads fresh: a clause already in the session when
    the query runs would keep lazy-loading ``sub_clauses`` without them, so the tenant filter
    would silently depend on what the request happened to load first.
    """
    return [
        with_loader_criteria(QMSClause, (QMSClause.company_id == company_id) & (QMSClause.is_deleted == False)),
        with_loader_criteria(
            QMSClauseEvidence,
            (QMSClauseEvidence.company_id == company_id) & (QMSClauseEvidence.is_deleted == False),
        ),
    ]


def _assert_parent_clauses(
    db: Session, parent_clause_ids: List[int | None], *, company_id: int, standard_id: int
) -> None:
    """Refuse any ``parent_clause_id`` that is not a live clause of the active company in ``standard_id``.

    ``qms_clauses.parent_clause_id`` is a plain self-referencing foreign key, so the database
    alone accepts any clause row, another company's included, and the ``sub_clauses`` backref
    then renders the child under that parent in the other company's reads. One indexed read
    resolves every distinct id; any id it does not return refuses the whole request 404
    ``Parent clause not found`` before anything is written. ``None`` is skipped: no parent (or,
    on an update, clearing it) is always allowed.
    """
    wanted = {pid for pid in parent_clause_ids if pid is not None}
    if not wanted:
        return
    found = {
        row.id
        for row in db.query(QMSClause.id).filter(
            QMSClause.id.in_(wanted),
            QMSClause.company_id == company_id,
            QMSClause.standard_id == standard_id,
            QMSClause.is_deleted == False,
        )
    }
    if found != wanted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PARENT_CLAUSE_NOT_FOUND)


def _assert_not_own_descendant(db: Session, clause_id: int, parent_clause_id: int, *, company_id: int) -> None:
    """Refuse a parent that is the clause itself or one of its descendants (422).

    Called only after ``_assert_parent_clauses`` has resolved ``parent_clause_id`` as one of the
    active company's clauses, so another company's id never reaches it. Either case would store a
    cycle, which fails response serialization and then every read of the standard.

    Walks the stored parent links up from the proposed parent, scoped to the company and NOT
    filtered on ``is_deleted``: a deleted clause's link is still stored, and a cycle through it
    would come back if the clause were ever restored. ``seen`` bounds the walk on a chain that is
    already cyclic from before this check. One primary-key read per level; clause trees are a
    handful deep. The caller holds the company's ``QMS_CLAUSE_PARENT_LOCK``, so the links it reads
    cannot be changed by another re-parent before this transaction commits.
    """
    if parent_clause_id == clause_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=PARENT_CLAUSE_IS_SELF)
    seen: set = set()
    current: int | None = parent_clause_id
    while current is not None and current not in seen:
        if current == clause_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=PARENT_CLAUSE_IS_DESCENDANT)
        seen.add(current)
        current = (
            db.query(QMSClause.parent_clause_id)
            .filter(QMSClause.id == current, QMSClause.company_id == company_id)
            .scalar()
        )


def _live_clause_or_404(db: Session, clause_id: int, company_id: int) -> QMSClause:
    """The active company's clause, or 404 ``Clause not found`` if it or its standard is removed.

    Removing a standard soft-deletes only the standard row. Its clauses keep ``is_deleted = false``
    but leave every read with it (the standard, clause-list and audit-readiness reads all require
    a live standard), so a clause of a removed standard is removed too. Every route that resolves
    a clause from a request goes through here, so none of them edits, removes, links evidence to
    or reports on such a clause. Before, ``PUT`` could still change its ``compliance_status`` and
    ``POST .../evidence`` could link evidence no screen would show.

    The standard is joined inside the active company, so a clause whose standard is not the
    company's (bad data) is not found either. The 404 is the same in every case. Both flags are
    judged in the ``WHERE``, by the database, whatever copy of either row the session holds.

    A plain read: the standard row is not locked. ``delete_standard`` writes its audit row, taking
    the global audit-chain lock, before its own row change, so a share lock held here while a
    write route waits for the chain lock would deadlock against it. A standard removed between
    this read and the caller's commit is not seen.
    """
    clause = (
        db.query(QMSClause)
        .join(QMSStandard, QMSStandard.id == QMSClause.standard_id)
        .filter(
            QMSClause.id == clause_id,
            QMSClause.company_id == company_id,
            QMSClause.is_deleted == False,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if clause is None:
        raise HTTPException(status_code=404, detail="Clause not found")
    return clause


@router.get("/{standard_id}/clauses", response_model=List[QMSClauseResponse])
def list_clauses(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all clauses for a standard (flat list, use parent_clause_id for tree).

    404 ``QMS standard not found`` unless the standard is a live one of the active company, as for
    ``GET /{standard_id}``. Removing a standard leaves its clauses' own flags clear, so without
    this a removed standard's clauses and evidence links were still listed here. A missing or
    another company's id answers the same 404 (it used to answer an empty list).
    """
    standard_live = (
        db.query(QMSStandard.id)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if standard_live is None:
        raise HTTPException(status_code=404, detail="QMS standard not found")
    clauses = (
        db.query(QMSClause)
        .options(
            joinedload(QMSClause.evidence_links),
            # QMSClauseResponse serializes the self-referential `sub_clauses` backref, which would
            # otherwise lazy-load soft-deleted and other companies' child clauses unfiltered.
            *_clause_tree_criteria(company_id),
        )
        .filter(
            QMSClause.standard_id == standard_id,
            QMSClause.company_id == company_id,
            QMSClause.is_deleted == False,
        )
        .order_by(QMSClause.sort_order, QMSClause.clause_number)
        .populate_existing()  # see _clause_tree_criteria
        .all()
    )
    return clauses


@router.post("/{standard_id}/clauses", response_model=QMSClauseResponse, status_code=status.HTTP_201_CREATED)
def create_clause(
    standard_id: int,
    data: QMSClauseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add a clause to a standard.

    A ``parent_clause_id`` must name a live clause of your company in the same standard. Any other
    id (one that does not exist, another company's, a deleted clause, a clause of another
    standard) is refused 404 ``Parent clause not found`` and nothing is written.
    """
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    _assert_parent_clauses(db, [data.parent_clause_id], company_id=company_id, standard_id=standard_id)

    clause = QMSClause(standard_id=standard_id, company_id=company_id, **data.model_dump())
    db.add(clause)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    audit.log_create("qms_clause", clause.id, clause.clause_number, new_values=clause)
    db.commit()
    db.refresh(clause)
    return clause


@router.post("/{standard_id}/clauses/bulk", response_model=List[QMSClauseResponse], status_code=status.HTTP_201_CREATED)
def bulk_create_clauses(
    standard_id: int,
    data: QMSClauseBulkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Bulk-import clauses for a standard (e.g., from a parsed document).

    Every ``parent_clause_id`` must name a live clause of your company already in this standard
    (clauses in the same request have no id yet). One that does not refuses the whole request 404
    ``Parent clause not found``, and no clause is created.
    """
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    _assert_parent_clauses(
        db, [c.parent_clause_id for c in data.clauses], company_id=company_id, standard_id=standard_id
    )

    clauses = []
    for i, clause_data in enumerate(data.clauses):
        clause = QMSClause(
            standard_id=standard_id,
            company_id=company_id,
            sort_order=clause_data.sort_order or i,
            **clause_data.model_dump(exclude={"sort_order"}),
        )
        db.add(clause)
        clauses.append(clause)

    db.flush()  # assign clause PKs without committing

    audit.log_create(
        "qms_standard",
        standard.id,
        standard.name,
        description=f"Bulk-imported {len(clauses)} clauses",
    )

    db.commit()
    for c in clauses:
        db.refresh(c)
    return clauses


@router.put("/clauses/{clause_id}", response_model=QMSClauseResponse)
def update_clause(
    clause_id: int,
    data: QMSClauseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a clause, including compliance status assessment.

    A non-null ``parent_clause_id`` must name a live clause of your company in the clause's own
    standard: any other id (one that does not exist, another company's, a deleted clause, a
    clause of another standard) is refused 404 ``Parent clause not found``. The clause itself,
    or one of its own sub-clauses at any depth, is refused 422. Either way nothing is written,
    the other fields in the same request included. ``null`` clears the parent. A request that
    does not send ``parent_clause_id`` is not judged on the value already stored.
    """
    clause = _live_clause_or_404(db, clause_id, company_id)

    update_data = data.model_dump(exclude_unset=True)
    # Before any attribute is set, so a refusal leaves the row and the audit log untouched.
    if update_data.get("parent_clause_id") is not None:
        # Serializes re-parenting within the company, so two opposite moves in flight at once
        # (C under P, P under C) cannot both pass the descendant walk: the second waits, then its
        # walk reads the first one's committed link. This is the only place that takes it, before
        # the request writes anything or reaches the audit chain lock, so it adds no lock-order
        # edge (the chain lock is the single-bigint advisory form; this is the two-int form, a
        # separate key space). No-op on SQLite.
        acquire_generator_lock(db, QMS_CLAUSE_PARENT_LOCK, company_id)
        _assert_parent_clauses(
            db, [update_data["parent_clause_id"]], company_id=company_id, standard_id=clause.standard_id
        )
        _assert_not_own_descendant(db, clause.id, update_data["parent_clause_id"], company_id=company_id)

    old_values = {c.key: getattr(clause, c.key) for c in clause.__table__.columns}
    old_status = clause.compliance_status

    # Track who assessed compliance
    if "compliance_status" in update_data:
        update_data["last_assessed_date"] = datetime.utcnow()
        update_data["last_assessed_by"] = current_user.id

    for key, value in update_data.items():
        setattr(clause, key, value)

    audit.log_update("qms_clause", clause.id, clause.clause_number, old_values=old_values, new_values=clause)
    if "compliance_status" in update_data and clause.compliance_status != old_status:
        audit.log_status_change("qms_clause", clause.id, clause.clause_number, old_status, clause.compliance_status)

    db.commit()
    # Reload through the same criteria as the reads: a plain refresh would lazy-load sub_clauses
    # unfiltered, rendering deleted children and any clause another company grafted under this one.
    return (
        db.query(QMSClause)
        .options(joinedload(QMSClause.evidence_links), *_clause_tree_criteria(company_id))
        .filter(QMSClause.id == clause.id, QMSClause.company_id == company_id)
        .populate_existing()
        .one()
    )


@router.delete("/clauses/{clause_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_clause(
    clause_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete a clause and its evidence links."""
    clause = _live_clause_or_404(db, clause_id, company_id)

    audit.log_delete("qms_clause", clause.id, clause.clause_number, old_values=clause, soft_delete=True)
    clause.soft_delete(current_user.id)
    db.commit()


# ============== Auto-Evidence Discovery ==============


@router.get("/clauses/{clause_id}/auto-evidence", response_model=ClauseAutoEvidenceResponse)
def get_clause_auto_evidence(
    clause_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Discover live ERP/MES evidence for a single clause.

    An evidence rule whose query fails is listed as a placeholder row (``health_status``
    ``not_computed``, ``suggested_compliance`` ``not_assessed``, zero placeholder counts), so the
    clause never rolls up ``compliant`` while part of its evidence could not be computed.
    ``not_computed`` means nothing is known about that module from this call; ``no_data`` means
    the query ran and found no records. Do not report a ``not_computed`` row's counts.
    """
    clause = _live_clause_or_404(db, clause_id, company_id)

    results = discover_evidence_for_clause(db, clause, company_id)
    overall = compute_overall_compliance(results)

    # Strip internal keys (_rule_id, _evaluation_failed) before returning. A rule whose query
    # failed is still listed, as its not_computed / not_assessed placeholder row.
    clean_results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]

    return ClauseAutoEvidenceResponse(
        clause_id=clause.id,
        clause_number=clause.clause_number,
        discovered_evidence=clean_results,
        overall_suggested_compliance=overall,
    )


@router.post("/{standard_id}/auto-link", response_model=AutoLinkSummary)
def auto_link_standard(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Run auto-discovery for ALL clauses in a standard and persist evidence links.
    Creates or updates QMSClauseEvidence records with is_auto_linked=True.

    A rule whose query failed this run is counted in ``compliance_summary`` (its clause
    rolls up ``not_assessed`` unless a computed row is ``non_compliant`` / ``partial``) and in
    ``total_evidence_failed``, but persists nothing: its stored evidence keeps the counts and
    ``last_refreshed`` of the last run that computed them, rather than being overwritten by a
    transient failure. It does not make its clause count as a clause with evidence. The audit
    row names every skipped (clause, rule) in ``extra_data.evidence_not_computed``, since the
    stale ``last_refreshed`` on those rows is otherwise unexplained.
    """
    standard = (
        db.query(QMSStandard)
        .filter(
            QMSStandard.id == standard_id,
            QMSStandard.company_id == company_id,
            QMSStandard.is_deleted == False,
        )
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    clauses = (
        db.query(QMSClause)
        .filter(
            QMSClause.standard_id == standard_id,
            QMSClause.company_id == company_id,
            QMSClause.is_deleted == False,
        )
        .all()
    )

    now = datetime.utcnow()
    total_created = 0
    total_updated = 0
    not_computed: List[dict] = []
    clauses_with_evidence = 0
    compliance_counts = {}

    for clause in clauses:
        results = discover_evidence_for_clause(db, clause, company_id)
        computed = [r for r in results if not is_evaluation_failed(r)]
        not_computed.extend(
            {"clause_id": clause.id, "clause_number": clause.clause_number, "rule_id": r["_rule_id"]}
            for r in results
            if is_evaluation_failed(r)
        )

        if computed:
            clauses_with_evidence += 1

        # The roll-up sees the failed rules' placeholder rows, so a clause whose rule failed
        # is summarized not_assessed (or worse), never compliant from its other rows.
        overall = compute_overall_compliance(results)
        compliance_counts[overall] = compliance_counts.get(overall, 0) + 1

        # Persistence sees only computed rows: a transient failure must not overwrite the
        # stored counts of the last run that could compute them.
        for result in computed:
            rule_id = result.get("_rule_id", result.get("evidence_type", "unknown"))

            # Check for existing auto-linked evidence of this type on this clause
            existing = (
                db.query(QMSClauseEvidence)
                .filter(
                    QMSClauseEvidence.clause_id == clause.id,
                    QMSClauseEvidence.company_id == company_id,
                    QMSClauseEvidence.is_deleted == False,
                    QMSClauseEvidence.is_auto_linked == True,
                    QMSClauseEvidence.auto_link_query == rule_id,
                )
                .first()
            )

            if existing:
                # Update existing auto-linked evidence with fresh data
                existing.title = result["title"]
                existing.description = result["description"]
                existing.module_reference = result["module_reference"]
                existing.live_count = result["total_count"]
                existing.last_refreshed = now
                total_updated += 1
            else:
                # Create new auto-linked evidence
                evidence = QMSClauseEvidence(
                    clause_id=clause.id,
                    company_id=company_id,
                    evidence_type=result["evidence_type"],
                    title=result["title"],
                    description=result["description"],
                    module_reference=result["module_reference"],
                    record_type=result.get("evidence_type"),
                    is_auto_linked=True,
                    auto_link_query=rule_id,
                    live_count=result["total_count"],
                    last_refreshed=now,
                    created_by=current_user.id,
                )
                db.add(evidence)
                total_created += 1

    db.flush()  # assign any new evidence PKs without committing

    total_failed = len(not_computed)
    audit.log_create(
        "qms_standard",
        standard.id,
        standard.name,
        description=(
            f"Auto-linked evidence: {total_created} created, {total_updated} updated"
            + (f", {total_failed} not computed (left unchanged)" if total_failed else "")
        ),
        extra_data={"evidence_not_computed": not_computed} if not_computed else None,
    )

    db.commit()

    logger.info(
        f"Auto-link for standard {standard.name}: "
        f"{total_created} created, {total_updated} updated, "
        f"{clauses_with_evidence}/{len(clauses)} clauses with evidence, "
        f"{total_failed} evidence rules not computed"
    )

    return AutoLinkSummary(
        standard_id=standard.id,
        standard_name=standard.name,
        total_clauses=len(clauses),
        clauses_with_evidence=clauses_with_evidence,
        clauses_without_evidence=len(clauses) - clauses_with_evidence,
        total_evidence_created=total_created,
        total_evidence_updated=total_updated,
        total_evidence_failed=total_failed,
        compliance_summary=compliance_counts,
    )


# ============== Evidence Links ==============


def _lock_live_evidence_or_404(db: Session, evidence_id: int, company_id: int) -> QMSClauseEvidence:
    """Lock the caller's evidence row and return it, or 404 if it, its clause or its standard is removed.

    For the evidence ``PUT`` and ``DELETE``. The row is read ``FOR UPDATE`` (a no-op on SQLite),
    so this request is serialized on the row with any other writer of it, including an auto-link
    run (``POST /{standard_id}/auto-link``), which writes the clause's auto-linked rows before
    its audit rows:

    * Lock order. The caller flushes its change before it writes its audit row, so it holds this
      row lock before the global audit-chain lock, the order auto-link and the rest of the
      codebase use. Before this, these routes read the row without a lock and let the audit row
      take the chain lock first, then waited for the row while holding it: a deadlock against
      auto-link (40P01, the app-wide 409), and a stall of every tenant's audited writes while
      the request waited.
    * No edit of a removed row. ``is_deleted`` is tested after the lock, on the row as it stands
      once the lock is held (``populate_existing`` re-reads a row already in the session), not
      in the ``WHERE``: a row that auto-link retired, or another person removed, while this
      request waited is a 404. Before, a Verify that read the row before a retirement committed
      could still mark it verified, leaving it verified and deleted, and answer 200.
    * No edit under a removed clause or standard. Removing a standard or clause soft-deletes only
      that row; its evidence keeps ``is_deleted = false``, every QMS read hides it with its
      parent, and ``DELETE /documents/{id}`` counts it as removed (it keeps its document). Such a
      row is a frozen record, so it answers the same 404 as a removed row, and nothing is written.
      Before, ``PUT`` could clear its ``document_id`` and so let that document be deleted. The
      clause routes refuse a clause of a removed standard for the same reason
      (``_live_clause_or_404``).

    The ``WHERE`` keeps the tenant filter, so another company's row is never locked. The clause
    and standard are read after the lock, also inside the tenant (a clause or standard that does
    not join is not found), as a plain read of their flags as committed now. They are not locked:
    ``delete_standard``, ``delete_clause`` and ``update_clause`` write their audit row, taking the
    global audit-chain lock, before their own row change, so a share lock here, held while this
    request waits for the chain lock, would deadlock against them. The cost is that a clause or
    standard removed after this read and before this request commits is not seen.
    """
    evidence = (
        db.query(QMSClauseEvidence)
        .filter(
            QMSClauseEvidence.id == evidence_id,
            QMSClauseEvidence.company_id == company_id,
        )
        .with_for_update()
        .populate_existing()
        .first()
    )
    if evidence is None or evidence.is_deleted:
        raise HTTPException(status_code=404, detail="Evidence not found")
    # Column reads, not entities: the flags come from the database, never a stale session copy.
    parents = (
        db.query(QMSClause.is_deleted.label("clause_deleted"), QMSStandard.is_deleted.label("standard_deleted"))
        .join(QMSStandard, QMSStandard.id == QMSClause.standard_id)
        .filter(
            QMSClause.id == evidence.clause_id,
            QMSClause.company_id == company_id,
            QMSStandard.company_id == company_id,
        )
        .first()
    )
    if parents is None or parents.clause_deleted or parents.standard_deleted:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return evidence


def _evidence_column_values(evidence: QMSClauseEvidence) -> Dict[str, Any]:
    """Every column of the evidence row as it stands now, for ``log_update`` / ``log_delete``'s ``old_values``.

    Raw values (datetimes included): those helpers serialize them. ``log_required`` stores
    ``old_values`` as given, so it needs JSON-ready values instead.
    """
    return {c.key: getattr(evidence, c.key) for c in evidence.__table__.columns}


@router.post("/clauses/{clause_id}/evidence", response_model=QMSEvidenceResponse, status_code=status.HTTP_201_CREATED)
def add_evidence(
    clause_id: int,
    data: QMSEvidenceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Link evidence to a clause."""
    # A clause of a removed standard is not found: evidence linked there could never be edited or
    # cleared (``_lock_live_evidence_or_404`` refuses it), so its document could never be deleted.
    _live_clause_or_404(db, clause_id, company_id)

    evidence = QMSClauseEvidence(
        clause_id=clause_id,
        company_id=company_id,
        **data.model_dump(),
        created_by=current_user.id,
    )
    db.add(evidence)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    audit.log_create("qms_clause_evidence", evidence.id, evidence.title, new_values=evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


@router.put("/evidence/{evidence_id}", response_model=QMSEvidenceResponse)
def update_evidence(
    evidence_id: int,
    data: QMSEvidenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update evidence, including verification.

    The row is locked before it is read and the change is flushed before the audit row is
    written, so this takes its row lock ahead of the global audit-chain lock. See
    ``_lock_live_evidence_or_404``.
    """
    evidence = _lock_live_evidence_or_404(db, evidence_id, company_id)

    old_values = _evidence_column_values(evidence)

    update_data = data.model_dump(exclude_unset=True)

    # Track who verified
    if "is_verified" in update_data and update_data["is_verified"]:
        update_data["verified_by"] = current_user.id
        update_data["verified_date"] = datetime.utcnow()

    for key, value in update_data.items():
        setattr(evidence, key, value)

    # Write the row change before the audit row: ``AuditService.log`` takes the global
    # audit-chain lock as its first statement, so an unflushed change would reach the database
    # only after it, while this request held the chain lock (the opposite of the codebase order).
    db.flush()
    audit.log_update("qms_clause_evidence", evidence.id, evidence.title, old_values=old_values, new_values=evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


@router.delete("/evidence/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Remove an evidence link.

    Locks the row, soft-deletes it and flushes that before the audit row is written, so the row
    lock comes before the global audit-chain lock. See ``_lock_live_evidence_or_404``.
    """
    evidence = _lock_live_evidence_or_404(db, evidence_id, company_id)

    # The audit row records the row as it was before the delete, so take its values first.
    old_values = _evidence_column_values(evidence)
    evidence.soft_delete(current_user.id)
    db.flush()
    audit.log_delete("qms_clause_evidence", evidence.id, evidence.title, old_values=old_values, soft_delete=True)
    db.commit()

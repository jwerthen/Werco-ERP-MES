from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.qms_standard import QMSStandard, QMSClause, QMSClauseEvidence
from app.schemas.qms_standard import (
    QMSStandardCreate, QMSStandardUpdate, QMSStandardResponse, QMSStandardListResponse,
    QMSClauseCreate, QMSClauseUpdate, QMSClauseResponse, QMSClauseBulkCreate,
    QMSEvidenceCreate, QMSEvidenceUpdate, QMSEvidenceResponse,
    QMSAuditReadinessSummary,
)

router = APIRouter()


# ============== QMS Standards ==============

@router.get("/", response_model=List[QMSStandardListResponse])
def list_standards(
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all QMS standards with compliance summary counts."""
    query = db.query(QMSStandard)
    if active_only:
        query = query.filter(QMSStandard.is_active == True)

    standards = query.order_by(QMSStandard.name).all()
    results = []
    for std in standards:
        clauses = db.query(QMSClause).filter(QMSClause.standard_id == std.id).all()
        statuses = [c.compliance_status for c in clauses]
        results.append(QMSStandardListResponse(
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
        ))
    return results


@router.post("/", response_model=QMSStandardResponse, status_code=status.HTTP_201_CREATED)
def create_standard(
    data: QMSStandardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Create a new QMS standard."""
    standard = QMSStandard(
        **data.model_dump(),
        created_by=current_user.id,
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


@router.get("/audit-readiness", response_model=QMSAuditReadinessSummary)
def get_audit_readiness(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get audit readiness summary across all active standards."""
    active_standards = db.query(QMSStandard).filter(QMSStandard.is_active == True).count()

    clauses = (
        db.query(QMSClause)
        .join(QMSStandard)
        .filter(QMSStandard.is_active == True)
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
        .filter(QMSStandard.is_active == True)
        .count()
    )
    verified_evidence = (
        db.query(QMSClauseEvidence)
        .join(QMSClause)
        .join(QMSStandard)
        .filter(QMSStandard.is_active == True, QMSClauseEvidence.is_verified == True)
        .count()
    )

    now = datetime.utcnow()
    overdue_reviews = (
        db.query(QMSClause)
        .join(QMSStandard)
        .filter(
            QMSStandard.is_active == True,
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


@router.get("/{standard_id}", response_model=QMSStandardResponse)
def get_standard(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a QMS standard with all its clauses and evidence."""
    standard = (
        db.query(QMSStandard)
        .options(
            joinedload(QMSStandard.clauses)
            .joinedload(QMSClause.evidence_links)
        )
        .filter(QMSStandard.id == standard_id)
        .first()
    )
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")
    return standard


@router.put("/{standard_id}", response_model=QMSStandardResponse)
def update_standard(
    standard_id: int,
    data: QMSStandardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Update a QMS standard."""
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(standard, key, value)

    db.commit()
    db.refresh(standard)
    return standard


@router.delete("/{standard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_standard(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
):
    """Delete a QMS standard and all its clauses/evidence (Admin only)."""
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")
    db.delete(standard)
    db.commit()


# ============== QMS Clauses ==============

@router.get("/{standard_id}/clauses", response_model=List[QMSClauseResponse])
def list_clauses(
    standard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all clauses for a standard (flat list, use parent_clause_id for tree)."""
    clauses = (
        db.query(QMSClause)
        .options(joinedload(QMSClause.evidence_links))
        .filter(QMSClause.standard_id == standard_id)
        .order_by(QMSClause.sort_order, QMSClause.clause_number)
        .all()
    )
    return clauses


@router.post("/{standard_id}/clauses", response_model=QMSClauseResponse, status_code=status.HTTP_201_CREATED)
def create_clause(
    standard_id: int,
    data: QMSClauseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Add a clause to a standard."""
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    clause = QMSClause(standard_id=standard_id, **data.model_dump())
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


@router.post("/{standard_id}/clauses/bulk", response_model=List[QMSClauseResponse], status_code=status.HTTP_201_CREATED)
def bulk_create_clauses(
    standard_id: int,
    data: QMSClauseBulkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Bulk-import clauses for a standard (e.g., from a parsed document)."""
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    clauses = []
    for i, clause_data in enumerate(data.clauses):
        clause = QMSClause(
            standard_id=standard_id,
            sort_order=clause_data.sort_order or i,
            **clause_data.model_dump(exclude={"sort_order"}),
        )
        db.add(clause)
        clauses.append(clause)

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
):
    """Update a clause, including compliance status assessment."""
    clause = db.query(QMSClause).filter(QMSClause.id == clause_id).first()
    if not clause:
        raise HTTPException(status_code=404, detail="Clause not found")

    update_data = data.model_dump(exclude_unset=True)

    # Track who assessed compliance
    if "compliance_status" in update_data:
        update_data["last_assessed_date"] = datetime.utcnow()
        update_data["last_assessed_by"] = current_user.id

    for key, value in update_data.items():
        setattr(clause, key, value)

    db.commit()
    db.refresh(clause)
    return clause


@router.delete("/clauses/{clause_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_clause(
    clause_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
):
    """Delete a clause and its evidence links."""
    clause = db.query(QMSClause).filter(QMSClause.id == clause_id).first()
    if not clause:
        raise HTTPException(status_code=404, detail="Clause not found")
    db.delete(clause)
    db.commit()


# ============== Evidence Links ==============

@router.post("/clauses/{clause_id}/evidence", response_model=QMSEvidenceResponse, status_code=status.HTTP_201_CREATED)
def add_evidence(
    clause_id: int,
    data: QMSEvidenceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Link evidence to a clause."""
    clause = db.query(QMSClause).filter(QMSClause.id == clause_id).first()
    if not clause:
        raise HTTPException(status_code=404, detail="Clause not found")

    evidence = QMSClauseEvidence(
        clause_id=clause_id,
        **data.model_dump(),
        created_by=current_user.id,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


@router.put("/evidence/{evidence_id}", response_model=QMSEvidenceResponse)
def update_evidence(
    evidence_id: int,
    data: QMSEvidenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Update evidence, including verification."""
    evidence = db.query(QMSClauseEvidence).filter(QMSClauseEvidence.id == evidence_id).first()
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found")

    update_data = data.model_dump(exclude_unset=True)

    # Track who verified
    if "is_verified" in update_data and update_data["is_verified"]:
        update_data["verified_by"] = current_user.id
        update_data["verified_date"] = datetime.utcnow()

    for key, value in update_data.items():
        setattr(evidence, key, value)

    db.commit()
    db.refresh(evidence)
    return evidence


@router.delete("/evidence/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """Remove an evidence link."""
    evidence = db.query(QMSClauseEvidence).filter(QMSClauseEvidence.id == evidence_id).first()
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found")
    db.delete(evidence)
    db.commit()



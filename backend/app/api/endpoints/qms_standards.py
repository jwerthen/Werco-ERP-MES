from typing import List, Optional
from datetime import datetime
import json
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
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
    AutoEvidenceResult, ClauseAutoEvidenceResponse, AutoLinkSummary,
)
from app.services.auto_evidence_service import (
    discover_evidence_for_clause, compute_overall_compliance,
)

logger = logging.getLogger(__name__)
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


@router.post("/{standard_id}/upload-pdf", response_model=List[QMSClauseResponse])
async def upload_pdf_and_extract_clauses(
    standard_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
):
    """
    Upload a QMS standard PDF (quality manual, AS9100D, ISO 9001, etc.)
    and automatically extract all clauses using AI.
    """
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
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
        from pypdf import PdfReader
        import io
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
        raise HTTPException(status_code=400, detail="Could not extract text from PDF. The file may be scanned/image-based or empty.")

    # Use Claude AI to extract clauses
    try:
        import anthropic
    except ImportError:
        raise HTTPException(status_code=500, detail="AI extraction library not available")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI extraction not configured (ANTHROPIC_API_KEY missing)")

    clause_schema = """[
  {
    "clause_number": "string - e.g. '4.1', '8.5.2'",
    "title": "string - clause title",
    "description": "string - full clause text or summary of requirements"
  }
]"""

    prompt = f"""You are a QMS standards expert. Extract ALL clauses and sub-clauses from this quality management document.

The document is: {standard.name} {standard.version or ''}

Rules:
1. Extract EVERY numbered clause and sub-clause (e.g., 4.1, 4.2, 5.1.1, 8.5.2)
2. Include the clause number, title, and the full requirement text as description
3. Maintain the hierarchical numbering exactly as shown in the document
4. Include ALL levels of sub-clauses
5. For the description, include the actual requirement text — not just a summary
6. Return a JSON array of objects, nothing else

Schema:
{clause_schema}

Document text:
---
{pdf_text[:100000]}
---

Return ONLY a valid JSON array. No markdown, no explanations."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()

        # Clean markdown fences
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        extracted = json.loads(response_text.strip())

        if not isinstance(extracted, list):
            raise ValueError("Expected a JSON array of clauses")

    except json.JSONDecodeError as e:
        logger.error(f"AI returned invalid JSON: {e}")
        raise HTTPException(status_code=500, detail="AI extraction returned invalid data. Try again or use manual entry.")
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
            clause_number=clause_number,
            title=title[:500],
            description=description,
            sort_order=i,
        )
        db.add(clause)
        clauses.append(clause)

    db.commit()
    for c in clauses:
        db.refresh(c)

    logger.info(f"Extracted {len(clauses)} clauses from PDF for standard {standard.name}")
    return clauses


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


# ============== Auto-Evidence Discovery ==============

@router.get("/clauses/{clause_id}/auto-evidence", response_model=ClauseAutoEvidenceResponse)
def get_clause_auto_evidence(
    clause_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Discover live ERP/MES evidence for a single clause."""
    clause = db.query(QMSClause).filter(QMSClause.id == clause_id).first()
    if not clause:
        raise HTTPException(status_code=404, detail="Clause not found")

    results = discover_evidence_for_clause(db, clause)
    overall = compute_overall_compliance(results)

    # Strip internal _rule_id before returning
    clean_results = [{k: v for k, v in r.items() if k != "_rule_id"} for r in results]

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
):
    """
    Run auto-discovery for ALL clauses in a standard and persist evidence links.
    Creates or updates QMSClauseEvidence records with is_auto_linked=True.
    """
    standard = db.query(QMSStandard).filter(QMSStandard.id == standard_id).first()
    if not standard:
        raise HTTPException(status_code=404, detail="QMS standard not found")

    clauses = (
        db.query(QMSClause)
        .filter(QMSClause.standard_id == standard_id)
        .all()
    )

    now = datetime.utcnow()
    total_created = 0
    total_updated = 0
    clauses_with_evidence = 0
    compliance_counts = {}

    for clause in clauses:
        results = discover_evidence_for_clause(db, clause)

        if results:
            clauses_with_evidence += 1

        overall = compute_overall_compliance(results)
        compliance_counts[overall] = compliance_counts.get(overall, 0) + 1

        for result in results:
            rule_id = result.get("_rule_id", result.get("evidence_type", "unknown"))

            # Check for existing auto-linked evidence of this type on this clause
            existing = (
                db.query(QMSClauseEvidence)
                .filter(
                    QMSClauseEvidence.clause_id == clause.id,
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

    db.commit()

    logger.info(
        f"Auto-link for standard {standard.name}: "
        f"{total_created} created, {total_updated} updated, "
        f"{clauses_with_evidence}/{len(clauses)} clauses with evidence"
    )

    return AutoLinkSummary(
        standard_id=standard.id,
        standard_name=standard.name,
        total_clauses=len(clauses),
        clauses_with_evidence=clauses_with_evidence,
        clauses_without_evidence=len(clauses) - clauses_with_evidence,
        total_evidence_created=total_created,
        total_evidence_updated=total_updated,
        compliance_summary=compliance_counts,
    )


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



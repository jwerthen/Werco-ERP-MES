"""Reusable process library, separate from quote and feasibility approvals."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id
from app.api.endpoints.fabrication_quotes import mutate, read_user, write_user
from app.db.database import get_db
from app.models.fabrication_quote_profile import FabricationQuoteProfile
from app.models.user import User
from app.schemas.fabrication_quote_profile import SaveFabricationQuoteProfile
from app.services.audit_service import AuditService
from app.services.fabrication_quote_service import digest, iso

router = APIRouter()


def thickness_text(value):
    return format(value.normalize(), "f") if value is not None else None


def serialize(row):
    return {
        "id": row.id,
        "key": row.key,
        "revision": row.revision,
        "name": row.name,
        "process": row.process,
        "machine": row.machine,
        "material": row.material,
        "thickness_mm": thickness_text(row.thickness_mm),
        "currency": row.currency,
        "template": row.template_json,
        "evidence_note": row.evidence_note,
        "content_sha256": row.content_sha256,
        "created_at": iso(row.created_at),
        "created_by": row.created_by,
    }


@router.get("")
def list_profiles(
    process: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    latest = (
        db.query(
            FabricationQuoteProfile.key.label("key"),
            func.max(FabricationQuoteProfile.revision).label("revision"),
        )
        .filter(FabricationQuoteProfile.company_id == company_id)
        .group_by(FabricationQuoteProfile.key)
        .subquery()
    )
    query = (
        db.query(FabricationQuoteProfile)
        .join(
            latest,
            and_(
                FabricationQuoteProfile.key == latest.c.key,
                FabricationQuoteProfile.revision == latest.c.revision,
            ),
        )
        .filter(FabricationQuoteProfile.company_id == company_id)
    )
    if process is not None:
        query = query.filter(FabricationQuoteProfile.process == process)
    total = query.count()
    rows = (
        query.order_by(FabricationQuoteProfile.name, FabricationQuoteProfile.key)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {"items": [serialize(row) for row in rows], "total": total}


@router.get("/{key}/revisions")
def list_revisions(
    key: UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(FabricationQuoteProfile).filter(
        FabricationQuoteProfile.company_id == company_id,
        FabricationQuoteProfile.key == str(key),
    )
    total = query.count()
    if not total:
        raise HTTPException(404, "Process profile not found")
    rows = query.order_by(FabricationQuoteProfile.revision.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return {"items": [serialize(row) for row in rows], "total": total}


def append_profile(db, company_id, user, value):
    key = str(value.key) if value.key else str(uuid4())
    revision = 1
    if value.key:
        current = (
            db.query(FabricationQuoteProfile)
            .filter(
                FabricationQuoteProfile.company_id == company_id,
                FabricationQuoteProfile.key == key,
            )
            .order_by(FabricationQuoteProfile.revision.desc())
            .first()
        )
        if current is None:
            raise HTTPException(404, "Process profile not found")
        if current.revision != value.expected_revision:
            raise HTTPException(409, "This profile has changed. Reload before saving a new revision.")
        revision = current.revision + 1
    template = value.template.model_dump(mode="json")
    # Quote-specific review and feasibility must never travel with a profile.
    template["evidence"]["reviewed"] = False
    if template["recipe"]["kind"] == "brake":
        template["recipe"]["feasibility_reviewed"] = False
    basis = {
        **value.model_dump(mode="json", exclude={"key", "expected_revision", "template"}),
        "template": template,
        "key": key,
        "revision": revision,
        "thickness_mm": thickness_text(value.thickness_mm),
    }
    row = FabricationQuoteProfile(
        company_id=company_id,
        key=key,
        revision=revision,
        name=value.name,
        process=value.process,
        machine=value.machine,
        material=value.material,
        thickness_mm=value.thickness_mm,
        currency=value.currency,
        template_json=template,
        evidence_note=value.evidence_note,
        content_sha256=digest(basis),
        created_by=user.id,
    )
    db.add(row)
    # Unique(company, key, revision) resolves racing writers; mutate rolls back
    # both the new row and its audit event on any failed transaction.
    db.flush()
    AuditService(db, user).log_required(
        action="CREATE",
        resource_type="fabrication_quote_profile",
        resource_id=row.id,
        resource_identifier=row.name,
        company_id=company_id,
        description="Saved process profile revision; quote review still required",
        new_values={
            "key": key,
            "revision": revision,
            "content_sha256": row.content_sha256,
        },
    )
    return serialize(row)


@router.post("")
def save_profile(
    value: SaveFabricationQuoteProfile,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, append_profile, company_id, user, value)

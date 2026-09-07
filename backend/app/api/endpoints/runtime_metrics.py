from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.runtime_metric import RuntimeMetricSample, RuntimeMetricSetting
from app.models.user import User, UserRole
from app.schemas.runtime_metric import ROUTES, RuntimeMetricBatch, RuntimeMetricSettingWrite
from app.services.runtime_metric_service import (
    RETENTION_DAYS,
    collection_enabled,
    record_runtime_metrics,
    summarize_runtime_metrics,
)

router = APIRouter()


def admin_only(user: User = Depends(require_role([UserRole.ADMIN]))) -> User:
    if getattr(user, "_token_scope", None) == "kiosk":
        raise HTTPException(403, "Performance settings require a desktop admin session")
    return user


@router.get("/config")
def runtime_metric_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return {"enabled": collection_enabled(db, company_id), "retention_days": RETENTION_DAYS}


@router.post("/samples", status_code=202)
def ingest_runtime_metrics(
    body: RuntimeMetricBatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    try:
        result = record_runtime_metrics(db, company_id, body)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


@router.get("/summary")
def runtime_metric_summary(
    days: int = Query(7, ge=1, le=30),
    page: int = Query(1, ge=1, le=1500),
    device: Optional[Literal["mobile", "tablet", "desktop"]] = None,
    release: Optional[str] = Query(None, pattern=r"^(?:[a-f0-9]{40}|development|unknown)$"),
    route: Optional[str] = Query(None, max_length=100),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    if route is not None and route not in ROUTES:
        raise HTTPException(422, "Unknown route template")
    rows = summarize_runtime_metrics(db, company_id, days, device, release, route, offset=(page - 1) * 200)
    return {
        "enabled": collection_enabled(db, company_id),
        "retention_days": RETENTION_DAYS,
        "days": days,
        "rows": rows[:200],
        "page": page,
        "has_more": len(rows) > 200,
    }


@router.put("/config")
def update_runtime_metric_config(
    body: RuntimeMetricSettingWrite,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    acquire_generator_lock(db, "runtime_metrics", company_id)
    row = db.query(RuntimeMetricSetting).filter_by(company_id=company_id).first()
    if row is None:
        row = RuntimeMetricSetting(company_id=company_id)
        db.add(row)
    row.enabled = body.enabled
    db.commit()
    return {"enabled": body.enabled, "retention_days": RETENTION_DAYS}


@router.delete("/samples", status_code=204)
def erase_runtime_metric_samples(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    acquire_generator_lock(db, "runtime_metrics", company_id)
    db.query(RuntimeMetricSample).filter_by(company_id=company_id).delete(synchronize_session=False)
    db.commit()

"""Typed employee submissions; a chat suggestion never executes a business write."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_tasks import (
    HankCapabilities,
    HankTaskCommand,
    HankTaskCreate,
    HankTaskList,
    HankTaskResponse,
    HankTaskStatus,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_task_service import HankTaskService

router = APIRouter()


def _command(db, service, operation):
    try:
        task = operation()
        # Validate the complete receipt before the only commit.
        response = service.response(task)
        db.commit()
        return response
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, 'Unable to save audit record. No Hank action was committed.') from exc
    except (IntegrityError, StaleDataError) as exc:
        db.rollback()
        raise HTTPException(
            409, 'This task conflicts with a saved record. Refresh its state before continuing.'
        ) from exc
    except Exception:
        db.rollback()
        raise


@router.get('/capabilities', response_model=HankCapabilities)
def capabilities(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return HankTaskService(db, user, company_id).capabilities()


@router.get('/tasks', response_model=HankTaskList)
def list_tasks(
    limit: int = Query(25, ge=1, le=100),
    before_id: int | None = Query(None, gt=0),
    status: HankTaskStatus | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return HankTaskService(db, user, company_id).list(limit=limit, before_id=before_id, status=status)


@router.get('/tasks/{task_id}', response_model=HankTaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    service = HankTaskService(db, user, company_id)
    return service.response(service.get(task_id))


@router.post('/tasks', response_model=HankTaskResponse)
def prepare_task(
    payload: HankTaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    service = HankTaskService(db, user, company_id)
    return _command(db, service, lambda: service.prepare(payload, audit))


@router.post('/tasks/{task_id}/execute', response_model=HankTaskResponse)
def execute_task(
    task_id: int,
    payload: HankTaskCommand,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    service = HankTaskService(db, user, company_id)
    return _command(db, service, lambda: service.execute(task_id, payload, audit))


@router.post('/tasks/{task_id}/cancel', response_model=HankTaskResponse)
def cancel_task(
    task_id: int,
    payload: HankTaskCommand,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    service = HankTaskService(db, user, company_id)
    return _command(db, service, lambda: service.cancel(task_id, payload, audit))

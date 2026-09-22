"""Explicit interactive opt-in and controls for private Hank follow-ups."""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.api.endpoints.hank_tasks import _command
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_tasks import HankTaskCommand, HankTaskResponse
from app.schemas.hank_watches import HankWatchCreate
from app.services.audit_service import AuditService
from app.services.hank_watch_service import HankWatchService

router = APIRouter()


@router.post('/watches', response_model=HankTaskResponse)
def create_watch(
    payload: HankWatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Start one private follow-up from an interactive account with current work-order view access.

    Record the baseline now; notify once when all current blockers clear or a new
    matching PDF is attached. Attachment metadata never establishes approval.
    """
    service = HankWatchService(db, user, company_id)
    return _command(db, service.tasks, lambda: service.prepare(payload, audit))


@router.post('/watches/{task_id}/{action}', response_model=HankTaskResponse)
def update_watch(
    task_id: int,
    action: Literal['check', 'snooze', 'resume', 'cancel'],
    payload: HankTaskCommand,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Check now, snooze for one hour, resume or stop your versioned watch.

    Recheck current owner, company, interactive credential and work-order view
    authority. Completed checks replay the stored receipt without a second alert.
    """
    service = HankWatchService(db, user, company_id)
    return _command(db, service.tasks, lambda: service.command(task_id, action, payload, audit))

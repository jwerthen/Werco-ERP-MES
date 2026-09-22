"""Explicit handoffs and approved procedure runs under the employee's own authority."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_tasks import HankTaskCommand
from app.schemas.hank_teamwork import (
    HandoffCreate,
    HandoffList,
    HandoffPeople,
    HandoffResponse,
    HandoffStatus,
    QueueState,
    RoutineAdvance,
    RoutineCreate,
    RoutineList,
    RoutineResponse,
    RoutineRunList,
    RoutineRunResponse,
    RoutineStart,
    RoutineUpdate,
    WorkQueue,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_teamwork_service import HankTeamworkService

router = APIRouter()


def service(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return HankTeamworkService(db, user, company_id)


def commit(svc, operation):
    try:
        result = operation()
        svc.db.commit()
        return result
    except AuditWriteError as exc:
        svc.db.rollback()
        raise HTTPException(503, 'Required audit could not be saved. No Hank teamwork change was committed.') from exc
    except IntegrityError as exc:
        svc.db.rollback()
        raise HTTPException(409, 'This record changed. Reload it before continuing.') from exc
    except Exception:
        svc.db.rollback()
        raise


@router.get('/handoff-people', response_model=HandoffPeople)
def people(q: str = Query('', max_length=100), svc=Depends(service)):
    return svc.people(q)


@router.get('/handoffs', response_model=HandoffList)
def handoffs(
    direction: Literal['all', 'sent', 'received'] = 'all',
    status: HandoffStatus | None = None,
    limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, gt=0),
    svc=Depends(service),
):
    return svc.list_handoffs(direction, status, limit, before_id)


@router.post('/handoffs', response_model=HandoffResponse)
def create_handoff(payload: HandoffCreate, svc=Depends(service), audit: AuditService = Depends(get_audit_service)):
    return commit(svc, lambda: svc.create_handoff(payload, audit))


@router.get('/handoffs/{handoff_id}', response_model=HandoffResponse)
def handoff(handoff_id: int, svc=Depends(service)):
    return svc.handoff_response(svc.get_handoff(handoff_id))


@router.post('/handoffs/{handoff_id}/acknowledge', response_model=HandoffResponse)
def acknowledge(
    handoff_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_handoff(handoff_id, payload, 'acknowledge', audit))


@router.post('/handoffs/{handoff_id}/complete', response_model=HandoffResponse)
def complete_handoff(
    handoff_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_handoff(handoff_id, payload, 'complete', audit))


@router.post('/handoffs/{handoff_id}/cancel', response_model=HandoffResponse)
def cancel_handoff(
    handoff_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_handoff(handoff_id, payload, 'cancel', audit))


@router.post('/handoffs/{handoff_id}/attachments', response_model=HandoffResponse)
def add_photo(
    handoff_id: int,
    expected_company_id: int = Form(..., gt=0),
    expected_version: int = Form(..., ge=1),
    request_key: UUID = Form(...),
    file: UploadFile = File(...),
    svc=Depends(service),
    audit: AuditService = Depends(get_audit_service),
):
    payload = HankTaskCommand(expected_company_id=expected_company_id, expected_version=expected_version)
    try:
        return svc.add_attachment(
            handoff_id, payload, str(request_key), file.filename, file.file.read(10 * 1024 * 1024 + 1), audit
        )
    except AuditWriteError as exc:
        raise HTTPException(503, 'Required photo audit could not be saved. Reload the handoff.') from exc


@router.get('/handoffs/{handoff_id}/attachments/{attachment_id}')
def photo(handoff_id: int, attachment_id: UUID, svc=Depends(service)):
    content, mime = svc.attachment(handoff_id, str(attachment_id))
    filename = 'handoff-photo.png' if mime == 'image/png' else 'handoff-photo.jpg'
    return Response(
        content=content,
        media_type=mime,
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'private, no-store',
        },
    )


@router.get('/routines', response_model=RoutineList)
def routines(svc=Depends(service)):
    return svc.list_routines()


@router.post('/routines', response_model=RoutineResponse)
def create_routine(payload: RoutineCreate, svc=Depends(service), audit: AuditService = Depends(get_audit_service)):
    return commit(svc, lambda: svc.create_routine(payload, audit))


@router.get('/routines/{routine_id}', response_model=RoutineResponse)
def routine(routine_id: int, svc=Depends(service)):
    return svc.routine_response(svc.get_routine(routine_id))


@router.put('/routines/{routine_id}', response_model=RoutineResponse)
def update_routine(
    routine_id: int, payload: RoutineUpdate, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.update_routine(routine_id, payload, audit))


@router.post('/routines/{routine_id}/approve', response_model=RoutineResponse)
def approve_routine(
    routine_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_routine(routine_id, payload, 'approve', audit))


@router.post('/routines/{routine_id}/archive', response_model=RoutineResponse)
def archive_routine(
    routine_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_routine(routine_id, payload, 'archive', audit))


@router.post('/routines/{routine_id}/start', response_model=RoutineRunResponse)
def start_routine(
    routine_id: int, payload: RoutineStart, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.start_routine(routine_id, payload, audit))


@router.get('/routine-runs', response_model=RoutineRunList)
def runs(limit: int = Query(20, ge=1, le=100), before_id: int | None = Query(None, gt=0), svc=Depends(service)):
    return svc.list_runs(limit, before_id)


@router.get('/routine-runs/{run_id}', response_model=RoutineRunResponse)
def run(run_id: int, svc=Depends(service)):
    return svc.run_response(svc.get_run(run_id))


@router.post('/routine-runs/{run_id}/advance', response_model=RoutineRunResponse)
def advance_run(
    run_id: int, payload: RoutineAdvance, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_run(run_id, payload, 'advance', audit))


@router.post('/routine-runs/{run_id}/cancel', response_model=RoutineRunResponse)
def cancel_run(
    run_id: int, payload: HankTaskCommand, svc=Depends(service), audit: AuditService = Depends(get_audit_service)
):
    return commit(svc, lambda: svc.transition_run(run_id, payload, 'cancel', audit))


@router.get('/work-queue', response_model=WorkQueue)
def queue(state: QueueState | None = None, svc=Depends(service)):
    from app.services.hank_work_queue import work_queue

    return work_queue(svc.db, svc.user, svc.company_id, state)

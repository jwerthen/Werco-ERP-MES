"""Explicit advisory observations, isolated from operational inventory writers."""

import json
from typing import Union

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import atomic_transaction, get_db
from app.models.user import User, UserRole
from app.schemas.remnant_planning import PlanningSnapshotRequest, PlanningSnapshotResponse
from app.schemas.stock_piece import (
    CreatePiece,
    ObservationDetail,
    ObservationPage,
    RecordObservation,
    SourcePage,
    WithdrawObservation,
)
from app.services import remnant_planning
from app.services import stock_piece as service
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()


def read_user(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(UserRole))),
    company_id: int = Depends(get_current_company_id),
) -> User:
    service.require_access(db, user, company_id)
    return user


def write_user(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(service.WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
) -> User:
    service.require_access(db, user, company_id, write=True)
    return user


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


async def check_body(request: Request) -> None:
    # The existing global 256 KiB receive cap precedes parsing/auth. The service
    # separately caps canonical measurement evidence to 128 KiB before writes.
    try:
        json.loads(await request.body(), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, 'Provide one unambiguous JSON object without duplicate fields') from exc


def save(db, user, company_id, audit, command, piece_id=None):
    try:
        with atomic_transaction(db):
            return service.save_observation(db, user, company_id, audit, command, piece_id=piece_id)
    except IntegrityError as exc:
        raise HTTPException(
            409, 'The label, source or observation changed concurrently; retry the same request to recover its result'
        ) from exc
    except AuditWriteError as exc:
        raise HTTPException(
            503, 'The observation was not saved because required audit failed; retry the same request'
        ) from exc


@router.get('/stock-piece-sources', response_model=SourcePage)
def list_stock_piece_sources(
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=100),
    inventory_item_id: int | None = Query(None, ge=1, le=2147483647),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read exact tenant inventory/Part evidence and movement watermarks without writes or availability claims."""
    return service.list_sources(
        db, company_id, page=page, per_page=per_page, q=q, inventory_item_id=inventory_item_id, user=user
    )


@router.get('/stock-pieces', response_model=ObservationPage)
def list_stock_pieces(
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read latest advisory observations, including withdrawn records and observational source drift; no reconcile."""
    return service.list_observations(db, company_id, page=page, per_page=per_page, user=user)


@router.get('/stock-pieces/{piece_id}/observations', response_model=ObservationPage)
def list_stock_piece_observations(
    piece_id: int = Path(..., ge=1, le=2147483647),
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read immutable observation history; historical versions never acquire current availability authority."""
    return service.list_observations(db, company_id, page=page, per_page=per_page, piece_id=piece_id, user=user)


@router.get('/stock-pieces/{piece_id}/observations/{number}', response_model=ObservationDetail)
def get_stock_piece_observation(
    piece_id: int = Path(..., ge=1, le=2147483647),
    number: int = Path(..., ge=1, le=2147483647),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Open exact measured-evidence history with its original version/hash and a pure current-source comparison."""
    return service.get_observation(db, company_id, piece_id, number)


@router.post('/stock-pieces', response_model=ObservationDetail)
async def create_stock_piece(
    request: Request,
    body: CreatePiece,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record a labeled physical-piece observation and required audit atomically; never receive inventory."""
    await check_body(request)
    return await run_in_threadpool(save, db, user, company_id, audit, body)


@router.post(
    '/stock-pieces/{piece_id}/observations/{number}/planning-snapshot', response_model=PlanningSnapshotResponse
)
async def resolve_stock_piece_planning_snapshot(
    request: Request,
    body: PlanningSnapshotRequest,
    piece_id: int = Path(..., ge=1, le=2147483647),
    number: int = Path(..., ge=1, le=2147483647),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read exact current recorded-piece evidence for unapproved planning; no geometry, audit or inventory writes."""
    await check_body(request)
    return await run_in_threadpool(remnant_planning.resolve_snapshot, db, user, company_id, piece_id, number, body)


@router.post('/stock-pieces/{piece_id}/observations', response_model=ObservationDetail)
async def append_stock_piece_observation(
    request: Request,
    piece_id: int = Path(..., ge=1, le=2147483647),
    body: Union[RecordObservation, WithdrawObservation] = Body(discriminator='state'),
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Append a correction or withdrawal using version CAS and same-actor/credential UUID recovery; no stock write-off."""
    await check_body(request)
    return await run_in_threadpool(save, db, user, company_id, audit, body, piece_id)

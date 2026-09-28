"""Company-wide visibility and manager-owned continuous improvement workflow."""

from datetime import date, datetime
from typing import get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.continuous_improvement import ImprovementActivity, ImprovementSuggestion, utcnow
from app.models.user import User, UserRole
from app.schemas.continuous_improvement import (
    ActivityResponse,
    ImprovementCategory,
    ImprovementMetadata,
    ImprovementPriority,
    ImprovementStatus,
    SuggestionComment,
    SuggestionCreate,
    SuggestionDetail,
    SuggestionList,
    SuggestionResponse,
    SuggestionUpdate,
)
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()
MANAGEMENT_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.PLATFORM_ADMIN)
manager = require_role([UserRole.ADMIN, UserRole.MANAGER])
CATEGORIES = (
    ('poka_yoke', 'Poka-yoke / mistake proofing', 'Prevent mistakes or detect them at their source.'),
    ('five_s', '5S / workplace organization', 'Sort, set in order, shine, standardize, and sustain.'),
    ('standard_work', 'Standard work', 'Make the best known way clear and repeatable.'),
    ('flow_layout', 'Flow / shop layout', 'Reduce travel, waiting, bottlenecks, and handoffs.'),
    ('quality', 'Quality / defect reduction', 'Improve first-pass quality and reduce rework.'),
    ('safety_ergonomics', 'Safety / ergonomics', 'Reduce hazards and improve how work is performed.'),
    ('setup_reduction', 'Setup reduction / SMED', 'Shorten and simplify changeovers.'),
    ('equipment', 'Equipment / reliability', 'Improve uptime, maintenance, and equipment effectiveness.'),
    ('inventory', 'Inventory / material handling', 'Improve material availability, storage, and movement.'),
    ('other', 'Other improvement', 'Capture opportunities that do not fit another category.'),
)
STATUS_LABELS = {
    'new': 'New',
    'under_review': 'Under review',
    'approved': 'Approved',
    'in_progress': 'In progress',
    'implemented': 'Implemented',
    'on_hold': 'On hold',
    'declined': 'Declined',
}


def _suggestion(db, company_id, suggestion_id):
    row = (
        db.query(ImprovementSuggestion)
        .filter(
            ImprovementSuggestion.company_id == company_id,
            ImprovementSuggestion.id == suggestion_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(404, 'Suggestion not found')
    return row


def _owner(db, company_id, owner_id):
    if owner_id is None:
        return None
    owner = (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.id == owner_id,
            User.is_active.is_(True),
            or_(User.role.in_(MANAGEMENT_ROLES), User.is_superuser.is_(True)),
        )
        .first()
    )
    if owner is None:
        raise HTTPException(422, 'Owner must be an active manager or administrator in this company')
    return owner.full_name


def _detail(db, row):
    history = (
        db.query(ImprovementActivity)
        .filter(
            ImprovementActivity.company_id == row.company_id,
            ImprovementActivity.suggestion_id == row.id,
        )
        .order_by(ImprovementActivity.created_at, ImprovementActivity.id)
        .all()
    )
    return SuggestionDetail(
        **SuggestionResponse.model_validate(row).model_dump(),
        history=[ActivityResponse.model_validate(item) for item in history],
    )


def _json_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _record(db, row, user, audit, *, kind, body=None, changes=None):
    db.flush()  # Version CAS and constraints succeed before adding activity/audit.
    activity = ImprovementActivity(
        company_id=row.company_id,
        suggestion_id=row.id,
        kind=kind,
        actor_id=user.id,
        actor_name=user.full_name,
        created_at=row.updated_at,
        body=body,
        changes=changes or {},
    )
    db.add(activity)
    db.flush()
    audit.log_required(
        'CREATE' if kind == 'submitted' else 'STATUS_CHANGE' if kind == 'status_changed' else 'UPDATE',
        'improvement_suggestion',
        resource_id=row.id,
        resource_identifier=f'CI-{row.id:04d}',
        description=f'Continuous improvement: {kind}',
        company_id=row.company_id,
        new_values={'version': row.version, 'activity_id': activity.id, 'body': body, 'changes': changes or {}},
    )


def _write(db, operation):
    try:
        row = operation()
        response = _detail(db, row)
        db.commit()
        return response
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, 'Unable to save audit record. No changes were committed.') from exc
    except (IntegrityError, StaleDataError) as exc:
        db.rollback()
        raise HTTPException(409, 'This suggestion changed. Refresh it before saving again.') from exc
    except Exception:
        db.rollback()
        raise


def _expect_version(row, expected_version):
    if row.version != expected_version:
        raise HTTPException(409, 'This suggestion changed. Refresh it before saving again.')


@router.get('/metadata', response_model=ImprovementMetadata)
def metadata(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    owners = (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.is_active.is_(True),
            or_(User.role.in_(MANAGEMENT_ROLES), User.is_superuser.is_(True)),
        )
        .order_by(User.first_name, User.last_name, User.id)
        .all()
    )
    return {
        'categories': [
            dict(value=value, label=label, description=description) for value, label, description in CATEGORIES
        ],
        'statuses': [dict(value=value, label=label) for value, label in STATUS_LABELS.items()],
        'priorities': [dict(value=value, label=value.title()) for value in get_args(ImprovementPriority)],
        'owners': [dict(id=owner.id, name=owner.full_name) for owner in owners],
        'can_manage': bool(
            (user.role in MANAGEMENT_ROLES or user.is_superuser)
            and not getattr(user, '_read_only_company_context', False)
        ),
    }


@router.get('/', response_model=SuggestionList)
def list_suggestions(
    q: str | None = Query(None, max_length=200),
    status: ImprovementStatus | None = None,
    category: ImprovementCategory | None = None,
    priority: ImprovementPriority | None = None,
    owner_id: int | None = Query(None, gt=0),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(ImprovementSuggestion).filter(ImprovementSuggestion.company_id == company_id)
    counts = dict(
        query.with_entities(ImprovementSuggestion.status, func.count(ImprovementSuggestion.id))
        .group_by(ImprovementSuggestion.status)
        .all()
    )
    if q and q.strip():
        term = q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = f'%{term}%'
        predicates = [
            field.ilike(pattern, escape='\\')
            for field in (
                ImprovementSuggestion.title,
                ImprovementSuggestion.problem,
                ImprovementSuggestion.proposed_solution,
                ImprovementSuggestion.expected_benefit,
                ImprovementSuggestion.area,
            )
        ]
        reference = q.strip().upper().removeprefix('CI-')
        if reference.isascii() and reference.isdigit() and len(reference) <= 10:
            predicates.append(ImprovementSuggestion.id == int(reference))
        query = query.filter(or_(*predicates))
    for field, value in (
        (ImprovementSuggestion.status, status),
        (ImprovementSuggestion.category, category),
        (ImprovementSuggestion.priority, priority),
        (ImprovementSuggestion.owner_id, owner_id),
    ):
        if value is not None:
            query = query.filter(field == value)
    total = query.count()
    items = (
        query.order_by(ImprovementSuggestion.created_at.desc(), ImprovementSuggestion.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return {'items': items, 'total': total, 'status_counts': {value: counts.get(value, 0) for value in STATUS_LABELS}}


@router.post('/', response_model=SuggestionDetail, status_code=201)
def create_suggestion(
    payload: SuggestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    def create():
        now = utcnow()
        row = ImprovementSuggestion(
            **payload.model_dump(),
            company_id=company_id,
            owner_name=_owner(db, company_id, payload.owner_id),
            created_by=user.id,
            created_by_name=user.full_name,
            updated_by=user.id,
            updated_by_name=user.full_name,
            created_at=now,
            updated_at=now,
            status='new',
        )
        db.add(row)
        _record(
            db,
            row,
            user,
            audit,
            kind='submitted',
            changes={key: {'from': None, 'to': _json_value(value)} for key, value in payload.model_dump().items()}
            | {'status': {'from': None, 'to': 'new'}, 'owner_name': {'from': None, 'to': row.owner_name}},
        )
        return row

    return _write(db, create)


@router.get('/{suggestion_id}', response_model=SuggestionDetail)
def get_suggestion(
    suggestion_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return _detail(db, _suggestion(db, company_id, suggestion_id))


@router.patch('/{suggestion_id}', response_model=SuggestionDetail)
def update_suggestion(
    suggestion_id: int,
    payload: SuggestionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    def update():
        row = _suggestion(db, company_id, suggestion_id)
        _expect_version(row, payload.expected_version)
        values = payload.model_dump(exclude_unset=True, exclude={'expected_version', 'change_note'})
        new_status = values.get('status', row.status)
        status_changed = new_status != row.status
        if status_changed:
            if new_status == 'new':
                raise HTTPException(422, 'A reviewed suggestion cannot return to New; reopen it for review instead')
            if (new_status in ('on_hold', 'declined') or row.status == 'implemented') and not payload.change_note:
                raise HTTPException(
                    422, 'Add a change note when holding, declining, or reopening an implemented suggestion'
                )
        notes = values.get('implementation_notes', row.implementation_notes)
        if new_status == 'implemented' and not (notes and notes.strip()):
            raise HTTPException(422, 'Implementation notes are required before marking a suggestion implemented')
        if 'owner_id' in values and values['owner_id'] != row.owner_id:
            values['owner_name'] = _owner(db, company_id, values['owner_id'])
        changes = {
            key: {'from': _json_value(getattr(row, key)), 'to': _json_value(value)}
            for key, value in values.items()
            if getattr(row, key) != value
        }
        if not changes and not payload.change_note:
            return row
        now = utcnow()
        for key, value in values.items():
            setattr(row, key, value)
        if status_changed:
            if row.reviewed_at is None:
                row.reviewed_at = now
            row.implemented_at = now if new_status == 'implemented' else None
        row.updated_at = now
        row.updated_by, row.updated_by_name = user.id, user.full_name
        _record(
            db,
            row,
            user,
            audit,
            kind='status_changed' if status_changed else 'updated' if changes else 'comment',
            body=payload.change_note,
            changes=changes,
        )
        return row

    return _write(db, update)


@router.post('/{suggestion_id}/comments', response_model=SuggestionDetail)
def add_comment(
    suggestion_id: int,
    payload: SuggestionComment,
    db: Session = Depends(get_db),
    user: User = Depends(manager),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    def comment():
        row = _suggestion(db, company_id, suggestion_id)
        _expect_version(row, payload.expected_version)
        row.updated_at = utcnow()
        row.updated_by, row.updated_by_name = user.id, user.full_name
        _record(db, row, user, audit, kind='comment', body=payload.body)
        return row

    return _write(db, comment)

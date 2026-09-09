"""Advisory measurements, immutable history and pure observational source reads."""

import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, defer

from app.core.time_utils import to_utc_iso
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.stock_piece import StockPiece, StockPieceObservation
from app.models.user import User, UserRole
from app.schemas.stock_piece import MAX_PAYLOAD_BYTES, CreatePiece, RecordObservation, WithdrawObservation
from app.services.audit_service import AuditService

WRITE_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR)
REVIEW_ISSUES = [
    'Reported measurements only; geometry topology has not been checked by the server calculation kernel.',
    'Physical presence, material specification, ownership, certification and eligibility are unverified.',
    'No inventory quantity, reservation, physical lineage or monetary value is created by this observation.',
]
WATERMARK_COVERAGE = 'direct_item_and_unattributed_same_part'


def require_access(db: Session, user: User, company_id: int, *, write: bool = False) -> None:
    if write and getattr(user, '_read_only_company_context', False):
        raise HTTPException(403, 'This company context is read-only')
    if user.is_superuser or user.role == UserRole.PLATFORM_ADMIN:
        return
    if write and user.role not in WRITE_ROLES:
        raise HTTPException(403, 'Recording stock observations requires an inventory mutator role')
    row = tenant_query(db, RolePermission, company_id).filter(RolePermission.role == user.role).first()
    permissions = row.permissions if row is not None else DEFAULT_ROLE_PERMISSIONS.get(user.role, [])
    if (
        not isinstance(permissions, list)
        or not all(isinstance(p, str) for p in permissions)
        or 'inventory:view' not in permissions
    ):
        raise HTTPException(403, 'Stock observations require inventory:view')


def can_record(db: Session, user: User, company_id: int) -> bool:
    try:
        require_access(db, user, company_id, write=True)
    except HTTPException:
        return False
    return True


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _snapshot_value(value):
    if isinstance(value, datetime):
        return to_utc_iso(value)
    if isinstance(value, Enum):
        return value.value
    if type(value) is float:
        return repr(value) if math.isfinite(value) else None
    return value


def _source_query(db: Session, company_id: int):
    # One statement binds the Item/Part metadata and the movement watermark to
    # the same READ COMMITTED snapshot. No lock or stock-write participation.
    movement = and_(
        InventoryTransaction.company_id == company_id,
        or_(
            InventoryTransaction.inventory_item_id == InventoryItem.id,
            and_(
                InventoryTransaction.inventory_item_id.is_(None), InventoryTransaction.part_id == InventoryItem.part_id
            ),
        ),
    )
    watermarks = [
        db.query(aggregate).filter(movement).correlate(InventoryItem).scalar_subquery()
        for aggregate in (
            func.count(InventoryTransaction.id),
            func.max(InventoryTransaction.id),
            func.max(InventoryTransaction.created_at),
        )
    ]
    return (
        tenant_query(db, InventoryItem, company_id)
        .autoflush(False)
        .join(Part, and_(Part.id == InventoryItem.part_id, Part.company_id == company_id))
        .filter(Part.is_deleted.is_(False))
        .add_entity(Part)
        .add_columns(*watermarks)
        .populate_existing()
    )


def _source_response(row) -> dict:
    item, part, count, last_id, last_at = row
    item_fields = (
        'id',
        'company_id',
        'part_id',
        'location',
        'warehouse',
        'quantity_on_hand',
        'quantity_allocated',
        'quantity_available',
        'lot_number',
        'serial_number',
        'received_date',
        'supplier_id',
        'po_number',
        'cert_number',
        'heat_lot',
        'expiration_date',
        'unit_cost',
        'status',
        'is_active',
        'updated_at',
    )
    part_fields = (
        'id',
        'company_id',
        'part_number',
        'revision',
        'name',
        'part_type',
        'unit_of_measure',
        'is_active',
        'is_deleted',
        'standard_cost',
        'material_cost',
        'updated_at',
    )
    snapshot = {
        'version': 1,
        'item': {field: _snapshot_value(getattr(item, field)) for field in item_fields},
        'part': {field: _snapshot_value(getattr(part, field)) for field in part_fields},
        'movement_watermark': {
            'coverage': WATERMARK_COVERAGE,
            'count': count,
            'max_id': last_id,
            'max_created_at': _snapshot_value(last_at),
        },
    }
    issues = [
        'Source quantity is an aggregate, not a physical-piece count; no UOM conversion or value allocation was inferred.',
        'Watermark includes direct-item and unattributed same-Part movements; drift may concern another lot.',
        'Inventory cert/heat text and cost are historical source metadata, not verified certification or piece valuation.',
    ]
    if not item.is_active or not part.is_active:
        issues.append('The inventory source or Part is inactive; recording it does not change that status.')
    if item.status != 'available':
        issues.append('The inventory source is held or has a nonstandard status; no release or eligibility is implied.')
    for field in ('lot_number', 'heat_lot', 'cert_number', 'location'):
        if not getattr(item, field):
            issues.append('Source metadata is missing: ' + field)
    if not part.unit_of_measure:
        issues.append('Source metadata is missing: unit_of_measure')
    for entity, fields in (
        (item, ('quantity_on_hand', 'quantity_allocated', 'quantity_available', 'unit_cost')),
        (part, ('standard_cost', 'material_cost')),
    ):
        if any(type(getattr(entity, field)) is float and not math.isfinite(getattr(entity, field)) for field in fields):
            issues.append('Nonfinite legacy numeric source metadata is represented as unknown, never as zero.')
    return {
        'inventory_item_id': item.id,
        'part_id': part.id,
        'source_sha256': digest(snapshot),
        'snapshot': snapshot,
        'review_issues': issues,
    }


def source_by_id(db: Session, company_id: int, item_id: int) -> dict | None:
    row = _source_query(db, company_id).filter(InventoryItem.id == item_id).first()
    return _source_response(row) if row is not None else None


def list_sources(
    db: Session,
    company_id: int,
    *,
    page: int,
    per_page: int,
    user: User,
    q: str | None = None,
    inventory_item_id: int | None = None,
) -> dict:
    query = _source_query(db, company_id)
    if inventory_item_id is not None:
        query = query.filter(InventoryItem.id == inventory_item_id)
    if q:
        needle = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.filter(
            or_(
                Part.part_number.ilike(needle, escape='\\'),
                Part.name.ilike(needle, escape='\\'),
                InventoryItem.lot_number.ilike(needle, escape='\\'),
            )
        )
    total = query.count()
    rows = query.order_by(InventoryItem.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return {
        'company_id': company_id,
        'can_record': can_record(db, user, company_id),
        'items': [_source_response(row) for row in rows],
        'total': total,
        'page': page,
        'per_page': per_page,
    }


def _piece(db: Session, company_id: int, piece_id: int, *, locked: bool = False):
    query = tenant_query(db, StockPiece, company_id).filter(StockPiece.id == piece_id)
    piece = query.with_for_update().populate_existing().first() if locked else query.first()
    if piece is None:
        raise HTTPException(404, 'Stock piece observation identity not found')
    return piece


def _observation(db: Session, company_id: int, piece_id: int, number: int):
    result = (
        tenant_query(db, StockPieceObservation, company_id)
        .filter(StockPieceObservation.stock_piece_id == piece_id, StockPieceObservation.observation_number == number)
        .first()
    )
    if result is None:
        raise HTTPException(404, 'Stock piece observation not found')
    return result


def observation_response(db: Session, company_id: int, row: StockPieceObservation, *, detail: bool = False) -> dict:
    piece = _piece(db, company_id, row.stock_piece_id)
    source = source_by_id(db, company_id, row.source_inventory_item_id)
    return _render_observation(company_id, row, piece, source, detail=detail)


def _render_observation(
    company_id: int,
    row: StockPieceObservation,
    piece: StockPiece,
    source: dict | None,
    *,
    detail: bool = False,
) -> dict:
    current_hash = source['source_sha256'] if source else None
    status = 'missing' if source is None else 'unchanged' if current_hash == row.source_sha256 else 'changed'
    issues = list(REVIEW_ISSUES)
    if status != 'unchanged':
        issues.append(
            'The source is '
            + status
            + ' since this immutable observation; this is a staleness warning, not an availability check.'
        )
    if row.state == 'WITHDRAWN':
        issues.append('This observation was withdrawn. It is retained as history and does not write off inventory.')
    result = {
        'piece_id': piece.id,
        'company_id': company_id,
        'label': piece.label,
        'observation_number': row.observation_number,
        'piece_version': row.observation_number,
        'state': row.state,
        'reason': row.reason,
        'observed_at': to_utc_iso(row.observed_at),
        'observer_name': row.observer_name,
        'created_at': to_utc_iso(row.created_at),
        'created_by': row.created_by,
        'submitted_api_token_id': row.submitted_api_token_id,
        'payload_schema_version': row.payload_schema_version,
        'payload_sha256': row.payload_sha256,
        'payload_bytes': row.payload_bytes,
        'source_inventory_item_id': row.source_inventory_item_id,
        'source_part_id': row.source_part_id,
        'source_sha256': row.source_sha256,
        'source_status': status,
        'current_source_sha256': current_hash,
        'review_issues': issues,
    }
    if detail:
        result.update(evidence=row.payload_json, source_snapshot=row.source_snapshot_json, request_key=row.request_key)
    return result


def get_observation(db: Session, company_id: int, piece_id: int, number: int) -> dict:
    return observation_response(db, company_id, _observation(db, company_id, piece_id, number), detail=True)


def list_observations(
    db: Session, company_id: int, *, page: int, per_page: int, user: User, piece_id: int | None = None
) -> dict:
    query = tenant_query(db, StockPieceObservation, company_id).options(
        defer(StockPieceObservation.payload_json), defer(StockPieceObservation.source_snapshot_json)
    )
    if piece_id is None:
        query = query.join(
            StockPiece, and_(StockPiece.id == StockPieceObservation.stock_piece_id, StockPiece.company_id == company_id)
        ).filter(StockPiece.latest_observation_number == StockPieceObservation.observation_number)
    else:
        _piece(db, company_id, piece_id)
        query = query.filter(StockPieceObservation.stock_piece_id == piece_id)
    total = query.count()
    rows = (
        query.order_by(StockPieceObservation.created_at.desc(), StockPieceObservation.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    # Request-local batches keep list/history cost bounded by the page, rather
    # than issuing header/source queries for every observation. The unchanged
    # source query reads each Item, Part and watermark in one SQL snapshot.
    pieces = {}
    sources = {}
    if rows:
        pieces = {
            piece.id: piece
            for piece in tenant_query(db, StockPiece, company_id)
            .filter(StockPiece.id.in_({row.stock_piece_id for row in rows}))
            .all()
        }
        sources = {
            source_row[0].id: _source_response(source_row)
            for source_row in _source_query(db, company_id)
            .filter(InventoryItem.id.in_({row.source_inventory_item_id for row in rows}))
            .all()
        }
        if any(row.stock_piece_id not in pieces for row in rows):
            raise HTTPException(404, 'Stock piece observation identity not found')
    return {
        'company_id': company_id,
        'can_record': can_record(db, user, company_id),
        'items': [
            _render_observation(company_id, row, pieces[row.stock_piece_id], sources.get(row.source_inventory_item_id))
            for row in rows
        ],
        'total': total,
        'page': page,
        'per_page': per_page,
    }


def save_observation(
    db: Session,
    user: User,
    company_id: int,
    audit: AuditService,
    command: CreatePiece | RecordObservation | WithdrawObservation,
    *,
    piece_id: int | None = None,
) -> dict:
    """Flush observation/header/audit once; caller owns the sole atomic commit."""
    require_access(db, user, company_id, write=True)
    if command.expected_company_id != company_id:
        raise HTTPException(409, 'Active company changed; reopen this company before recording an observation')
    if (piece_id is None) != isinstance(command, CreatePiece):
        raise HTTPException(422, 'The command does not match the stock observation endpoint')
    token_id = getattr(user, '_api_token_id', None)
    request_hash = digest(
        {
            'version': 1,
            'company_id': company_id,
            'actor_id': user.id,
            'api_token_id': token_id,
            'piece_id': piece_id,
            'command': command.model_dump(mode='json'),
        }
    )
    acquire_generator_lock(db, 'stock_piece_observation:' + command.request_key, company_id)
    prior = (
        tenant_query(db, StockPieceObservation, company_id)
        .filter(StockPieceObservation.request_key == command.request_key)
        .first()
    )
    if prior is not None:
        if (
            prior.created_by != user.id
            or prior.submitted_api_token_id != token_id
            or prior.request_hash != request_hash
        ):
            raise HTTPException(409, 'This request key already records a different command or credential')
        return observation_response(db, company_id, prior, detail=True)
    piece = _piece(db, company_id, piece_id, locked=True) if piece_id is not None else None
    if piece is not None and piece.version != command.expected_version:
        raise HTTPException(409, 'This physical-piece identity has a newer observation; reopen its latest history')
    if isinstance(command, WithdrawObservation):
        previous = _observation(db, company_id, piece.id, piece.latest_observation_number)
        if previous.state != 'RECORDED':
            raise HTTPException(409, 'Only a recorded observation can be withdrawn')
        payload = previous.payload_json
        source_snapshot = previous.source_snapshot_json
        source_hash = previous.source_sha256
        source_item_id, source_part_id = previous.source_inventory_item_id, previous.source_part_id
    else:
        source = source_by_id(db, company_id, command.source_inventory_item_id)
        if source is None or source['part_id'] != command.source_part_id:
            raise HTTPException(404, 'The exact inventory source and Part relationship was not found in this company')
        if source['source_sha256'] != command.expected_source_sha256:
            raise HTTPException(409, 'Inventory source evidence changed; refresh and review it before recording')
        payload = command.evidence.model_dump(mode='json')
        source_snapshot, source_hash = source['snapshot'], source['source_sha256']
        source_item_id, source_part_id = command.source_inventory_item_id, command.source_part_id
    payload_bytes = len(canonical(payload).encode('utf-8'))
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, 'Observation measurement payloads are limited to 128 KiB')
    now = datetime.utcnow()
    if piece is None:
        piece = StockPiece(
            company_id=company_id,
            label=command.label,
            version=1,
            latest_observation_number=1,
            created_by=user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(piece)
        db.flush()
        number = 1
    else:
        number = command.expected_version + 1
        changed = (
            tenant_query(db, StockPiece, company_id)
            .filter(StockPiece.id == piece.id, StockPiece.version == command.expected_version)
            .update(
                {StockPiece.version: number, StockPiece.latest_observation_number: number, StockPiece.updated_at: now},
                synchronize_session=False,
            )
        )
        if changed != 1:
            raise HTTPException(409, 'The stock observation changed during save; reopen its latest history')
    row = StockPieceObservation(
        company_id=company_id,
        stock_piece_id=piece.id,
        observation_number=number,
        state=command.state,
        reason=command.reason,
        observed_at=command.observed_datetime(),
        observer_name=command.observer_name,
        payload_schema_version=1,
        payload_json=payload,
        payload_sha256=digest(payload),
        payload_bytes=payload_bytes,
        source_inventory_item_id=source_item_id,
        source_part_id=source_part_id,
        source_snapshot_json=source_snapshot,
        source_sha256=source_hash,
        created_by=user.id,
        created_at=now,
        submitted_api_token_id=token_id,
        request_key=command.request_key,
        request_hash=request_hash,
    )
    db.add(row)
    db.flush()
    audit.log_required(
        action='CREATE',
        resource_type='stock_piece_observation',
        resource_id=row.id,
        resource_identifier=f'{piece.id}/observation/{number}',
        company_id=company_id,
        description='Recorded an advisory physical-piece observation; no inventory effect',
        new_values={
            'piece_id': piece.id,
            'observation_number': number,
            'state': row.state,
            'payload_sha256': row.payload_sha256,
            'source_sha256': row.source_sha256,
            'request_key': row.request_key,
        },
        extra_data={'advisory_only': True},
    )
    return observation_response(db, company_id, row, detail=True)

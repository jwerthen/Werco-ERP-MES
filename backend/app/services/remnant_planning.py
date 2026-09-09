"""Pure source resolution and future save/start fences for conditional planning."""

import math
from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.remnant_domain_profile import is_current_remnant_profile
from app.core.remnant_evidence import evidence_sha256, target_group_sha256
from app.core.time_utils import to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.stock_piece import StockPiece, StockPieceObservation
from app.models.user import User
from app.schemas.quote_nesting_spacing import normalize_thickness
from app.schemas.remnant_planning import PlanningSnapshotRequest, RemnantSelection, RemnantSnapshot
from app.schemas.stock_piece import ObservationEvidence, UnknownShape
from app.services import stock_piece

SOURCE_ITEM_FIELDS = (
    'id',
    'company_id',
    'part_id',
    'location',
    'warehouse',
    'lot_number',
    'serial_number',
    'received_date',
    'supplier_id',
    'po_number',
    'cert_number',
    'heat_lot',
    'expiration_date',
    'status',
    'is_active',
    'updated_at',
)
SOURCE_PART_FIELDS = (
    'id',
    'company_id',
    'part_number',
    'revision',
    'name',
    'part_type',
    'unit_of_measure',
    'is_active',
    'is_deleted',
    'updated_at',
)
REVIEW_ISSUES = [
    'This is conditional planning evidence; physical availability and material eligibility remain unverified.',
    'The server resolver checks bounded source structure, not shape topology. The calculation kernel must check geometry.',
    'Family assignment and exact grade/thickness text do not verify certification or catalog-to-inventory compatibility.',
    'Source equality is as of the read; direct-item and unattributed same-Part movements can flag conservative drift.',
    'No reservation, inventory quantity, consumption, monetary value or physical child piece is created.',
]


def require_inventory_access(db: Session, user: User, company_id: int) -> None:
    """Additional evidence-read gate, called alongside existing nesting permissions."""
    stock_piece.require_access(db, user, company_id)


def lock_observation_header(db: Session, company_id: int, piece_id: int) -> StockPiece:
    """FOR SHARE until caller commit; call after draft/run/policy locks, before audit."""
    row = (
        tenant_query(db, StockPiece, company_id)
        .autoflush(False)
        .filter(StockPiece.id == piece_id)
        .populate_existing()
        .with_for_update(read=True)
        .first()
    )
    if row is None:
        raise HTTPException(404, 'Recorded piece was not found in this company')
    return row


def _known_evidence(payload: dict) -> ObservationEvidence:
    try:
        evidence = ObservationEvidence.model_validate(payload)
        if isinstance(evidence.geometry, UnknownShape):
            raise ValueError('Record a known physical shape before planning')
        if evidence.thickness is None or evidence.grade is None:
            raise ValueError('Record known thickness and grade before planning')
        with localcontext(Context(prec=50)):
            if Decimal(evidence.thickness) * Decimal('25.4') > Decimal(100):
                raise ValueError('Reported thickness exceeds the 100 mm planning limit')
        return evidence
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, 'Reported geometry/specification cannot be selected. Review the observation.') from exc


def _build_snapshot(company_id: int, row: StockPieceObservation, piece: StockPiece) -> dict:
    source = row.source_snapshot_json
    payload = row.payload_json
    try:
        if (
            stock_piece.digest(payload) != row.payload_sha256
            or len(stock_piece.canonical(payload).encode('utf-8')) != row.payload_bytes
            or stock_piece.digest(source) != row.source_sha256
            or source.get('version') != 1
        ):
            raise ValueError('Historical evidence digest does not match')
        _known_evidence(payload)
        result = {
            'version': 1,
            'companyId': company_id,
            'pieceId': piece.id,
            'label': piece.label,
            'observationNumber': row.observation_number,
            'state': row.state,
            'observedAt': to_utc_iso(row.observed_at),
            'observerName': row.observer_name,
            'reason': row.reason,
            'createdAt': to_utc_iso(row.created_at),
            'createdBy': row.created_by,
            'submittedApiTokenId': row.submitted_api_token_id,
            'payloadSchemaVersion': row.payload_schema_version,
            'payloadSha256': row.payload_sha256,
            'payloadBytes': row.payload_bytes,
            'evidence': payload,
            'sourceInventoryItemId': row.source_inventory_item_id,
            'sourcePartId': row.source_part_id,
            'sourceSha256': row.source_sha256,
            'sourceEvidence': {
                'version': 1,
                'item': {key: source['item'][key] for key in SOURCE_ITEM_FIELDS},
                'part': {key: source['part'][key] for key in SOURCE_PART_FIELDS},
                'movement_watermark': source['movement_watermark'],
            },
        }
        return RemnantSnapshot.model_validate(result).model_dump(mode='json')
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise HTTPException(
            409, 'Recorded source evidence is inconsistent; review its history before planning'
        ) from exc


def resolve_snapshot(
    db: Session,
    user: User,
    company_id: int,
    piece_id: int,
    number: int,
    request: PlanningSnapshotRequest,
    *,
    lock_header: bool = False,
) -> dict:
    """No writes/flush/commit. Freshness is a source statement, never a reservation."""
    require_inventory_access(db, user, company_id)
    if request.expected_company_id != company_id:
        raise HTTPException(409, 'Active company changed; reopen the recorded piece')
    if lock_header:
        lock_observation_header(db, company_id, piece_id)
    record = (
        tenant_query(db, StockPieceObservation, company_id)
        .autoflush(False)
        .join(
            StockPiece, and_(StockPiece.id == StockPieceObservation.stock_piece_id, StockPiece.company_id == company_id)
        )
        .filter(StockPieceObservation.stock_piece_id == piece_id, StockPieceObservation.observation_number == number)
        .add_entity(StockPiece)
        .populate_existing()
        .first()
    )
    if record is None:
        raise HTTPException(404, 'Recorded piece observation was not found in this company')
    row, piece = record
    if (
        row.state != 'RECORDED'
        or piece.latest_observation_number != number
        or row.payload_sha256 != request.expected_payload_sha256
        or row.source_sha256 != request.expected_source_sha256
    ):
        raise HTTPException(
            409, 'Observation was changed, withdrawn or superseded; select its current recorded history'
        )
    # The source query remains unchanged. Add live header state in the SAME
    # source SQL statement so Item/Part/watermark and latestness share its snapshot.
    current = (
        stock_piece._source_query(db, company_id)
        .join(StockPiece, and_(StockPiece.company_id == company_id, StockPiece.id == piece_id))
        .filter(stock_piece.InventoryItem.id == row.source_inventory_item_id)
        .add_columns(StockPiece.latest_observation_number)
        .first()
    )
    if current is None:
        raise HTTPException(409, 'Inventory source is missing; review and record new evidence before planning')
    source = stock_piece._source_response(current[:5])
    snapshot_source = source['snapshot']
    if (
        current[5] != number
        or source['part_id'] != row.source_part_id
        or source['source_sha256'] != row.source_sha256
        or snapshot_source['item']['is_active'] is not True
        or snapshot_source['part']['is_active'] is not True
        or snapshot_source['item']['status'] != 'available'
    ):
        raise HTTPException(
            409, 'Source evidence changed, is held or inactive; review a new observation before planning'
        )
    snapshot = _build_snapshot(company_id, row, piece)
    return {
        'company_id': company_id,
        'snapshot': snapshot,
        'snapshot_sha256': evidence_sha256(snapshot),
        'latest_observation_number': number,
        'source_status': 'unchanged',
        'current_source_sha256': source['source_sha256'],
        'checked_at': to_utc_iso(datetime.now(timezone.utc)),
        'review_issues': list(REVIEW_ISSUES),
    }


def verify_assignment(selection: RemnantSelection, raw_quote: dict[str, Any]) -> None:
    """Bind the whole exact imperial group before conversion or catalog application."""
    evidence = _known_evidence(selection.snapshot.evidence.model_dump(mode='json'))
    assignment = selection.assignment
    thickness = raw_quote.get('thickness')
    try:
        if (
            type(thickness) not in (int, float)
            or not math.isfinite(thickness)
            or not isinstance(raw_quote.get('parts'), list)
            or not raw_quote['parts']
            or raw_quote.get('material') != assignment.family
            or evidence.grade != assignment.requiredGrade
            or normalize_thickness(str(thickness)) != evidence.thickness
            or assignment.thicknessIn != evidence.thickness
            or target_group_sha256(selection.groupId, assignment.requiredGrade, raw_quote)
            != assignment.targetGroupSha256
        ):
            raise ValueError('The exact group/specification no longer matches')
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(
            409, 'Material group, thickness or exact required grade changed; refresh and reassign the piece'
        ) from exc


def verify_current_selection(
    db: Session,
    user: User,
    company_id: int,
    selection: RemnantSelection,
    raw_quote: dict[str, Any],
    *,
    lock_header: bool = True,
) -> dict:
    """Future save/start helper; caller owns transaction and existing nesting auth."""
    require_inventory_access(db, user, company_id)
    if selection.snapshot.companyId != company_id:
        raise HTTPException(404, 'Recorded piece was not found in this company')
    if not is_current_remnant_profile(selection.geometryProfile.model_dump()):
        raise HTTPException(422, 'Unsupported remnant geometry profile; refresh the planning rules')
    snapshot = selection.snapshot.model_dump(mode='json')
    if evidence_sha256(snapshot) != selection.snapshotSha256:
        raise HTTPException(409, 'Recorded piece snapshot hash does not match')
    current = resolve_snapshot(
        db,
        user,
        company_id,
        selection.snapshot.pieceId,
        selection.snapshot.observationNumber,
        PlanningSnapshotRequest(
            expected_company_id=company_id,
            expected_payload_sha256=selection.snapshot.payloadSha256,
            expected_source_sha256=selection.snapshot.sourceSha256,
        ),
        lock_header=lock_header,
    )
    if current['snapshot_sha256'] != selection.snapshotSha256 or current['snapshot'] != snapshot:
        raise HTTPException(409, 'Recorded piece snapshot differs from the immutable source')
    verify_assignment(selection, raw_quote)
    return current

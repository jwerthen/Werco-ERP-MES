"""Shop Data service — list/edit CutBend tables + quoted-vs-actual (Phase 5)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.estimate_job_actual import EstimateJobActual
from app.models.estimate_workbench import CutBendRow, CutBendTable, CutBendTableKind
from app.models.quote_config import SettingsAuditLog
from app.models.rfq_quote import QuoteEstimate
from app.models.user import User
from app.services.estimate_workbench_service import ensure_cut_bend_seeded


def _log_cut_bend_change(
    db: Session,
    *,
    entity_id: int,
    entity_name: str,
    action: str,
    current_user: User,
    field_changed: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    ip_address: Optional[str] = None,
) -> None:
    """Write SettingsAuditLog for Cut/Bend edits (same shape as admin log_change)."""
    active_company_id = getattr(current_user, "_active_company_id", None)
    company_id = active_company_id if active_company_id is not None else current_user.company_id
    db.add(
        SettingsAuditLog(
            entity_type="cut_bend_row",
            entity_id=entity_id,
            entity_name=entity_name,
            action=action,
            field_changed=field_changed,
            old_value=json.dumps(old_value) if old_value is not None else None,
            new_value=json.dumps(new_value) if new_value is not None else None,
            changed_by=current_user.id,
            ip_address=ip_address,
            company_id=company_id,
        )
    )

# Editable numeric / text fields on CutBendRow
ROW_EDITABLE_FIELDS = (
    "thickness_in",
    "gauge",
    "mild_steel",
    "stainless",
    "aluminum",
    "value",
    "fillet_leg_in",
    "arc_in_per_min",
    "min_per_in",
    "notes",
)


class ShopDataError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _kind_columns(kind: str) -> List[str]:
    if kind == CutBendTableKind.LASER_SPEED.value:
        return ["thickness_in", "mild_steel", "stainless", "aluminum", "notes"]
    if kind in (CutBendTableKind.PIERCE_TIME.value, CutBendTableKind.BRAKE_TIME.value):
        return ["thickness_in", "value", "notes"]
    if kind == CutBendTableKind.GAUGE_REFERENCE.value:
        return ["gauge", "mild_steel", "stainless", "aluminum", "notes"]
    if kind == CutBendTableKind.WELD_REFERENCE.value:
        return ["fillet_leg_in", "arc_in_per_min", "min_per_in", "notes"]
    return list(ROW_EDITABLE_FIELDS)


def serialize_row(row: CutBendRow) -> Dict[str, Any]:
    return {
        "id": row.id,
        "table_id": row.table_id,
        "sort_order": row.sort_order,
        "thickness_in": row.thickness_in,
        "gauge": row.gauge,
        "mild_steel": row.mild_steel,
        "stainless": row.stainless,
        "aluminum": row.aluminum,
        "value": row.value,
        "fillet_leg_in": row.fillet_leg_in,
        "arc_in_per_min": row.arc_in_per_min,
        "min_per_in": row.min_per_in,
        "notes": row.notes,
    }


def serialize_table(table: CutBendTable) -> Dict[str, Any]:
    rows = sorted(table.rows or [], key=lambda r: (r.sort_order, r.id or 0))
    return {
        "id": table.id,
        "kind": table.kind,
        "name": table.name,
        "description": table.description,
        "columns": _kind_columns(table.kind),
        "rows": [serialize_row(r) for r in rows],
        "updated_at": table.updated_at.isoformat() + "Z" if table.updated_at else None,
    }


def list_shop_data_tables(db: Session, company_id: int) -> List[Dict[str, Any]]:
    ensure_cut_bend_seeded(db, company_id)
    db.commit()  # persist seed if it ran
    tables = (
        db.query(CutBendTable)
        .options(joinedload(CutBendTable.rows))
        .filter(CutBendTable.company_id == company_id)
        .order_by(CutBendTable.id)
        .all()
    )
    # Stable kind order matching Excel workbook
    order = [
        CutBendTableKind.LASER_SPEED.value,
        CutBendTableKind.PIERCE_TIME.value,
        CutBendTableKind.BRAKE_TIME.value,
        CutBendTableKind.GAUGE_REFERENCE.value,
        CutBendTableKind.WELD_REFERENCE.value,
    ]
    by_kind = {t.kind: t for t in tables}
    return [serialize_table(by_kind[k]) for k in order if k in by_kind]


def _get_table(db: Session, company_id: int, kind: str) -> CutBendTable:
    ensure_cut_bend_seeded(db, company_id)
    table = (
        db.query(CutBendTable)
        .options(joinedload(CutBendTable.rows))
        .filter(CutBendTable.company_id == company_id, CutBendTable.kind == kind)
        .first()
    )
    if not table:
        raise ShopDataError(f"Shop data table '{kind}' not found", 404)
    return table


def _resort_thickness_rows(table: CutBendTable) -> None:
    """Keep banded-lookup order: thickness ascending (gauge / fillet similarly)."""
    kind = table.kind
    rows = list(table.rows or [])
    if kind == CutBendTableKind.GAUGE_REFERENCE.value:
        rows.sort(key=lambda r: (r.gauge is None, r.gauge or 0, r.id or 0))
    elif kind == CutBendTableKind.WELD_REFERENCE.value:
        rows.sort(key=lambda r: (r.fillet_leg_in is None, r.fillet_leg_in or 0, r.id or 0))
    else:
        rows.sort(key=lambda r: (r.thickness_in is None, r.thickness_in or 0, r.id or 0))
    for i, row in enumerate(rows):
        row.sort_order = i


def update_shop_data_row(
    db: Session,
    *,
    company_id: int,
    kind: str,
    row_id: int,
    updates: Dict[str, Any],
    note: str,
    current_user: User,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    if not (note or "").strip():
        raise ShopDataError("A change note is required (source of the measurement / reason)")

    table = _get_table(db, company_id, kind)
    row = next((r for r in (table.rows or []) if r.id == row_id), None)
    if not row or row.company_id != company_id:
        raise ShopDataError("Row not found", 404)

    changed: List[Tuple[str, Any, Any]] = []
    for field, new_val in updates.items():
        if field not in ROW_EDITABLE_FIELDS:
            continue
        old_val = getattr(row, field)
        # Normalize empty string → None for numeric cells
        if new_val == "":
            new_val = None
        if old_val != new_val:
            setattr(row, field, new_val)
            changed.append((field, old_val, new_val))

    if not changed:
        return serialize_row(row)

    _resort_thickness_rows(table)
    table.updated_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()

    for field, old_val, new_val in changed:
        _log_cut_bend_change(
            db,
            entity_id=row.id,
            entity_name=f"{table.kind}:{table.name}",
            action="update",
            current_user=current_user,
            field_changed=field,
            old_value={"value": old_val, "note": note.strip()},
            new_value={"value": new_val, "note": note.strip()},
            ip_address=ip_address,
        )

    db.commit()
    db.refresh(row)
    return serialize_row(row)


def create_shop_data_row(
    db: Session,
    *,
    company_id: int,
    kind: str,
    payload: Dict[str, Any],
    note: str,
    current_user: User,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    if not (note or "").strip():
        raise ShopDataError("A change note is required when adding a thickness band")

    table = _get_table(db, company_id, kind)
    now = datetime.utcnow()
    row = CutBendRow(
        company_id=company_id,
        table_id=table.id,
        sort_order=len(table.rows or []),
        thickness_in=payload.get("thickness_in"),
        gauge=payload.get("gauge"),
        mild_steel=payload.get("mild_steel"),
        stainless=payload.get("stainless"),
        aluminum=payload.get("aluminum"),
        value=payload.get("value"),
        fillet_leg_in=payload.get("fillet_leg_in"),
        arc_in_per_min=payload.get("arc_in_per_min"),
        min_per_in=payload.get("min_per_in"),
        notes=payload.get("notes"),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    table.rows = list(table.rows or []) + [row]
    _resort_thickness_rows(table)
    table.updated_at = now

    _log_cut_bend_change(
        db,
        entity_id=row.id,
        entity_name=f"{table.kind}:{table.name}",
        action="create",
        current_user=current_user,
        field_changed="row",
        old_value=None,
        new_value={**serialize_row(row), "note": note.strip()},
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(row)
    return serialize_row(row)


def list_shop_data_history(
    db: Session,
    company_id: int,
    *,
    kind: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    q = (
        db.query(SettingsAuditLog)
        .filter(
            SettingsAuditLog.company_id == company_id,
            SettingsAuditLog.entity_type == "cut_bend_row",
        )
        .order_by(SettingsAuditLog.changed_at.desc())
    )
    if kind:
        q = q.filter(SettingsAuditLog.entity_name.like(f"{kind}:%"))
    rows = q.limit(min(limit, 200)).all()

    out: List[Dict[str, Any]] = []
    for a in rows:
        try:
            old_v = json.loads(a.old_value) if a.old_value else None
        except json.JSONDecodeError:
            old_v = a.old_value
        try:
            new_v = json.loads(a.new_value) if a.new_value else None
        except json.JSONDecodeError:
            new_v = a.new_value
        note = None
        if isinstance(new_v, dict):
            note = new_v.get("note")
        out.append(
            {
                "id": a.id,
                "entity_id": a.entity_id,
                "entity_name": a.entity_name,
                "action": a.action,
                "field_changed": a.field_changed,
                "old_value": old_v,
                "new_value": new_v,
                "note": note,
                "changed_by": a.changed_by,
                "changed_at": a.changed_at.isoformat() + "Z" if a.changed_at else None,
            }
        )
    return out


def _pct_delta(quoted: float, actual: Optional[float]) -> Optional[float]:
    if actual is None:
        return None
    if quoted == 0:
        return None if actual == 0 else 1.0
    return (actual - quoted) / quoted


def serialize_actual(row: EstimateJobActual) -> Dict[str, Any]:
    return {
        "id": row.id,
        "quote_estimate_id": row.quote_estimate_id,
        "work_order_id": row.work_order_id,
        "job_label": row.job_label,
        "quoted_laser_hours": row.quoted_laser_hours,
        "quoted_brake_hours": row.quoted_brake_hours,
        "quoted_weld_hours": row.quoted_weld_hours,
        "actual_laser_hours": row.actual_laser_hours,
        "actual_brake_hours": row.actual_brake_hours,
        "actual_weld_hours": row.actual_weld_hours,
        "delta_laser_pct": _pct_delta(row.quoted_laser_hours or 0, row.actual_laser_hours),
        "delta_brake_pct": _pct_delta(row.quoted_brake_hours or 0, row.actual_brake_hours),
        "delta_weld_pct": _pct_delta(row.quoted_weld_hours or 0, row.actual_weld_hours),
        "notes": row.notes,
        "entered_by": row.entered_by,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "propose_tune": _propose_tune_hints(row),
    }


def _propose_tune_hints(row: EstimateJobActual) -> List[Dict[str, Any]]:
    """Suggest which Cut/Bend table to open when variance is material."""
    hints: List[Dict[str, Any]] = []
    pairs = [
        ("laser", row.quoted_laser_hours, row.actual_laser_hours, CutBendTableKind.LASER_SPEED.value),
        ("brake", row.quoted_brake_hours, row.actual_brake_hours, CutBendTableKind.BRAKE_TIME.value),
        ("weld", row.quoted_weld_hours, row.actual_weld_hours, CutBendTableKind.WELD_REFERENCE.value),
    ]
    for label, q, a, kind in pairs:
        d = _pct_delta(q or 0, a)
        if d is not None and abs(d) >= 0.15:
            hints.append(
                {
                    "bucket": label,
                    "kind": kind,
                    "delta_pct": round(d, 4),
                    "message": (
                        f"{label.title()} actual {a:.2f}h vs quoted {q:.2f}h "
                        f"({d:+.0%}) — consider tuning {kind}"
                    ),
                }
            )
    return hints


def list_job_actuals(db: Session, company_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
    rows = (
        db.query(EstimateJobActual)
        .filter(
            EstimateJobActual.company_id == company_id,
            EstimateJobActual.is_deleted == False,  # noqa: E712
        )
        .order_by(EstimateJobActual.updated_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [serialize_actual(r) for r in rows]


def upsert_job_actual(
    db: Session,
    *,
    company_id: int,
    user_id: Optional[int],
    quote_estimate_id: Optional[int],
    work_order_id: Optional[int],
    job_label: Optional[str],
    actual_laser_hours: Optional[float],
    actual_brake_hours: Optional[float],
    actual_weld_hours: Optional[float],
    notes: Optional[str],
    quoted_laser_hours: Optional[float] = None,
    quoted_brake_hours: Optional[float] = None,
    quoted_weld_hours: Optional[float] = None,
) -> Dict[str, Any]:
    if quote_estimate_id is None and not job_label:
        raise ShopDataError("Provide quote_estimate_id or job_label")

    q_laser = quoted_laser_hours
    q_brake = quoted_brake_hours
    q_weld = quoted_weld_hours

    existing: Optional[EstimateJobActual] = None
    if quote_estimate_id is not None:
        estimate = (
            db.query(QuoteEstimate)
            .filter(QuoteEstimate.id == quote_estimate_id, QuoteEstimate.company_id == company_id)
            .first()
        )
        if not estimate:
            raise ShopDataError("Estimate not found", 404)
        breakdown = estimate.internal_breakdown or {}
        if q_laser is None:
            q_laser = float(breakdown.get("laser_hours") or 0)
        if q_brake is None:
            q_brake = float(breakdown.get("brake_hours") or 0)
        if q_weld is None:
            q_weld = float(breakdown.get("weld_hours") or 0)
        existing = (
            db.query(EstimateJobActual)
            .filter(
                EstimateJobActual.company_id == company_id,
                EstimateJobActual.quote_estimate_id == quote_estimate_id,
                EstimateJobActual.is_deleted == False,  # noqa: E712
            )
            .first()
        )

    now = datetime.utcnow()
    if existing:
        existing.actual_laser_hours = actual_laser_hours
        existing.actual_brake_hours = actual_brake_hours
        existing.actual_weld_hours = actual_weld_hours
        existing.notes = notes
        existing.work_order_id = work_order_id or existing.work_order_id
        existing.job_label = job_label or existing.job_label
        existing.quoted_laser_hours = float(q_laser or existing.quoted_laser_hours or 0)
        existing.quoted_brake_hours = float(q_brake or existing.quoted_brake_hours or 0)
        existing.quoted_weld_hours = float(q_weld or existing.quoted_weld_hours or 0)
        existing.updated_at = now
        existing.entered_by = user_id
        db.commit()
        db.refresh(existing)
        return serialize_actual(existing)

    row = EstimateJobActual(
        company_id=company_id,
        quote_estimate_id=quote_estimate_id,
        work_order_id=work_order_id,
        job_label=job_label,
        quoted_laser_hours=float(q_laser or 0),
        quoted_brake_hours=float(q_brake or 0),
        quoted_weld_hours=float(q_weld or 0),
        actual_laser_hours=actual_laser_hours,
        actual_brake_hours=actual_brake_hours,
        actual_weld_hours=actual_weld_hours,
        notes=notes,
        entered_by=user_id,
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize_actual(row)

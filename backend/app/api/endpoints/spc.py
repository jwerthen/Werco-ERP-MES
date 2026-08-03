from datetime import datetime
from math import sqrt
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.spc import ChartType, SPCCharacteristic, SPCControlLimit, SPCMeasurement, SPCProcessCapability
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService

router = APIRouter()


# ============== Pydantic Schemas ==============


class CharacteristicCreate(BaseModel):
    name: str
    part_id: int
    characteristic_type: str  # dimensional, weight, force, temperature, visual
    unit_of_measure: Optional[str] = None
    specification_nominal: Optional[float] = None
    specification_usl: Optional[float] = None
    specification_lsl: Optional[float] = None
    chart_type: Optional[str] = "xbar_r"
    subgroup_size: int = 5
    work_center_id: Optional[int] = None
    operation_number: Optional[int] = None
    is_critical: bool = False
    notes: Optional[str] = None


class CharacteristicUpdate(BaseModel):
    name: Optional[str] = None
    characteristic_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    specification_nominal: Optional[float] = None
    specification_usl: Optional[float] = None
    specification_lsl: Optional[float] = None
    chart_type: Optional[str] = None
    subgroup_size: Optional[int] = None
    work_center_id: Optional[int] = None
    operation_number: Optional[int] = None
    is_active: Optional[bool] = None
    is_critical: Optional[bool] = None
    notes: Optional[str] = None


class CharacteristicResponse(BaseModel):
    id: int
    name: str
    part_id: int
    characteristic_type: str
    unit_of_measure: Optional[str] = None
    specification_nominal: Optional[float] = None
    specification_usl: Optional[float] = None
    specification_lsl: Optional[float] = None
    chart_type: str
    subgroup_size: int
    work_center_id: Optional[int] = None
    operation_number: Optional[int] = None
    is_active: bool
    is_critical: bool
    notes: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MeasurementCreate(BaseModel):
    characteristic_id: int
    subgroup_number: int
    measurement_value: float
    sample_number: int
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    notes: Optional[str] = None


class MeasurementBatchCreate(BaseModel):
    measurements: List[MeasurementCreate]


class MeasurementResponse(BaseModel):
    id: int
    characteristic_id: int
    subgroup_number: int
    measurement_value: float
    sample_number: int
    measured_at: datetime
    measured_by: Optional[int] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    is_out_of_control: bool
    violation_rules: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class ControlLimitResponse(BaseModel):
    id: int
    characteristic_id: int
    calculation_date: datetime
    ucl: float
    lcl: float
    center_line: float
    ucl_range: Optional[float] = None
    lcl_range: Optional[float] = None
    center_line_range: Optional[float] = None
    sample_count: int
    is_current: bool
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class CapabilityResponse(BaseModel):
    id: int
    characteristic_id: int
    study_date: datetime
    sample_count: int
    mean: float
    std_dev: float
    cp: Optional[float] = None
    cpk: Optional[float] = None
    pp: Optional[float] = None
    ppk: Optional[float] = None
    within_spec_pct: Optional[float] = None
    is_capable: bool
    notes: Optional[str] = None

    class Config:
        from_attributes = True


# ============== X-bar/R Chart Constants ==============
# A2, D3, D4 constants for subgroup sizes 2-10
A2_TABLE = {2: 1.880, 3: 1.023, 4: 0.729, 5: 0.577, 6: 0.483, 7: 0.419, 8: 0.373, 9: 0.337, 10: 0.308}
D3_TABLE = {2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0.076, 8: 0.136, 9: 0.184, 10: 0.223}
D4_TABLE = {2: 3.267, 3: 2.575, 4: 2.282, 5: 2.114, 6: 2.004, 7: 1.924, 8: 1.864, 9: 1.816, 10: 1.777}
d2_TABLE = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847, 9: 2.970, 10: 3.078}


# ============== Western Electric Rules ==============


def check_western_electric_rules(values: List[float], center_line: float, ucl: float, lcl: float) -> List[dict]:
    """Check Western Electric rules for out-of-control signals.
    Returns list of violations with rule number and index."""
    violations = []
    if len(values) == 0 or ucl == lcl:
        return violations

    sigma = (ucl - center_line) / 3.0
    if sigma == 0:
        return violations

    one_sigma_upper = center_line + sigma
    one_sigma_lower = center_line - sigma
    two_sigma_upper = center_line + 2 * sigma
    two_sigma_lower = center_line - 2 * sigma

    for i, val in enumerate(values):
        point_violations = []

        # Rule 1: One point beyond 3-sigma (beyond control limits)
        if val > ucl or val < lcl:
            point_violations.append("Rule1")

        # Rule 2: Two of three consecutive points beyond 2-sigma (same side)
        if i >= 2:
            window = values[i - 2 : i + 1]
            above_2sigma = sum(1 for v in window if v > two_sigma_upper)
            below_2sigma = sum(1 for v in window if v < two_sigma_lower)
            if above_2sigma >= 2 or below_2sigma >= 2:
                point_violations.append("Rule2")

        # Rule 3: Four of five consecutive points beyond 1-sigma (same side)
        if i >= 4:
            window = values[i - 4 : i + 1]
            above_1sigma = sum(1 for v in window if v > one_sigma_upper)
            below_1sigma = sum(1 for v in window if v < one_sigma_lower)
            if above_1sigma >= 4 or below_1sigma >= 4:
                point_violations.append("Rule3")

        # Rule 4: Eight consecutive points on same side of center line
        if i >= 7:
            window = values[i - 7 : i + 1]
            above_center = sum(1 for v in window if v > center_line)
            below_center = sum(1 for v in window if v < center_line)
            if above_center == 8 or below_center == 8:
                point_violations.append("Rule4")

        if point_violations:
            violations.append({"index": i, "rules": point_violations})

    return violations


# ============== Tenant scoping helpers ==============
# EVERY handler below is keyed on a characteristic, and the characteristic is the
# ownership anchor for its measurements, control limits and capability studies
# (invariant #1). Resolving it tenant-scoped is therefore the check -- filtering the
# leaf rows alone would still let a foreign id steer a window or a max-date subquery.


def _get_owned_characteristic(db: Session, characteristic_id: int, company_id: int) -> SPCCharacteristic:
    """Resolve a characteristic inside the caller's company, or 404.

    404 rather than 403 so a foreign id is indistinguishable from an absent one and the
    status code cannot be used as an existence oracle -- the same rule as the component
    lookup in ``api/endpoints/bom.py``. The message is flat and echoes no identifier.
    """
    char = tenant_query(db, SPCCharacteristic, company_id).filter(SPCCharacteristic.id == characteristic_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Characteristic not found")
    return char


def _assert_owned_part(db: Session, part_id: int, company_id: int) -> None:
    """A characteristic may only ever point at a part of the caller's own company.

    An unvalidated FK is a cross-tenant reference that later leaks the foreign part's
    identity through joins and reports -- the same class the BOM component lookup closed.
    """
    if not tenant_query(db, Part, company_id).filter(Part.id == part_id).first():
        raise HTTPException(status_code=404, detail="Part not found")


def _assert_owned_work_center(db: Session, work_center_id: int, company_id: int) -> None:
    """Same rule for the optional work-center reference."""
    if not tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first():
        raise HTTPException(status_code=404, detail="Work center not found")


def _assert_owned_work_order(db: Session, work_order_id: int, company_id: int) -> None:
    """A measurement may only be attributed to the caller's own work order.

    Nothing joins ``spc_measurements.work_order_id`` today, so an unchecked foreign id is
    not a live read leak -- it is a FALSE TRACEABILITY POINTER written onto an AS9100D
    quality record, which is the worse failure mode: the row looks authoritative and its
    provenance is wrong. Same check the characteristic FKs get, for the same reason.
    """
    if not tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == work_order_id).first():
        raise HTTPException(status_code=404, detail="Work order not found")


# ============== Characteristics CRUD ==============


@router.get("/characteristics", response_model=List[CharacteristicResponse])
def list_characteristics(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    part_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all SPC characteristics"""
    query = db.query(SPCCharacteristic).filter(SPCCharacteristic.company_id == company_id)
    if part_id is not None:
        query = query.filter(SPCCharacteristic.part_id == part_id)
    if is_active is not None:
        query = query.filter(SPCCharacteristic.is_active == is_active)
    return query.order_by(SPCCharacteristic.name).offset(skip).limit(limit).all()


@router.post("/characteristics", response_model=CharacteristicResponse)
def create_characteristic(
    data: CharacteristicCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new SPC characteristic"""
    # Both FKs must resolve inside the caller's company before anything is constructed.
    _assert_owned_part(db, data.part_id, company_id)
    if data.work_center_id is not None:
        _assert_owned_work_center(db, data.work_center_id, company_id)

    char = SPCCharacteristic(**data.model_dump())
    char.company_id = company_id
    db.add(char)
    db.commit()
    db.refresh(char)
    return char


@router.get("/characteristics/{char_id}", response_model=CharacteristicResponse)
def get_characteristic(
    char_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a single SPC characteristic"""
    char = (
        db.query(SPCCharacteristic)
        .filter(SPCCharacteristic.id == char_id, SPCCharacteristic.company_id == company_id)
        .first()
    )
    if not char:
        raise HTTPException(status_code=404, detail="Characteristic not found")
    return char


@router.put("/characteristics/{char_id}", response_model=CharacteristicResponse)
def update_characteristic(
    char_id: int,
    data: CharacteristicUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update an SPC characteristic"""
    char = (
        db.query(SPCCharacteristic)
        .filter(SPCCharacteristic.id == char_id, SPCCharacteristic.company_id == company_id)
        .first()
    )
    if not char:
        raise HTTPException(status_code=404, detail="Characteristic not found")

    update_data = data.model_dump(exclude_unset=True)
    # ``work_center_id`` is the only FK this payload can repoint -- validate it BEFORE the
    # setattr loop so a refusal leaves the row untouched. (``part_id`` is not on the update
    # schema, so a characteristic can never be moved onto another part at all.)
    if update_data.get("work_center_id") is not None:
        _assert_owned_work_center(db, update_data["work_center_id"], company_id)

    for key, value in update_data.items():
        setattr(char, key, value)
    char.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(char)
    return char


# ============== Measurements ==============


@router.post("/measurements", response_model=List[MeasurementResponse])
def add_measurements(
    batch: MeasurementBatchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Add one or more measurements (batch entry for subgroups)"""
    # A batch may name several characteristics, so every DISTINCT id is validated before
    # the first ``db.add`` -- a mixed batch fails whole rather than half-writing. The
    # optional work_order_id is validated the same way and in the same pass.
    for cid in {m.characteristic_id for m in batch.measurements}:
        _get_owned_characteristic(db, cid, company_id)
    for wo_id in {m.work_order_id for m in batch.measurements if m.work_order_id is not None}:
        _assert_owned_work_order(db, wo_id, company_id)

    created = []
    for m in batch.measurements:
        measurement = SPCMeasurement(
            # ``spc_measurements.company_id`` is NOT NULL with no server default (migration
            # 026 drops the interim one), so omitting it raised NotNullViolation on every
            # call to this endpoint -- it is the tenant scope AND the reason it 500'd.
            company_id=company_id,
            characteristic_id=m.characteristic_id,
            subgroup_number=m.subgroup_number,
            measurement_value=m.measurement_value,
            sample_number=m.sample_number,
            measured_by=current_user.id,
            work_order_id=m.work_order_id,
            lot_number=m.lot_number,
            serial_number=m.serial_number,
            notes=m.notes,
        )
        db.add(measurement)
        created.append(measurement)
    db.commit()
    for m in created:
        db.refresh(m)
    return created


@router.get("/measurements/{characteristic_id}", response_model=List[MeasurementResponse])
def get_measurements(
    characteristic_id: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get measurements for a characteristic, with optional date filters"""
    _get_owned_characteristic(db, characteristic_id, company_id)

    query = tenant_query(db, SPCMeasurement, company_id).filter(SPCMeasurement.characteristic_id == characteristic_id)
    if start_date:
        query = query.filter(SPCMeasurement.measured_at >= datetime.fromisoformat(start_date))
    if end_date:
        query = query.filter(SPCMeasurement.measured_at <= datetime.fromisoformat(end_date))

    return query.order_by(SPCMeasurement.subgroup_number, SPCMeasurement.sample_number).limit(limit).all()


# ============== Chart Data ==============


@router.get("/chart-data/{characteristic_id}")
def get_chart_data(
    characteristic_id: int,
    last_n_subgroups: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get formatted chart data: subgroup averages, ranges, and control/spec limits"""
    char = _get_owned_characteristic(db, characteristic_id, company_id)

    # Get measurements grouped by subgroup. Bound the read to the rendered window:
    # resolve the last-N distinct subgroup numbers first (a query shaped for
    # ix_spc_measurements_char_subgroup), then fetch only those subgroups' rows —
    # fetch cost stays O(window), not O(full history). last_n_subgroups <= 0
    # keeps the pre-existing unbounded behavior (0 = full history).
    # The tenant predicate goes on the INNER subquery as well as the outer fetch: scoping
    # only the outer query would still let foreign rows choose which window is rendered.
    measurements_query = tenant_query(db, SPCMeasurement, company_id).filter(
        SPCMeasurement.characteristic_id == characteristic_id
    )
    if last_n_subgroups > 0:
        recent_subgroup_numbers = (
            db.query(SPCMeasurement.subgroup_number)
            .filter(
                SPCMeasurement.company_id == company_id,
                SPCMeasurement.characteristic_id == characteristic_id,
            )
            .distinct()
            .order_by(SPCMeasurement.subgroup_number.desc())
            .limit(last_n_subgroups)
            .scalar_subquery()
        )
        measurements_query = measurements_query.filter(SPCMeasurement.subgroup_number.in_(recent_subgroup_numbers))
    measurements = measurements_query.order_by(SPCMeasurement.subgroup_number, SPCMeasurement.sample_number).all()

    # Group by subgroup_number
    subgroups = {}
    for m in measurements:
        if m.subgroup_number not in subgroups:
            subgroups[m.subgroup_number] = []
        subgroups[m.subgroup_number].append(m)

    # Calculate subgroup stats
    sorted_sg_numbers = sorted(subgroups.keys())
    # With a positive window the bounded fetch above already returns <= N subgroups,
    # so this trim can only fire on the legacy last_n_subgroups < 0 path — its
    # negative-slice behavior is test-pinned; don't "clean it up".
    if last_n_subgroups and len(sorted_sg_numbers) > last_n_subgroups:
        sorted_sg_numbers = sorted_sg_numbers[-last_n_subgroups:]

    chart_points = []
    for sg_num in sorted_sg_numbers:
        sg_measurements = subgroups[sg_num]
        values = [m.measurement_value for m in sg_measurements]
        sg_mean = sum(values) / len(values) if values else 0
        sg_range = max(values) - min(values) if len(values) > 1 else 0
        any_ooc = any(m.is_out_of_control for m in sg_measurements)
        violations = set()
        for m in sg_measurements:
            if m.violation_rules:
                for rule in m.violation_rules.split(","):
                    violations.add(rule.strip())

        chart_points.append(
            {
                "subgroup_number": sg_num,
                "mean": round(sg_mean, 6),
                "range": round(sg_range, 6),
                "sample_count": len(values),
                "is_out_of_control": any_ooc,
                "violations": list(violations),
                "measured_at": to_utc_iso(sg_measurements[0].measured_at) if sg_measurements else None,
            }
        )

    # Get current control limits
    control_limit = (
        tenant_query(db, SPCControlLimit, company_id)
        .filter(SPCControlLimit.characteristic_id == characteristic_id, SPCControlLimit.is_current == True)
        .first()
    )

    cl_data = None
    if control_limit:
        cl_data = {
            "ucl": control_limit.ucl,
            "lcl": control_limit.lcl,
            "center_line": control_limit.center_line,
            "ucl_range": control_limit.ucl_range,
            "lcl_range": control_limit.lcl_range,
            "center_line_range": control_limit.center_line_range,
        }

    return {
        "characteristic": {
            "id": char.id,
            "name": char.name,
            "chart_type": char.chart_type.value if isinstance(char.chart_type, ChartType) else char.chart_type,
            "subgroup_size": char.subgroup_size,
            "specification_nominal": char.specification_nominal,
            "specification_usl": char.specification_usl,
            "specification_lsl": char.specification_lsl,
            "unit_of_measure": char.unit_of_measure,
        },
        "chart_points": chart_points,
        "control_limits": cl_data,
    }


# ============== Control Limits ==============


@router.post("/control-limits/{characteristic_id}/calculate", response_model=ControlLimitResponse)
def calculate_control_limits(
    characteristic_id: int,
    last_n_subgroups: Optional[int] = None,
    db: Session = Depends(get_db),
    # This handler REWRITES quality evidence in place (see the flag-change audit below), so
    # it is gated to the roles that own the quality system -- matching the NCR void/restore
    # gate in ``api/endpoints/quality.py``. Before this, any authenticated role could fire it.
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Calculate control limits from measurement data and apply Western Electric rules"""
    char = _get_owned_characteristic(db, characteristic_id, company_id)

    # Get measurements
    measurements = (
        tenant_query(db, SPCMeasurement, company_id)
        .filter(SPCMeasurement.characteristic_id == characteristic_id)
        .order_by(SPCMeasurement.subgroup_number, SPCMeasurement.sample_number)
        .all()
    )

    # Group by subgroup. The PRIOR out-of-control state is captured in the same pass,
    # before the bulk UPDATEs below overwrite it, so the audit row can report old -> new.
    subgroups = {}
    prior_flags: dict = {}
    for m in measurements:
        if m.subgroup_number not in subgroups:
            subgroups[m.subgroup_number] = []
        subgroups[m.subgroup_number].append(m.measurement_value)
        was_ooc, was_rules = prior_flags.get(m.subgroup_number, (False, None))
        prior_flags[m.subgroup_number] = (was_ooc or bool(m.is_out_of_control), was_rules or m.violation_rules)

    sorted_sg_numbers = sorted(subgroups.keys())
    if last_n_subgroups and len(sorted_sg_numbers) > last_n_subgroups:
        sorted_sg_numbers = sorted_sg_numbers[-last_n_subgroups:]

    if len(sorted_sg_numbers) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 subgroups to calculate control limits")

    n = char.subgroup_size
    if n < 2 or n > 10:
        raise HTTPException(status_code=400, detail="Subgroup size must be between 2 and 10 for X-bar/R charts")

    # Calculate subgroup means and ranges
    sg_means = []
    sg_ranges = []
    for sg_num in sorted_sg_numbers:
        values = subgroups[sg_num]
        sg_means.append(sum(values) / len(values))
        sg_ranges.append(max(values) - min(values))

    # Grand mean (X-double-bar) and average range (R-bar)
    x_double_bar = sum(sg_means) / len(sg_means)
    r_bar = sum(sg_ranges) / len(sg_ranges)

    # Control limits for X-bar chart
    a2 = A2_TABLE.get(n, 0.577)
    ucl = x_double_bar + a2 * r_bar
    lcl = x_double_bar - a2 * r_bar

    # Control limits for R chart
    d3 = D3_TABLE.get(n, 0)
    d4 = D4_TABLE.get(n, 2.114)
    ucl_range = d4 * r_bar
    lcl_range = d3 * r_bar

    # Mark previous control limits as not current. The tenant predicate belongs on the
    # bulk UPDATE itself -- without it this statement rewrites another company's rows.
    # The ids are read first so the audit row names what this recalculation superseded.
    superseded_query = db.query(SPCControlLimit.id).filter(
        SPCControlLimit.company_id == company_id,
        SPCControlLimit.characteristic_id == characteristic_id,
        SPCControlLimit.is_current == True,
    )
    superseded_ids = [row_id for (row_id,) in superseded_query.all()]
    db.query(SPCControlLimit).filter(
        SPCControlLimit.company_id == company_id,
        SPCControlLimit.characteristic_id == characteristic_id,
        SPCControlLimit.is_current == True,
    ).update({"is_current": False})

    # Save new control limits
    new_cl = SPCControlLimit(
        # NOT NULL, no server default -- omitting it 500'd this endpoint in production.
        company_id=company_id,
        characteristic_id=characteristic_id,
        ucl=round(ucl, 6),
        lcl=round(lcl, 6),
        center_line=round(x_double_bar, 6),
        ucl_range=round(ucl_range, 6),
        lcl_range=round(lcl_range, 6),
        center_line_range=round(r_bar, 6),
        sample_count=len(sorted_sg_numbers),
        is_current=True,
        calculated_by=current_user.id,
    )
    db.add(new_cl)
    db.flush()  # so the audit row below can name the control-limit row it describes

    # Apply Western Electric rules to subgroup means
    violations = check_western_electric_rules(sg_means, x_double_bar, ucl, lcl)
    violation_map = {}  # sg_index -> rules
    for v in violations:
        violation_map[v["index"]] = v["rules"]

    # Update measurements with out-of-control flags. This REWRITES HISTORICAL ROWS IN
    # PLACE: every subgroup in the window that no longer violates is reset to
    # False/None, so a re-run with a narrower ``last_n_subgroups`` recomputes the limits
    # from a subset and can CLEAR a Western Electric violation previously recorded
    # against shipped material. The before-values are collected here and audited below --
    # without that row the erasure has no actor, no timestamp and no prior value.
    flag_changes = []
    for idx, sg_num in enumerate(sorted_sg_numbers):
        rules = violation_map.get(idx, [])
        is_ooc = len(rules) > 0
        rule_str = ",".join(rules) if rules else None
        was_ooc, was_rules = prior_flags.get(sg_num, (False, None))
        if (bool(was_ooc), was_rules) != (is_ooc, rule_str):
            flag_changes.append(
                {
                    "subgroup_number": sg_num,
                    "old": {"is_out_of_control": bool(was_ooc), "violation_rules": was_rules},
                    "new": {"is_out_of_control": is_ooc, "violation_rules": rule_str},
                }
            )
        # Same rule as the is_current flip above: an unscoped bulk UPDATE would stamp
        # out-of-control flags onto another company's measurements.
        db.query(SPCMeasurement).filter(
            SPCMeasurement.company_id == company_id,
            SPCMeasurement.characteristic_id == characteristic_id,
            SPCMeasurement.subgroup_number == sg_num,
        ).update(
            {
                "is_out_of_control": is_ooc,
                "violation_rules": rule_str,
            }
        )

    # The violations this run ERASED are the AS9100D-relevant number, so they get their
    # own count rather than being left for a reader to derive from the change list.
    violations_cleared = [c["subgroup_number"] for c in flag_changes if c["old"]["is_out_of_control"]]

    # Audited BEFORE db.commit(): a row added after the commit is discarded by the
    # get_db teardown and the recalculation would go unrecorded.
    audit.log_create(
        "spc_control_limit",
        new_cl.id,
        f"{char.name} control limits",
        new_values=new_cl,
        description=(
            f"Recalculated control limits for SPC characteristic '{char.name}' "
            f"from {len(sorted_sg_numbers)} subgroups"
        ),
        extra_data={
            "characteristic_id": characteristic_id,
            "characteristic_name": char.name,
            "part_id": char.part_id,
            # The caller-supplied window is what makes the rewrite steerable, so it is
            # recorded verbatim alongside the subgroups it actually resolved to.
            "last_n_subgroups": last_n_subgroups,
            "subgroups_used": len(sorted_sg_numbers),
            "subgroup_numbers": sorted_sg_numbers,
            "subgroup_size": n,
            "control_limits": {
                "ucl": new_cl.ucl,
                "lcl": new_cl.lcl,
                "center_line": new_cl.center_line,
                "ucl_range": new_cl.ucl_range,
                "lcl_range": new_cl.lcl_range,
                "center_line_range": new_cl.center_line_range,
            },
            "superseded_control_limit_ids": superseded_ids,
            "measurement_flag_changes": flag_changes,
            "violations_cleared_subgroups": violations_cleared,
            "violations_cleared_count": len(violations_cleared),
        },
    )

    db.commit()
    db.refresh(new_cl)
    return new_cl


@router.get("/control-limits/{characteristic_id}", response_model=Optional[ControlLimitResponse])
def get_control_limits(
    characteristic_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get current control limits for a characteristic"""
    # Ownership is checked on the characteristic so a FOREIGN id 404s, while an owned
    # characteristic with no limits yet keeps its established 200/null response.
    _get_owned_characteristic(db, characteristic_id, company_id)

    cl = (
        tenant_query(db, SPCControlLimit, company_id)
        .filter(SPCControlLimit.characteristic_id == characteristic_id, SPCControlLimit.is_current == True)
        .first()
    )
    return cl


# ============== Capability Study ==============


@router.post("/capability-study/{characteristic_id}", response_model=CapabilityResponse)
def run_capability_study(
    characteristic_id: int,
    last_n_subgroups: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Run a Cp/Cpk process capability study"""
    char = _get_owned_characteristic(db, characteristic_id, company_id)

    if char.specification_usl is None or char.specification_lsl is None:
        raise HTTPException(status_code=400, detail="USL and LSL must be defined to run capability study")

    # Get all measurements
    query = (
        tenant_query(db, SPCMeasurement, company_id)
        .filter(SPCMeasurement.characteristic_id == characteristic_id)
        .order_by(SPCMeasurement.subgroup_number, SPCMeasurement.sample_number)
    )

    measurements = query.all()
    if len(measurements) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 measurements for capability study")

    values = [m.measurement_value for m in measurements]

    # Optionally limit to recent subgroups
    if last_n_subgroups:
        subgroups = {}
        for m in measurements:
            if m.subgroup_number not in subgroups:
                subgroups[m.subgroup_number] = []
            subgroups[m.subgroup_number].append(m.measurement_value)
        sorted_sg = sorted(subgroups.keys())
        if len(sorted_sg) > last_n_subgroups:
            sorted_sg = sorted_sg[-last_n_subgroups:]
        values = []
        for sg in sorted_sg:
            values.extend(subgroups[sg])

    n = len(values)
    mean_val = sum(values) / n
    variance = sum((v - mean_val) ** 2 for v in values) / (n - 1)
    std_dev = sqrt(variance) if variance > 0 else 0.0001

    usl = char.specification_usl
    lsl = char.specification_lsl

    # Cp = (USL - LSL) / (6 * sigma)
    cp = (usl - lsl) / (6 * std_dev)
    # Cpk = min((USL - mean) / (3 * sigma), (mean - LSL) / (3 * sigma))
    cpk = min((usl - mean_val) / (3 * std_dev), (mean_val - lsl) / (3 * std_dev))

    # Pp and Ppk (using overall standard deviation - same as Cp/Cpk for this context)
    # For a more rigorous implementation, Pp uses overall sigma vs within-subgroup sigma
    pp = cp
    ppk = cpk

    # Within spec percentage
    within_spec = sum(1 for v in values if lsl <= v <= usl)
    within_spec_pct = round((within_spec / n) * 100, 2) if n > 0 else 0

    is_capable = cpk >= 1.33

    capability = SPCProcessCapability(
        # NOT NULL, no server default -- omitting it 500'd this endpoint in production.
        company_id=company_id,
        characteristic_id=characteristic_id,
        sample_count=n,
        mean=round(mean_val, 6),
        std_dev=round(std_dev, 6),
        cp=round(cp, 4),
        cpk=round(cpk, 4),
        pp=round(pp, 4),
        ppk=round(ppk, 4),
        within_spec_pct=within_spec_pct,
        is_capable=is_capable,
        performed_by=current_user.id,
    )
    db.add(capability)
    db.commit()
    db.refresh(capability)
    return capability


@router.get("/capability/{characteristic_id}", response_model=Optional[CapabilityResponse])
def get_capability(
    characteristic_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get the latest capability study results for a characteristic"""
    _get_owned_characteristic(db, characteristic_id, company_id)

    return (
        tenant_query(db, SPCProcessCapability, company_id)
        .filter(SPCProcessCapability.characteristic_id == characteristic_id)
        .order_by(SPCProcessCapability.study_date.desc())
        .first()
    )


# ============== Out-of-Control & Violations ==============


@router.get("/out-of-control")
def get_out_of_control(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all characteristics that have recent out-of-control points"""
    # Find characteristics with OOC points in recent measurements. Unscoped, this aggregate
    # spanned every company and handed any authenticated caller a platform-wide list of
    # other tenants' out-of-control characteristics.
    ooc_chars = (
        db.query(
            SPCMeasurement.characteristic_id,
            func.count(SPCMeasurement.id).label("ooc_count"),
            func.max(SPCMeasurement.measured_at).label("last_ooc"),
        )
        .filter(SPCMeasurement.company_id == company_id, SPCMeasurement.is_out_of_control == True)
        .group_by(SPCMeasurement.characteristic_id)
        .all()
    )

    # One scoped lookup for the whole page instead of a query per row: the tenant fix and
    # the N+1 removal are the same edit.
    char_ids = [row.characteristic_id for row in ooc_chars]
    chars_by_id = {
        char.id: char
        for char in tenant_query(db, SPCCharacteristic, company_id)
        .filter(SPCCharacteristic.id.in_(char_ids), SPCCharacteristic.is_active == True)
        .all()
    }

    results = []
    for row in ooc_chars:
        char = chars_by_id.get(row.characteristic_id)
        if char:
            results.append(
                {
                    "characteristic_id": row.characteristic_id,
                    "characteristic_name": char.name,
                    "part_id": char.part_id,
                    "is_critical": char.is_critical,
                    "ooc_count": row.ooc_count,
                    "last_ooc": to_utc_iso(row.last_ooc),
                }
            )

    return results


@router.get("/violations/{characteristic_id}")
def check_violations(
    characteristic_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Check Western Electric rules for a characteristic and return violations"""
    char = _get_owned_characteristic(db, characteristic_id, company_id)

    # Get current control limits
    cl = (
        tenant_query(db, SPCControlLimit, company_id)
        .filter(SPCControlLimit.characteristic_id == characteristic_id, SPCControlLimit.is_current == True)
        .first()
    )

    if not cl:
        return {"violations": [], "message": "No control limits calculated yet"}

    # Get measurements grouped by subgroup
    measurements = (
        tenant_query(db, SPCMeasurement, company_id)
        .filter(SPCMeasurement.characteristic_id == characteristic_id)
        .order_by(SPCMeasurement.subgroup_number, SPCMeasurement.sample_number)
        .all()
    )

    subgroups = {}
    for m in measurements:
        if m.subgroup_number not in subgroups:
            subgroups[m.subgroup_number] = []
        subgroups[m.subgroup_number].append(m.measurement_value)

    sorted_sg_numbers = sorted(subgroups.keys())
    sg_means = []
    for sg_num in sorted_sg_numbers:
        values = subgroups[sg_num]
        sg_means.append(sum(values) / len(values))

    violations = check_western_electric_rules(sg_means, cl.center_line, cl.ucl, cl.lcl)

    result_violations = []
    for v in violations:
        sg_idx = v["index"]
        sg_num = sorted_sg_numbers[sg_idx] if sg_idx < len(sorted_sg_numbers) else None
        result_violations.append(
            {
                "subgroup_number": sg_num,
                "subgroup_mean": round(sg_means[sg_idx], 6) if sg_idx < len(sg_means) else None,
                "rules_violated": v["rules"],
            }
        )

    return {
        "characteristic_id": characteristic_id,
        "characteristic_name": char.name,
        "control_limits": {
            "ucl": cl.ucl,
            "lcl": cl.lcl,
            "center_line": cl.center_line,
        },
        "violations": result_violations,
        "total_subgroups": len(sorted_sg_numbers),
        "total_violations": len(result_violations),
    }


# ============== Dashboard ==============


@router.get("/dashboard")
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """SPC Dashboard summary: total characteristics, OOC count, avg Cpk, attention needed"""
    # Every aggregate on this page used to span all tenants: the count, the OOC set, and
    # the Cp/Cpk average were computed over every company's quality data.
    total_chars = (
        db.query(func.count(SPCCharacteristic.id))
        .filter(SPCCharacteristic.company_id == company_id, SPCCharacteristic.is_active == True)
        .scalar()
        or 0
    )

    # Count characteristics with OOC points
    ooc_char_ids = (
        db.query(SPCMeasurement.characteristic_id)
        .filter(SPCMeasurement.company_id == company_id, SPCMeasurement.is_out_of_control == True)
        .distinct()
        .all()
    )
    ooc_count = len(ooc_char_ids)

    # Get latest Cpk for each characteristic
    # Subquery: latest capability study per characteristic. The predicate is on the INNER
    # subquery as well as the outer join -- scoping only the outer query would leave a
    # foreign study able to set max_date and silently drop this tenant's latest row.
    latest_caps = (
        db.query(SPCProcessCapability.characteristic_id, func.max(SPCProcessCapability.study_date).label("max_date"))
        .filter(SPCProcessCapability.company_id == company_id)
        .group_by(SPCProcessCapability.characteristic_id)
        .subquery()
    )

    capabilities = (
        tenant_query(db, SPCProcessCapability, company_id)
        .join(
            latest_caps,
            and_(
                SPCProcessCapability.characteristic_id == latest_caps.c.characteristic_id,
                SPCProcessCapability.study_date == latest_caps.c.max_date,
            ),
        )
        .all()
    )

    cpk_values = [c.cpk for c in capabilities if c.cpk is not None]
    avg_cpk = round(sum(cpk_values) / len(cpk_values), 4) if cpk_values else None

    below_threshold = [c for c in capabilities if c.cpk is not None and c.cpk < 1.33]

    # Characteristics needing attention: OOC or Cpk < 1.33
    attention_char_ids = set(r[0] for r in ooc_char_ids)
    for c in below_threshold:
        attention_char_ids.add(c.characteristic_id)

    # One scoped lookup for the whole attention list rather than a query per id.
    attention_chars_by_id = {
        char.id: char
        for char in tenant_query(db, SPCCharacteristic, company_id)
        .filter(SPCCharacteristic.id.in_(list(attention_char_ids)), SPCCharacteristic.is_active == True)
        .all()
    }

    attention_chars = []
    for cid in attention_char_ids:
        char = attention_chars_by_id.get(cid)
        if char:
            cap = next((c for c in capabilities if c.characteristic_id == cid), None)
            attention_chars.append(
                {
                    "id": char.id,
                    "name": char.name,
                    "part_id": char.part_id,
                    "is_critical": char.is_critical,
                    "cpk": cap.cpk if cap else None,
                    "has_ooc": cid in set(r[0] for r in ooc_char_ids),
                }
            )

    return {
        "total_characteristics": total_chars,
        "out_of_control_count": ooc_count,
        "average_cpk": avg_cpk,
        "characteristics_below_cpk_threshold": len(below_threshold),
        "attention_needed": attention_chars,
    }

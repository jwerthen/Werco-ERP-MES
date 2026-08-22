import enum
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_active_user, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.bom import BOM, BOMItem
from app.models.part import ENGINEERING_PART_TYPES, Part, PartType, UnitOfMeasure, is_engineering_part_type
from app.models.user import User, UserRole
from app.schemas.backflush_preview import BackflushDiagnostic, PartBackflushReadinessResponse
from app.schemas.part import PartCreate, PartResponse, PartUpdate
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    BACKFLUSH_BLOCKING,
    backflush_readiness_for_part,
    backflush_refusal_sentence,
)
from app.services.import_service import ImportFileError, parse_import_file
from app.services.material_tie_part_gate import assert_part_type_change_allowed
from app.services.part_number_service import generate_werco_part_number, normalize_description

router = APIRouter()


def assert_backflush_change_allowed(
    db: Session,
    part: Part,
    update_data: Dict[str, Any],
    *,
    company_id: int,
) -> Optional[Dict[str, Any]]:
    """THE refusal gate behind ``Part.backflush_components``. Shared by parts AND materials.

    ``PUT /materials/{id}`` is a byte-identical ``setattr`` loop over the SAME
    ``PartUpdate`` schema, writing the SAME ``parts`` rows, so a gate implemented in only
    one of the two files is not a gate at all. It lives here and is imported there, the
    way ``_part_to_response`` already is.

    Turning this flag on is a permanent, shop-wide policy change: from then on, completing
    a work order for the part moves its BOM components out of stock automatically, forever,
    and writes the lots it drew onto the as-built record. If the BOM or routing cannot be
    resolved cleanly, that automation issues the WRONG material -- so the flip is refused
    (**409**) while any BLOCKING readiness diagnostic stands.

    ``detail`` is a **plain string**: the blocker sentences joined with a space. The Axios
    interceptor renders a server ``detail`` verbatim, and the whole point of the refusal is
    that a human reads it and goes and fixes a BOM line. The STRUCTURED list lives on
    ``GET /parts/{id}/backflush-readiness``, which the UI calls first.

    **The gate runs only when the request actually turns the flag ON.** Turning it OFF is
    always allowed (stopping automatic consumption can never issue wrong material), and a
    request that re-states the flag's current value changes nothing and is not gated.

    Returns the ``extra_data`` to hang on the part-update audit row when the flag really
    moved, else ``None``. The flag itself already lands in that row's ``changes`` map
    (both sides of the diff enumerate model columns, and ``backflush_components`` is one);
    what this adds is the READINESS VERDICT that authorised the flip, which is not
    otherwise reconstructable after the BOM has since been edited.

    **The audit trail of a flip is queried as ``resource_type='part'``, from BOTH doors.**
    ``update_material`` logs this row under ``resource_type="part"`` rather than
    ``"material"`` for exactly that reason: the two handlers write the same ``parts`` rows
    through the same gate, and an auditor asking "who armed automatic stock movement, and
    when" must not have to know which URL was used. The recipe is: ``resource_type='part'``
    AND ``action='UPDATE'`` AND ``extra_data->>'backflush_readiness' IS NOT NULL``.

    ACCEPTED RESIDUALS -- recorded here rather than designed around, because the owner
    chose the ordinary part-edit field over a dedicated reasoned verb:

    * **Supervisor-tier.** The gate on ``PUT /parts/{id}`` and ``PUT /materials/{id}`` is
      ``[ADMIN, MANAGER, SUPERVISOR]`` -- the same permission as editing a description.
    * **No reason is captured.** Every other control change in this series (RETURN, untie,
      receiving void) requires a written reason; this one records the readiness verdict
      instead.
    * **Concurrent flips never 409.** ``Part`` maps NO ``version`` column (migration 004
      versioned the table, the model never mapped it), ``PartUpdate.version`` is required
      but written onto an unmapped attribute by the setattr loop, and
      ``_part_to_response`` returns a hard-coded ``0``. Optimistic locking on parts is
      cosmetic; last write wins.
    * **``scripts/seed_data.py`` splats ``Part(**data)``** and can therefore set the column
      with no code change, bypassing this gate. Not a production path.
    * **The create/delete rows on the materials router still log ``"material"``.** Only
      the UPDATE door was normalised, because only it can carry this flag; reconstructing
      a material record's FULL history still means querying both resource types, exactly
      as it did before.

    NOT a residual, closed rather than accepted: a SOFT-DELETED part cannot be armed. The
    ``PUT`` lookups filter ``company_id`` only, so a deleted part is still reachable by id,
    but ``backflush_readiness_for_part`` raises a blocking ``deleted_part`` diagnostic and
    this gate refuses on it -- through both doors, with no change to a lookup four other
    handlers share.
    """
    if "backflush_components" not in update_data:
        return None
    requested = bool(update_data["backflush_components"])
    if requested == bool(part.backflush_components):
        # No state change -> nothing to gate and nothing to record. (Re-stating the
        # current value must not be able to fail: a client PUTting a whole form back
        # would otherwise be refused for a BOM defect it is not introducing.)
        return None

    checked_at = to_utc_iso(datetime.utcnow())
    if not requested:
        return {
            "backflush_components": False,
            "backflush_readiness": "not_evaluated_disable",
            "backflush_readiness_checked_at": checked_at,
        }

    diagnostics = backflush_readiness_for_part(db, part, company_id=company_id)
    blockers = [d for d in diagnostics if d.severity == BACKFLUSH_BLOCKING]
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=" ".join(backflush_refusal_sentence(part.part_number, d) for d in blockers),
        )
    return {
        "backflush_components": True,
        "backflush_readiness": "clean",
        "backflush_readiness_checked_at": checked_at,
        # Blockers cannot be present (we would have raised), but filter on severity
        # anyway so the key means what it says regardless of what runs above it.
        "backflush_readiness_advisories": [d.code for d in diagnostics if d.severity != BACKFLUSH_BLOCKING],
    }


class PartItemGroup(str, enum.Enum):
    ENGINEERING = "engineering"
    MATERIALS = "materials"
    ALL = "all"


class PartCsvImportError(BaseModel):
    row: int
    part_number: Optional[str] = None
    reason: str


class PartCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[PartCsvImportError]
    dry_run: bool = False


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "active"}:
        return True
    if normalized in {"false", "0", "no", "n", "inactive"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'")


def _parse_float(value: str, field_name: str, default: float = 0.0) -> float:
    if value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _parse_int(value: str, field_name: str, default: int = 0) -> int:
    if value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _normalize_enum(value, fallback):
    if hasattr(value, "value"):
        return str(value.value).strip().lower()
    if isinstance(value, str):
        return value.strip().lower()
    return fallback


logger = logging.getLogger(__name__)


def _part_to_response(part: Part) -> Optional[PartResponse]:
    """Serialize a part for a list endpoint, or ``None`` if it cannot be represented.

    The ``None`` is not cosmetic: both list endpoints FILTER it out, so a part that
    fails here silently disappears from ``GET /parts/`` and ``GET /materials/`` --
    from every picker and search built on them -- while ``GET /parts/{id}`` still
    returns it. Real stock went missing this way (sheet/plate rows whose numbers carry
    spaces and inch marks, refused by an over-strict read contract until
    ``PartResponse`` relaxed ``part_number``).

    So the swallow stays -- one unrepresentable row must not 500 a whole catalog page
    -- but it no longer stays QUIET. Anything that lands in this log is a part the shop
    cannot see, and that is the only signal there will be.
    """
    try:
        part_type_val = _normalize_enum(part.part_type, PartType.MANUFACTURED.value)
        uom_val = _normalize_enum(part.unit_of_measure, UnitOfMeasure.EACH.value)
        return PartResponse(
            id=part.id,
            part_number=part.part_number or "",
            revision=part.revision or "A",
            name=part.name or "",
            description=part.description,
            part_type=PartType(part_type_val),
            unit_of_measure=UnitOfMeasure(uom_val),
            standard_cost=part.standard_cost or 0.0,
            material_cost=part.material_cost or 0.0,
            labor_cost=part.labor_cost or 0.0,
            overhead_cost=part.overhead_cost or 0.0,
            lead_time_days=part.lead_time_days or 0,
            safety_stock=part.safety_stock or 0.0,
            reorder_point=part.reorder_point or 0.0,
            reorder_quantity=part.reorder_quantity or 0.0,
            is_critical=part.is_critical or False,
            requires_inspection=part.requires_inspection if part.requires_inspection is not None else True,
            inspection_requirements=part.inspection_requirements,
            customer_name=part.customer_name,
            customer_part_number=part.customer_part_number,
            drawing_number=part.drawing_number,
            # LOAD-BEARING, not a field like the others. This helper hand-builds every
            # kwarg and is wrapped in ``except Exception: return None`` with the callers
            # filtering the ``None``s out -- so omitting a field here does not raise, it
            # makes the LIST endpoints report a stale default while ``GET /parts/{id}``
            # (which serialises the ORM object directly) reports the truth. Two endpoints
            # disagreeing about whether a part auto-consumes its BOM is exactly the kind
            # of divergence nobody notices until material has moved.
            backflush_components=bool(part.backflush_components),
            is_active=part.is_active if part.is_active is not None else True,
            status=part.status or "active",
            created_at=part.created_at,
            updated_at=part.updated_at,
            version=0,
        )
    except Exception:
        logger.warning(
            "part_to_response failed; part is HIDDEN from list endpoints "
            "(part_id=%s, company_id=%s, part_number=%r)",
            getattr(part, "id", None),
            getattr(part, "company_id", None),
            getattr(part, "part_number", None),
            exc_info=True,
        )
        return None


@router.get("/", response_model=List[PartResponse], summary="List all parts")
def list_parts(
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of records to return"),
    search: Optional[str] = Query(
        None, description="Search in part number, name, description, or customer part number"
    ),
    part_type: Optional[PartType] = Query(
        None, description="Filter by part type (manufactured, purchased, assembly, raw_material)"
    ),
    item_group: PartItemGroup = Query(
        PartItemGroup.ENGINEERING,
        description="Catalog group to list: engineering, materials, or all",
    ),
    active_only: bool = Query(True, description="Only return active parts"),
    include_bom_components: bool = Query(True, description="Include parts used as active BOM components"),
    include_deleted: bool = Query(False, description="Include soft-deleted parts (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Retrieve a list of parts with optional filtering and pagination.

    - **skip**: Number of records to skip (for pagination)
    - **limit**: Maximum number of records to return (max 500)
    - **search**: Text search across part number, name, description, and customer part number
    - **part_type**: Filter by type (manufactured, purchased, assembly, raw_material)
    - **active_only**: When true, only returns active parts (default: true)
    - **include_deleted**: Include soft-deleted parts (admin only, default: false)

    Returns parts ordered by part number.
    """
    query = (
        db.query(Part)
        .filter(Part.company_id == company_id)
        .options(selectinload(Part.bom), selectinload(Part.inventory_items))
    )

    # Filter out soft-deleted unless explicitly requested by admin
    if not (include_deleted and current_user.role == UserRole.ADMIN):
        query = query.filter(Part.is_deleted == False)

    if active_only:
        query = query.filter(Part.is_active == True)

    if item_group == PartItemGroup.ENGINEERING:
        query = query.filter(Part.part_type.in_(ENGINEERING_PART_TYPES))
    elif item_group == PartItemGroup.MATERIALS:
        from app.models.part import MATERIAL_SUPPLY_PART_TYPES

        query = query.filter(Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES))

    if part_type:
        query = query.filter(Part.part_type == part_type)

    if not include_bom_components:
        component_part_ids = (
            db.query(BOMItem.component_part_id)
            .join(BOM, BOM.id == BOMItem.bom_id)
            .filter(
                BOM.company_id == company_id,
                BOM.is_active == True,
                # Invariant 3 — a soft-deleted BOM's retained lines must not keep a part
                # classified as "a BOM component" and filter it out of the parts list.
                BOM.is_deleted == False,
            )
        )
        query = query.filter(~Part.id.in_(component_part_ids))

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Part.part_number.ilike(search_filter),
                Part.name.ilike(search_filter),
                Part.description.ilike(search_filter),
                Part.customer_part_number.ilike(search_filter),
            )
        )

    # Also filter out parts with NULL part_type to prevent serialization errors
    query = query.filter(Part.part_type.isnot(None))

    parts = query.order_by(Part.part_number).offset(skip).limit(limit).all()

    return [response for part in parts if (response := _part_to_response(part))]


@router.post("/", response_model=PartResponse, status_code=status.HTTP_201_CREATED, summary="Create a new part")
def create_part(
    part_in: PartCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Create a new part in the system.

    **Required roles**: Admin, Manager, or Supervisor

    The part number must be unique and will be automatically converted to uppercase.

    **Returns**: The created part with system-generated ID and timestamps.

    **Raises**:
    - 400: Part number already exists
    """
    if db.query(Part).filter(Part.part_number == part_in.part_number, Part.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Part number already exists")

    data = part_in.model_dump()
    # Normalize enum inputs in case clients send uppercase values or enum objects.
    part_type_val = data.get("part_type")
    if hasattr(part_type_val, "value"):
        data["part_type"] = str(part_type_val.value).strip().lower()
    elif isinstance(part_type_val, str):
        data["part_type"] = part_type_val.strip().lower()

    if not is_engineering_part_type(data.get("part_type")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Engineering parts must be manufactured or assembly. Use the materials endpoint for supplies.",
        )

    uom_val = data.get("unit_of_measure")
    if hasattr(uom_val, "value"):
        data["unit_of_measure"] = str(uom_val.value).strip().lower()
    elif isinstance(uom_val, str):
        data["unit_of_measure"] = uom_val.strip().lower()

    part = Part(**data, created_by=current_user.id)
    part.company_id = company_id
    db.add(part)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    # Audit log (before the terminal commit so it persists atomically)
    audit = AuditService(db, current_user, request)
    audit.log_create("part", part.id, part.part_number, new_values=part)

    db.commit()
    db.refresh(part)

    return part


@router.post("/import-csv", response_model=PartCsvImportResponse)
async def import_parts_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Import part master records from CSV or XLSX with row-level errors."""
    content = await file.read()
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(
            parse_import_file, file.filename, content, required_columns={"part_number", "name", "part_type"}
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _run_import() -> PartCsvImportResponse:
        existing_part_numbers = {
            (value or "").strip().upper()
            for (value,) in db.query(Part.part_number).filter(Part.company_id == company_id).all()
        }

        audit = AuditService(db, current_user, request)
        errors: List[PartCsvImportError] = []
        created_ids: List[int] = []
        total_rows = 0
        accepted_count = 0

        for row_number, row in table.iter_rows():
            total_rows += 1
            part_number = row.get("part_number", "").upper()

            try:
                if not part_number:
                    raise ValueError("part_number is required")
                if part_number in existing_part_numbers:
                    raise ValueError("Part number already exists")
                if not is_engineering_part_type(row.get("part_type", "")):
                    raise ValueError("part_type must be manufactured or assembly")

                part_in = PartCreate(
                    part_number=part_number,
                    revision=row.get("revision") or "A",
                    name=row.get("name", ""),
                    description=row.get("description") or None,
                    part_type=row.get("part_type", ""),
                    unit_of_measure=row.get("unit_of_measure") or row.get("uom") or UnitOfMeasure.EACH.value,
                    standard_cost=_parse_float(row.get("standard_cost", ""), "standard_cost"),
                    material_cost=_parse_float(row.get("material_cost", ""), "material_cost"),
                    labor_cost=_parse_float(row.get("labor_cost", ""), "labor_cost"),
                    overhead_cost=_parse_float(row.get("overhead_cost", ""), "overhead_cost"),
                    lead_time_days=_parse_int(row.get("lead_time_days", ""), "lead_time_days"),
                    safety_stock=_parse_float(row.get("safety_stock", ""), "safety_stock"),
                    reorder_point=_parse_float(row.get("reorder_point", ""), "reorder_point"),
                    reorder_quantity=_parse_float(row.get("reorder_quantity", ""), "reorder_quantity"),
                    is_critical=_parse_bool(row.get("is_critical", ""), False),
                    requires_inspection=_parse_bool(row.get("requires_inspection", ""), True),
                    inspection_requirements=row.get("inspection_requirements") or None,
                    customer_name=row.get("customer_name") or None,
                    customer_part_number=row.get("customer_part_number") or None,
                    drawing_number=row.get("drawing_number") or None,
                )
                is_active = _parse_bool(row.get("is_active", ""), True)
            except (ValueError, ValidationError) as exc:
                errors.append(PartCsvImportError(row=row_number, part_number=part_number or None, reason=str(exc)))
                continue

            if dry_run:
                existing_part_numbers.add(part_number)
                accepted_count += 1
                continue

            try:
                part = Part(**part_in.model_dump(), created_by=current_user.id)
                part.company_id = company_id
                part.is_active = is_active
                part.status = row.get("status") or "active"
                db.add(part)
                db.flush()
                audit.log_create("part", part.id, part.part_number, new_values=part, extra_data={"source": "import"})
                db.commit()
                db.refresh(part)
            except Exception as exc:
                db.rollback()
                errors.append(PartCsvImportError(row=row_number, part_number=part_number, reason=str(exc)))
                continue

            existing_part_numbers.add(part.part_number.upper())
            created_ids.append(part.id)
            accepted_count += 1

        return PartCsvImportResponse(
            imported_count=accepted_count,
            skipped_count=len(errors),
            total_rows=total_rows,
            created_ids=created_ids,
            errors=errors,
            dry_run=dry_run,
        )

    return await run_in_threadpool(_run_import)


@router.get("/generate-number", summary="Generate Werco part number for raw material or hardware")
def generate_part_number(
    description: str = Query(..., min_length=3, description="Part description"),
    part_type: PartType = Query(..., description="Part type"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    if part_type not in [PartType.RAW_MATERIAL, PartType.HARDWARE, PartType.CONSUMABLE]:
        return {"suggested_part_number": None, "existing": False}

    normalized = " ".join(normalize_description(description).lower().split())
    existing = (
        db.query(Part)
        .filter(
            Part.company_id == company_id,
            Part.part_type == part_type,
            func.lower(func.trim(Part.description)) == normalized,
        )
        .first()
    )
    if existing:
        return {"suggested_part_number": existing.part_number, "existing": True}

    suggested = generate_werco_part_number(description, part_type.value)
    return {"suggested_part_number": suggested, "existing": False}


@router.get("/by-number/{part_number}", response_model=PartResponse)
def get_part_by_number(
    part_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a part by part number"""
    part = db.query(Part).filter(Part.part_number == part_number, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.get("/{part_id}", response_model=PartResponse)
def get_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific part"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.put("/{part_id}", response_model=PartResponse)
def update_part(
    part_id: int,
    part_in: PartUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update a part.

    **Resolves ANY part in the company, engineering or material/supply** — deliberately.
    The BOM tab drills through to a component's part page, and BOM components are
    routinely ``purchased`` / ``raw_material``, so this is the edit door those pages
    reach; narrowing it to ``ENGINEERING_PART_TYPES`` would 404 every such save behind a
    "Part not found" that is not true, and would leave the Materials screen — which
    force-sends ``revision`` and ``is_critical`` on every save — as the only editor for a
    material row, quietly resetting revisions and clearing critical-characteristic flags
    (invariant 5). ``PUT /materials/{material_id}`` stays the material-shaped door; the
    two overlap on rows, and that is fine because what needed gating was never the EDIT.

    **What is gated is the CLASS CHANGE.** Reclassifying a material part that still
    carries OPEN work-order material ties **on an unfinished work order** into a produced
    one is refused **409** by the shared ``assert_part_type_change_allowed`` — the second
    half of the tie-time part-type gate, since the hazard (a tie whose part is something
    the shop produces, depleting finished goods at completion) is reachable by tying first
    and reclassifying after just as easily as by tying a produced part outright. Ties held
    by COMPLETE / CLOSED / CANCELLED work orders do **not** refuse it: they carry no live
    demand, and since nothing ever closes a tie at completion, counting them would block
    this edit forever on any part the shop has consumed. A ``part_type`` outside
    ``ENGINEERING_PART_TYPES`` is still refused **400** by this endpoint's own rule below.

    Turning ``backflush_components`` ON is refused with **409** (plain-string ``detail``:
    the blocker sentences joined) while this part's backflush readiness check reports any
    blocking diagnostic — see ``assert_backflush_change_allowed``, which is shared with
    ``PUT /materials/{material_id}`` because that endpoint writes the same rows.
    """
    # ``tenant_query`` rather than a hand-rolled ``company_id`` predicate — invariant 1's
    # helper, same rows, no behavior change.
    #
    # NO ``is_deleted == False`` FILTER, and that is the deliberate half of invariant 3's
    # "filter, or carry a comment saying why not". A soft-deleted part stays editable here
    # for two reasons. (1) The one edit on this handler that could do harm on a tombstoned
    # row — arming ``backflush_components`` — is ALREADY refused, by
    # ``assert_backflush_change_allowed``'s ``deleted_part`` diagnostic, with a 409 that
    # says so; swapping that for a 404 would replace an explanatory refusal with an opaque
    # one and lose the diagnostic on this door entirely. (2) ``PUT /materials/{id}`` writes
    # the same ``parts`` rows and has never filtered it either, so filtering one door only
    # would re-open the split-behavior gap this pair keeps closing. Every list, picker and
    # search DOES filter ``is_deleted``, so a tombstoned part cannot be SELECTED into
    # anything; what remains reachable is an audited edit to a record already withdrawn
    # from use. Changing this is a decision about the parts/materials pair as a whole, not
    # a line to tighten here in passing.
    part = tenant_query(db, Part, company_id).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(part, c.key) for c in part.__table__.columns}

    update_data = part_in.model_dump(exclude_unset=True)
    # BEFORE the first setattr: a refusal must leave the row untouched, and the readiness
    # check reads the part it is judging.
    #
    # THAT ORDER IS ONLY SAFE WHILE READINESS READS NO MUTABLE FIELD OF THE SUBJECT PART.
    # It reads ``part.id`` (immutable within a request), ``part.is_deleted``, and
    # ``part.part_number`` for message text; the unit of measure it compares is the
    # COMPONENT's, not the subject's. So a same-request "change a field that would break
    # readiness, and arm in the same PUT" is not currently constructible. If a future
    # ``PartUpdate`` field ever becomes an input to ``backflush_readiness_for_part``
    # (a subject-part UoM comparison, a part_type rule, a status rule), this call must move
    # AFTER the setattr loop and before the flush -- or the gate silently starts judging
    # the pre-request part while the request rewrites it.
    backflush_extra = assert_backflush_change_allowed(db, part, update_data, company_id=company_id)
    # Also before the first setattr: the conversion gate. It reads the part's CURRENT type
    # and this company's LIVE ties (OPEN, on a non-terminal work order), so it has to run
    # while the row still holds its old class. Refuses 409 only for material -> produced on
    # a part that live demand is still standing against; every other direction, any untied
    # part, and a part whose only ties belong to finished jobs all pass untouched.
    if "part_type" in update_data:
        assert_part_type_change_allowed(db, part, update_data["part_type"], company_id=company_id)
    for field, value in update_data.items():
        if field == "part_type":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
            if not is_engineering_part_type(value):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Engineering parts must be manufactured or assembly. Use the materials endpoint for supplies.",
                )
        if field == "unit_of_measure":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
        setattr(part, field, value)

    # Audit log (before the terminal commit so it persists atomically). ``extra_data``
    # carries the backflush readiness verdict that authorised a flag flip -- the flip
    # itself is already in the row's ``changes`` map (both sides enumerate model columns),
    # but the verdict is not reconstructable later once the BOM has been edited.
    audit.log_update(
        "part", part.id, part.part_number, old_values=old_values, new_values=part, extra_data=backflush_extra
    )

    db.commit()
    db.refresh(part)

    return part


@router.get(
    "/{part_id}/backflush-readiness",
    response_model=PartBackflushReadinessResponse,
    summary="Can this part opt into automatic BOM component backflush?",
)
def get_part_backflush_readiness(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    company_id: int = Depends(get_current_company_id),
):
    """The structured form of the refusal that ``PUT /parts/{part_id}`` would return.

    Open to any authenticated tenant user, like the other material reads: it discloses
    facts about this company's own BOM, and a UI that could not show them would be asking
    for a decision nobody could make.

    **Pure read — writes nothing.** No ledger row, no audit row, no operational event; a
    poll is not an actor and records no reason.

    ``eligible`` is a snapshot, not authorisation: BOM lines are mutable by other people,
    so the identical check re-runs server-side on the write that sets the flag.

    Only the BOM half is answerable at part scope. Routing conditions (an operation naming
    the work order's own part, two operations disagreeing on a component, routing demand
    the BOM excludes) need a work order and appear on
    ``GET /work-orders/{id}/backflush-preview``.
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    diagnostics = backflush_readiness_for_part(db, part, company_id=company_id)
    blockers = [BackflushDiagnostic.model_validate(d) for d in diagnostics if d.severity == BACKFLUSH_BLOCKING]
    advisories = [BackflushDiagnostic.model_validate(d) for d in diagnostics if d.severity != BACKFLUSH_BLOCKING]
    return PartBackflushReadinessResponse(
        part_id=part.id,
        part_number=part.part_number,
        backflush_components=bool(part.backflush_components),
        eligible=not blockers,
        blockers=blockers,
        advisories=advisories,
    )


@router.post("/{part_id}/revision")
def create_new_revision(
    part_id: int,
    new_revision: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new revision of a part (for AS9100D revision control)"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    old_revision = part.revision
    part.revision = new_revision
    db.commit()

    return {
        "message": f"Part revision updated from {old_revision} to {new_revision}",
        "part_number": part.part_number,
        "new_revision": new_revision,
    }


@router.delete("/{part_id}")
def delete_part(
    part_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete the record (admin only, use with caution)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Soft delete a part (default) or permanently delete (hard_delete=true).

    **Soft delete**: Marks the part as deleted but preserves data for recovery and audit trail.
    The part will be excluded from normal queries but can be restored.

    **Hard delete**: Permanently removes the record. Use with extreme caution.
    Only available if no dependencies exist (work orders, BOMs, etc.).
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    audit = AuditService(db, current_user, request)

    if hard_delete:
        # Check for dependencies before hard delete
        from app.models.bom import BOM, BOMItem
        from app.models.work_order import WorkOrder

        wo_count = db.query(WorkOrder).filter(WorkOrder.part_id == part_id).count()
        # DELIBERATELY blind to ``BOM.is_deleted`` (and to ``is_active``): this is a
        # referential-integrity probe for a PHYSICAL delete, not a business read. A
        # soft-deleted BOM row still exists and still holds a real foreign key to this
        # part, and ``delete_bom`` retains its lines on purpose, so hard-deleting the
        # part underneath it would orphan both. Filtering here would make the guard
        # weaker, not more correct.
        bom_count = db.query(BOM).filter(BOM.part_id == part_id).count()
        bom_item_count = db.query(BOMItem).filter(BOMItem.component_part_id == part_id).count()

        if wo_count > 0 or bom_count > 0 or bom_item_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot hard delete: Part has {wo_count} work orders, {bom_count} BOMs, {bom_item_count} BOM references",
            )

        audit.log_delete("part", part.id, part.part_number)
        db.delete(part)
        db.commit()
        return {"message": "Part permanently deleted"}

    # Soft delete
    part.soft_delete(current_user.id)
    part.is_active = False
    part.status = "obsolete"

    # Audit log (before the terminal commit so it persists atomically)
    audit.log_delete("part", part.id, part.part_number, soft_delete=True)

    db.commit()

    return {"message": "Part marked as deleted (soft delete)", "can_restore": True}


@router.post("/{part_id}/restore", summary="Restore a soft-deleted part")
def restore_part(
    part_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Restore a soft-deleted part.

    **Required roles**: Admin or Manager

    Returns the part to active status and clears deletion metadata.
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    if not part.is_deleted:
        raise HTTPException(status_code=400, detail="Part is not deleted")

    audit = AuditService(db, current_user, request)

    part.restore()
    part.is_active = True
    part.status = "active"

    # Audit log (before the terminal commit so it persists atomically)
    audit.log_update(
        "part",
        part.id,
        part.part_number,
        old_values={"is_deleted": True, "status": "obsolete"},
        new_values={"is_deleted": False, "status": "active"},
        action="restore",
    )

    db.commit()

    return {"message": "Part restored successfully", "part_id": part.id}

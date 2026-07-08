import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.bom import BOM, BOMItem
from app.models.part import Part, PartType
from app.models.process_sheet import ProcessSheet, ProcessSheetStatus
from app.models.routing import Routing, RoutingOperation
from app.models.routing_learning import RoutingGenerationSession
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.ai_learning import AICorrectionCreate, AIInteractionEventCreate
from app.schemas.routing import (
    PartSummary,
    RoutingCreate,
    RoutingListResponse,
    RoutingOperationCreate,
    RoutingOperationResponse,
    RoutingOperationUpdate,
    RoutingResponse,
    RoutingUpdate,
)
from app.schemas.routing_generation import (
    DrawingExtractionInfo,
    ProposedOperation,
    RoutingCreateFromGeneration,
    RoutingGenerationResult,
)
from app.schemas.routing_import import RoutingImportResponse
from app.services.ai_learning_service import AILearningService
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file
from app.services.prompts import ROUTING_GENERATION_PROMPT
from app.services.routing_import_service import import_routings
from app.services.routing_learning_service import (
    create_generation_session,
    get_learned_routing_context,
    learn_from_approved_generation,
)
from app.services.work_center_type_service import get_work_center_types, normalize_work_center_type

logger = logging.getLogger(__name__)

router = APIRouter()


def _routing_generation_corrections(
    generation_session: RoutingGenerationSession,
    approved_operations: List[Dict[str, Any]],
) -> List[AICorrectionCreate]:
    proposed_operations = generation_session.proposed_operations or []
    proposed_by_sequence = {operation.get("sequence"): operation for operation in proposed_operations}
    corrections: List[AICorrectionCreate] = []

    comparable_fields = [
        ("operation_name", "name"),
        ("work_center_id", "work_center_id"),
        ("work_center_type", "work_center_type"),
        ("setup_hours", "setup_hours"),
        ("run_hours_per_unit", "run_hours_per_unit"),
        ("work_instructions", "work_instructions"),
        ("is_inspection_point", "is_inspection_point"),
        ("is_outside_operation", "is_outside_operation"),
    ]

    if len(proposed_operations) != len(approved_operations):
        corrections.append(
            AICorrectionCreate(
                field_path="operations.count",
                proposed_value=len(proposed_operations),
                final_value=len(approved_operations),
                correction_reason="Human-reviewed routing changed operation count.",
            )
        )

    for index, approved in enumerate(approved_operations):
        sequence = approved.get("sequence")
        proposed = proposed_by_sequence.get(sequence) or (
            proposed_operations[index] if index < len(proposed_operations) else {}
        )
        for proposed_field, approved_field in comparable_fields:
            proposed_value = proposed.get(proposed_field)
            approved_value = approved.get(approved_field)
            if proposed_value != approved_value:
                corrections.append(
                    AICorrectionCreate(
                        field_path=f"operations.{sequence or index + 1}.{approved_field}",
                        proposed_value=proposed_value,
                        final_value=approved_value,
                        correction_reason="Human-reviewed routing approval changed this field.",
                    )
                )

    return corrections


def calculate_routing_totals(routing: Routing, db: Session):
    """Recalculate routing totals from operations"""
    total_setup = 0.0
    total_run = 0.0
    total_labor = 0.0
    total_overhead = 0.0

    for op in routing.operations:
        if not op.is_active:
            continue
        total_setup += op.setup_hours
        total_run += op.run_hours_per_unit

        # Get labor rate (override or work center rate)
        labor_rate = op.labor_rate_override
        if labor_rate is None and op.work_center:
            labor_rate = op.work_center.hourly_rate
        labor_rate = labor_rate or 0.0

        # Calculate costs
        op_labor = (op.setup_hours + op.run_hours_per_unit) * labor_rate
        op_overhead = (op.setup_hours + op.run_hours_per_unit) * op.overhead_rate

        if op.is_outside_operation:
            op_labor += op.outside_cost

        total_labor += op_labor
        total_overhead += op_overhead

    routing.total_setup_hours = total_setup
    routing.total_run_hours_per_unit = total_run
    routing.total_labor_cost = total_labor
    routing.total_overhead_cost = total_overhead


# Time-standard fields editable on a RELEASED routing operation. Structural / process-definition
# fields (work center, instructions, sequence, inspection flags) remain locked once released --
# changing them requires a new revision. The frontend contracts against the released-edit error
# message below, so keep that string stable.
TIME_STANDARD_FIELDS = {
    "setup_hours",
    "run_hours_per_unit",
    "move_hours",
    "queue_hours",
    "cycle_time_seconds",
    "pieces_per_cycle",
}


ALLOWED_DRAWING_EXTENSIONS = {".pdf", ".dxf", ".step", ".stp"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _validate_process_sheet_attach(db: Session, company_id: int, process_sheet_id: int) -> ProcessSheet:
    """Validate a process-sheet attach target: must exist in the ACTIVE company (tenant-scoped),
    not be soft-deleted (404 otherwise), and be RELEASED (409 otherwise) — only released
    inspection content may reach a traveler (docs/PROCESS_SHEETS_SCOPE.md)."""
    sheet = (
        tenant_query(db, ProcessSheet, company_id)
        .filter(ProcessSheet.id == process_sheet_id, ProcessSheet.is_deleted == False)  # noqa: E712
        .first()
    )
    if not sheet:
        raise HTTPException(status_code=404, detail="Process sheet not found")
    if sheet.status != ProcessSheetStatus.RELEASED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Only a released process sheet can be attached (sheet {sheet.sheet_number} is {sheet.status})",
        )
    return sheet


@router.post("/generate-from-drawing", response_model=RoutingGenerationResult)
async def generate_routing_from_drawing(
    file: UploadFile = File(...),
    part_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Upload a drawing (PDF, DXF, STEP) and get a proposed draft routing.
    Returns the proposed routing for user review -- does NOT create it yet.
    """
    # Validate part exists
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Validate file extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_DRAWING_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(ALLOWED_DRAWING_EXTENSIONS)}",
        )

    # Read file content
    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    # Save to temp location
    upload_dir = Path("uploads/routing_generation")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{part_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{ext}"
    file_path = upload_dir / safe_name
    file_path.write_bytes(file_content)

    warnings: List[str] = []

    # Check for existing active routing (warn but don't block)
    existing_routing_warning = None
    existing = (
        db.query(Routing)
        .filter(
            Routing.part_id == part_id,
            Routing.company_id == company_id,
            Routing.is_active == True,
        )
        .first()
    )
    if existing:
        existing_routing_warning = (
            f"Part already has an active routing (Rev {existing.revision}, status: {existing.status}). "
            "Creating a new routing will require deactivating the existing one first."
        )

    # Build work_centers_by_type lookup from this company's active work centers.
    configured_work_center_types = get_work_center_types(db, include_in_use=True, company_id=company_id)
    active_wcs = (
        db.query(WorkCenter)
        .filter(
            WorkCenter.company_id == company_id,
            WorkCenter.is_active == True,
        )
        .all()
    )
    work_centers_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for wc in active_wcs:
        wc_type = normalize_work_center_type(wc.work_center_type or "")
        if not wc_type:
            continue
        if wc_type not in work_centers_by_type:
            work_centers_by_type[wc_type] = []
        work_centers_by_type[wc_type].append({"id": wc.id, "name": wc.name, "code": wc.code})
    active_work_center_types = list(work_centers_by_type.keys())
    allowed_work_center_types = active_work_center_types or configured_work_center_types
    if not active_work_center_types:
        warnings.append(
            "No active work centers were found. Proposed operations will require manual work center assignment."
        )

    # Parse file based on type
    drawing_text = ""
    geometry = None
    is_ocr = False

    if ext == ".pdf":
        from app.services.pdf_service import extract_text_from_pdf

        result = extract_text_from_pdf(str(file_path))
        drawing_text = result.text
        is_ocr = result.is_ocr
        if not drawing_text or len(drawing_text.strip()) < 20:
            warnings.append("Very little text extracted from the PDF. The routing may be incomplete.")

    elif ext == ".dxf":
        from app.services.rfq_parsing_service import parse_dxf_geometry

        geometry = parse_dxf_geometry(str(file_path), file.filename or safe_name)
        # Build a text summary from geometry for the LLM
        parts_desc = []
        if geometry.get("cut_length"):
            parts_desc.append(f"Cut length: {geometry['cut_length']:.1f} inches")
        if geometry.get("hole_count"):
            parts_desc.append(f"Holes: {geometry['hole_count']}")
        if geometry.get("bend_count"):
            parts_desc.append(f"Bends: {geometry['bend_count']}")
        if geometry.get("flat_area"):
            parts_desc.append(f"Flat area: {geometry['flat_area']:.1f} sq inches")
        bbox = geometry.get("bbox", {})
        if bbox and bbox.get("min_x") is not None:
            w = (bbox.get("max_x", 0) or 0) - (bbox.get("min_x", 0) or 0)
            h = (bbox.get("max_y", 0) or 0) - (bbox.get("min_y", 0) or 0)
            parts_desc.append(f"Bounding box: {w:.1f} x {h:.1f} inches")
        drawing_text = f"DXF flat pattern for part {part.part_number} ({part.name}).\n" + "\n".join(parts_desc)

    elif ext in (".step", ".stp"):
        from app.services.rfq_parsing_service import parse_step_fallback

        geometry = parse_step_fallback(str(file_path), file.filename or safe_name)
        drawing_text = f"STEP file for part {part.part_number} ({part.name})."
        if geometry.get("warning"):
            warnings.append(geometry["warning"])

    # Generate the draft routing
    from app.services.routing_generation_service import generate_draft_routing

    learned_context = get_learned_routing_context(
        db,
        company_id=company_id,
        part=part,
        drawing_text=drawing_text,
        geometry=geometry,
    )

    gen_result = generate_draft_routing(
        drawing_text=drawing_text,
        geometry=geometry,
        work_centers_by_type=work_centers_by_type,
        is_ocr=is_ocr,
        work_center_types=allowed_work_center_types,
        part_context=(
            f"Part {part.part_number}: {part.name}. "
            f"ERP part type: {part.part_type.value if hasattr(part.part_type, 'value') else part.part_type}."
        ),
        is_assembly=part.part_type == PartType.ASSEMBLY,
        learned_aliases=learned_context.get("aliases"),
        learned_patterns=learned_context.get("patterns"),
        preferred_work_center_ids=learned_context.get("preferred_work_center_ids"),
        learned_examples_context=learned_context.get("examples_prompt"),
        company_id=company_id,
    )

    if gen_result.get("_error"):
        raise HTTPException(status_code=500, detail=gen_result["_error"])

    # Build response
    part_info = gen_result.get("part_info", {})
    drawing_info = DrawingExtractionInfo(
        material=part_info.get("material"),
        thickness=part_info.get("thickness"),
        finish=part_info.get("finish"),
        tolerances_noted=part_info.get("tolerances_noted", False),
        weld_required=part_info.get("weld_required", False),
        assembly_required=part_info.get("assembly_required", False),
        cut_length=geometry.get("cut_length") if geometry else None,
        hole_count=geometry.get("hole_count") if geometry else None,
        bend_count=geometry.get("bend_count") if geometry else None,
        flat_length=(
            ((geometry.get("bbox", {}).get("max_x", 0) or 0) - (geometry.get("bbox", {}).get("min_x", 0) or 0))
            if geometry and geometry.get("bbox") and geometry["bbox"].get("min_x") is not None
            else None
        ),
        flat_width=(
            ((geometry.get("bbox", {}).get("max_y", 0) or 0) - (geometry.get("bbox", {}).get("min_y", 0) or 0))
            if geometry and geometry.get("bbox") and geometry["bbox"].get("min_y") is not None
            else None
        ),
    )

    proposed_operations = [
        ProposedOperation(
            sequence=op.get("sequence", (i + 1) * 10),
            operation_name=op.get("operation_name", f"Operation {(i + 1) * 10}"),
            description=op.get("description"),
            work_center_type=op.get("work_center_type", "fabrication"),
            work_center_id=op.get("work_center_id"),
            work_center_name=op.get("work_center_name"),
            setup_hours=op.get("setup_hours", 0.0),
            run_hours_per_unit=op.get("run_hours_per_unit", 0.0),
            is_inspection_point=op.get("is_inspection_point", False),
            is_outside_operation=op.get("is_outside_operation", False),
            tooling_requirements=op.get("tooling_requirements"),
            work_instructions=op.get("work_instructions"),
            confidence=op.get("confidence", "medium"),
        )
        for i, op in enumerate(gen_result.get("operations", []))
    ]

    all_warnings = warnings + gen_result.get("warnings", [])
    drawing_info_payload = drawing_info.model_dump()
    proposed_operations_payload = [operation.model_dump() for operation in proposed_operations]
    generation_session = create_generation_session(
        db,
        company_id=company_id,
        part_id=part.id,
        created_by=current_user.id,
        file_name=file.filename or safe_name,
        file_type=ext.lstrip("."),
        file_size=len(file_content),
        file_path=str(file_path),
        drawing_text=drawing_text,
        geometry=geometry,
        drawing_info=drawing_info_payload,
        proposed_operations=proposed_operations_payload,
        warnings=all_warnings,
        extraction_confidence=gen_result.get("extraction_confidence", "medium"),
        source_was_ocr=is_ocr,
        learned_context=learned_context,
    )
    db.commit()
    db.refresh(generation_session)

    return RoutingGenerationResult(
        generation_session_id=generation_session.id,
        part_id=part.id,
        part_number=part.part_number,
        part_name=part.name,
        drawing_info=drawing_info,
        proposed_operations=proposed_operations,
        extraction_confidence=gen_result.get("extraction_confidence", "medium"),
        file_type=ext.lstrip("."),
        warnings=all_warnings,
        existing_routing_warning=existing_routing_warning,
    )


@router.post("/create-from-generation", response_model=RoutingResponse)
def create_routing_from_generation(
    data: RoutingCreateFromGeneration,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Create a routing and its operations from the reviewed/edited generation result.
    The routing is created in 'draft' status.
    """
    # Check part exists
    part = db.query(Part).filter(Part.id == data.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    generation_session = None
    if data.generation_session_id:
        generation_session = (
            db.query(RoutingGenerationSession)
            .filter(
                RoutingGenerationSession.id == data.generation_session_id,
                RoutingGenerationSession.part_id == data.part_id,
                RoutingGenerationSession.company_id == company_id,
            )
            .first()
        )
        if not generation_session:
            raise HTTPException(status_code=404, detail="Routing generation session not found")

    # Check for existing active routing
    existing = (
        db.query(Routing)
        .filter(
            Routing.part_id == data.part_id,
            Routing.company_id == company_id,
            Routing.is_active == True,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Part already has an active routing (Rev {existing.revision}). Deactivate it first or create a new revision.",
        )

    # Validate all work_center_ids exist
    for op in data.operations:
        wc = (
            db.query(WorkCenter)
            .filter(
                WorkCenter.id == op.work_center_id,
                WorkCenter.company_id == company_id,
            )
            .first()
        )
        if not wc:
            raise HTTPException(
                status_code=400,
                detail=f"Work center ID {op.work_center_id} not found (operation '{op.name}').",
            )

    # Create routing
    routing = Routing(
        part_id=data.part_id,
        revision=data.revision,
        description=data.description or f"Auto-generated from drawing for {part.part_number}",
        status="draft",
        created_by=current_user.id,
    )
    routing.company_id = company_id
    db.add(routing)
    db.flush()

    # Create operations
    approved_operations = []
    for op_data in data.operations:
        op_dict = op_data.model_dump()
        approved_operations.append(op_dict.copy())
        if not op_dict.get("operation_number"):
            op_dict["operation_number"] = f"Op {op_dict['sequence']}"
        operation = RoutingOperation(routing_id=routing.id, company_id=company_id, **op_dict)
        db.add(operation)

    db.flush()
    db.refresh(routing)
    calculate_routing_totals(routing, db)
    if generation_session:
        correction_summary = learn_from_approved_generation(
            db,
            generation_session=generation_session,
            approved_operations=approved_operations,
            part=part,
            routing_id=routing.id,
            approved_by=current_user.id,
            company_id=company_id,
        )
        ai_corrections = _routing_generation_corrections(generation_session, approved_operations)
        AILearningService(db).record_interaction(
            company_id=company_id,
            user=current_user,
            data=AIInteractionEventCreate(
                event_type="edited" if ai_corrections else "accepted",
                source_module="routing",
                ai_feature="drawing_routing_generation",
                surface="routing.create_from_generation",
                entity_type="routing",
                entity_id=routing.id,
                context_summary=f"Routing generated from drawing for part {part.part_number}.",
                event_payload={
                    "generation_session_id": generation_session.id,
                    "part_id": part.id,
                    "routing_id": routing.id,
                    "extraction_confidence": generation_session.extraction_confidence,
                    "correction_summary": correction_summary,
                    "suggest_only": True,
                },
                confidence_score=0.75 if not ai_corrections else 0.55,
                prompt_version=ROUTING_GENERATION_PROMPT.version,
                model_version=(generation_session.learned_context or {}).get("model"),
                corrections=ai_corrections,
            ),
        )
    db.commit()

    # Reload with relationships for response
    routing = (
        db.query(Routing)
        .options(
            joinedload(Routing.part),
            joinedload(Routing.operations).joinedload(RoutingOperation.work_center),
        )
        .filter(Routing.id == routing.id, Routing.company_id == company_id)
        .first()
    )

    return routing


ROUTING_IMPORT_REQUIRED_COLUMNS = {"part_number", "sequence", "operation_name"}


def _parse_routing_assignments(raw: Optional[str]) -> Dict[int, int]:
    """Parse the optional ``assignments`` multipart field.

    The field is a JSON object mapping a source file row number to a chosen
    work_center_id, e.g. ``{"2": 5, "3": 5, "4": 7}``. Keys arrive as JSON
    strings; both keys and values must be coercible to ints. A blank/missing
    field yields an empty map. Malformed input is a 400.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="assignments must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="assignments must be a JSON object mapping row -> work_center_id")
    assignments: Dict[int, int] = {}
    for key, value in decoded.items():
        # Reject JSON booleans explicitly: ``int(True)`` is 1 and ``int(False)``
        # is 0, so ``{"2": true}`` would silently become work_center_id=1 without
        # this guard. (JSON object keys are always strings, but a bool key can
        # still arrive if the payload was built oddly — reject it too.)
        if isinstance(key, bool) or isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail="assignments keys (row numbers) and values (work_center_id) must be integers",
            )
        try:
            row_number = int(key)
            work_center_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="assignments keys (row numbers) and values (work_center_id) must be integers",
            ) from exc
        assignments[row_number] = work_center_id
    return assignments


@router.post(
    "/import/preview", response_model=RoutingImportResponse, summary="Preview a routing import (CSV/XLSX, dry-run)"
)
async def import_routings_preview(
    file: UploadFile = File(...),
    assignments: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Upload a routing CSV/XLSX and preview it WITHOUT writing (dry-run, fully rolled back).

    Columns: part_number, routing_revision (default A), routing_description (optional),
    sequence (int, unique within a part), operation_name, work_center_code (OPTIONAL — a blank
    code means "assign in the UI"; a non-blank code must resolve to an active work center),
    setup_hours, run_hours_per_unit (numeric, default 0), description (optional),
    is_inspection_point, is_outside_operation (Y/N/true/false). Rows are grouped by part_number
    into one draft routing each; the part must already exist (manufactured/assembly).

    The optional ``assignments`` form field (JSON: row number -> work_center_id) carries the UI's
    chosen work centers. An assignment is authoritative for its row: it OVERRIDES the file
    ``work_center_code`` on that row (the file code is only a default that pre-fills the dropdown).
    Preview works with no assignments too — it then resolves the file code so the UI pre-fills.
    The response returns per-operation detail so the UI can render a work-center dropdown per op.
    """
    parsed_assignments = _parse_routing_assignments(assignments)
    content = await file.read()
    try:
        table = await run_in_threadpool(
            parse_import_file,
            file.filename,
            content,
            required_columns=ROUTING_IMPORT_REQUIRED_COLUMNS,
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await run_in_threadpool(
        import_routings,
        db,
        table=table,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        dry_run=True,
        assignments=parsed_assignments,
    )


@router.post("/import/commit", response_model=RoutingImportResponse, summary="Commit a routing import (CSV/XLSX)")
async def import_routings_commit(
    file: UploadFile = File(...),
    assignments: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Commit a routing CSV/XLSX import. Each part becomes one draft routing with its operations,
    created routing-by-routing (one bad routing never poisons the rest), with one audit_log
    CREATE per routing. Same columns/validation as /routing/import/preview.

    Each operation's work center is resolved with the UI choice authoritative: an ``assignments``
    entry for that row (JSON: row number -> work_center_id) wins and OVERRIDES the file
    ``work_center_code``; otherwise a non-blank file ``work_center_code`` is used. A routing with ANY
    operation still missing a work center is reported in ``errors`` and is NOT created."""
    parsed_assignments = _parse_routing_assignments(assignments)
    content = await file.read()
    try:
        table = await run_in_threadpool(
            parse_import_file,
            file.filename,
            content,
            required_columns=ROUTING_IMPORT_REQUIRED_COLUMNS,
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await run_in_threadpool(
        import_routings,
        db,
        table=table,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        dry_run=False,
        assignments=parsed_assignments,
    )


@router.get("/", response_model=List[RoutingListResponse])
def list_routings(
    skip: int = 0,
    limit: int = 100,
    part_id: Optional[int] = None,
    status: Optional[str] = None,
    active_only: bool = True,
    include_bom_components: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all routings with optional filtering"""
    query = (
        db.query(Routing)
        .filter(Routing.company_id == company_id)
        .options(joinedload(Routing.part), joinedload(Routing.operations))
    )

    if active_only:
        query = query.filter(Routing.is_active == True)

    if part_id:
        query = query.filter(Routing.part_id == part_id)
    elif not include_bom_components:
        component_part_ids = (
            db.query(BOMItem.component_part_id)
            .join(BOM, BOM.id == BOMItem.bom_id)
            .filter(
                BOM.company_id == company_id,
                BOM.is_active == True,
            )
        )
        query = query.filter(~Routing.part_id.in_(component_part_ids))

    if status:
        query = query.filter(Routing.status == status)

    routings = query.order_by(Routing.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for r in routings:
        result.append(
            RoutingListResponse(
                id=r.id,
                part_id=r.part_id,
                part=(
                    PartSummary(
                        id=r.part.id, part_number=r.part.part_number, name=r.part.name, part_type=r.part.part_type.value
                    )
                    if r.part
                    else None
                ),
                revision=r.revision,
                status=r.status,
                is_active=r.is_active,
                total_setup_hours=r.total_setup_hours,
                total_run_hours_per_unit=r.total_run_hours_per_unit,
                operation_count=len([op for op in r.operations if op.is_active]),
                created_at=r.created_at,
            )
        )

    return result


@router.post("/", response_model=RoutingResponse)
def create_routing(
    routing_in: RoutingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new routing for a part"""
    # Check part exists
    part = db.query(Part).filter(Part.id == routing_in.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Check for existing active routing
    existing = (
        db.query(Routing)
        .filter(Routing.part_id == routing_in.part_id, Routing.company_id == company_id, Routing.is_active == True)
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Part already has an active routing (Rev {existing.revision}). Deactivate it first or create a new revision.",
        )

    routing = Routing(**routing_in.model_dump(), created_by=current_user.id)
    routing.company_id = company_id
    db.add(routing)
    db.commit()
    db.refresh(routing)

    return routing


@router.get("/{routing_id}", response_model=RoutingResponse)
def get_routing(
    routing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get routing details with operations"""
    routing = (
        db.query(Routing)
        .options(joinedload(Routing.part), joinedload(Routing.operations).joinedload(RoutingOperation.work_center))
        .filter(Routing.id == routing_id, Routing.company_id == company_id)
        .first()
    )

    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    return routing


@router.get("/by-part/{part_id}", response_model=Optional[RoutingResponse])
def get_routing_by_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get the active routing for a part"""
    routing = (
        db.query(Routing)
        .options(joinedload(Routing.part), joinedload(Routing.operations).joinedload(RoutingOperation.work_center))
        .filter(Routing.part_id == part_id, Routing.company_id == company_id, Routing.is_active == True)
        .first()
    )

    return routing


@router.put("/{routing_id}", response_model=RoutingResponse)
def update_routing(
    routing_id: int,
    routing_in: RoutingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update routing details"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    update_data = routing_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(routing, field, value)

    db.commit()
    db.refresh(routing)

    return (
        db.query(Routing)
        .options(joinedload(Routing.part), joinedload(Routing.operations).joinedload(RoutingOperation.work_center))
        .filter(Routing.id == routing_id, Routing.company_id == company_id)
        .first()
    )


@router.post("/{routing_id}/release")
def release_routing(
    routing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Release a routing for production use"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Routing is already released")

    if not routing.operations:
        raise HTTPException(status_code=400, detail="Cannot release routing with no operations")

    routing.status = "released"
    routing.effective_date = datetime.utcnow()
    routing.approved_by = current_user.id
    routing.approved_at = datetime.utcnow()

    audit.log_status_change(
        "routing",
        routing.id,
        routing.revision or str(routing.id),
        old_status="draft",
        new_status="released",
    )

    db.commit()

    return {"message": "Routing released", "routing_id": routing_id}


@router.delete("/{routing_id}")
def delete_routing(
    routing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Delete a routing - hard delete for draft, soft delete for released"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "draft":
        # Hard delete draft routings
        for op in routing.operations:
            db.delete(op)
        db.delete(routing)
        db.commit()
        return {"message": "Routing deleted"}
    else:
        # Soft delete released/obsolete routings
        routing.is_active = False
        routing.status = "obsolete"
        routing.obsolete_date = datetime.utcnow()
        db.commit()
        return {"message": "Routing deactivated"}


# Operation endpoints
@router.post("/{routing_id}/operations", response_model=RoutingOperationResponse)
def add_operation(
    routing_id: int,
    operation_in: RoutingOperationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add an operation to a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    # Verify work center exists
    work_center = (
        db.query(WorkCenter)
        .filter(
            WorkCenter.id == operation_in.work_center_id,
            WorkCenter.company_id == company_id,
        )
        .first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")

    # Verify the attached process sheet (same company, not deleted, RELEASED) if provided
    if operation_in.process_sheet_id is not None:
        _validate_process_sheet_attach(db, company_id, operation_in.process_sheet_id)

    # Auto-generate operation number if not provided
    op_data = operation_in.model_dump()
    if not op_data.get('operation_number'):
        op_data['operation_number'] = f"Op {operation_in.sequence}"

    operation = RoutingOperation(routing_id=routing_id, company_id=company_id, **op_data)
    db.add(operation)

    # Recalculate totals
    db.flush()
    db.refresh(routing)
    calculate_routing_totals(routing, db)

    # Audit BEFORE the terminal commit so the CREATE row commits atomically with the
    # operation — AuditService.log() only flushes, and the request session never commits
    # on teardown, so an audit call placed after db.commit() opens a new transaction that
    # get_db teardown rolls back (the row would be silently discarded). The flush above
    # already assigned the operation's PK.
    audit.log_create(
        "routing_operation",
        operation.id,
        operation.operation_number or operation.name or str(operation.id),
        new_values=operation,
    )

    db.commit()
    db.refresh(operation)

    # Load work center for response
    operation = (
        db.query(RoutingOperation)
        .options(joinedload(RoutingOperation.work_center))
        .filter(RoutingOperation.id == operation.id)
        .first()
    )

    return operation


@router.put("/{routing_id}/operations/{operation_id}", response_model=RoutingOperationResponse)
def update_operation(
    routing_id: int,
    operation_id: int,
    operation_in: RoutingOperationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an operation.

    On a DRAFT routing every field is editable (Admin/Manager/Supervisor). On a RELEASED routing
    only time standards (setup/run/move/queue/cycle time, pieces per cycle) may be edited, and only
    by Admin/Manager (a Supervisor on the released-edit path gets 403) -- changing the process
    definition (work center, instructions, sequence, inspection flags) requires a new revision.
    A successful released time-standard edit re-stamps the routing's approval signature
    (approved_by/approved_at); draft edits do not. An OBSOLETE routing is fully locked. Every
    applied change is audit-logged.
    """
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    operation = (
        db.query(RoutingOperation)
        .filter(RoutingOperation.id == operation_id, RoutingOperation.routing_id == routing_id)
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    update_data = operation_in.model_dump(exclude_unset=True)

    # Only fields present in the payload AND different from the current value count as changes.
    changed_fields = {f for f, v in update_data.items() if getattr(operation, f) != v}

    # Status gate -- evaluate BEFORE mutating the operation.
    if routing.status == "obsolete":
        raise HTTPException(status_code=400, detail="Cannot modify an obsolete routing")
    if routing.status == "released":
        # Editing time standards on a RELEASED routing is release-adjacent authority
        # (it changes the live production content). Routing Release is Admin/Manager only
        # (Supervisor excluded) per docs/RBAC_PERMISSIONS.md, so gate the released-edit path to
        # the same set even though the decorator-level require_role admits SUPERVISOR for drafts.
        # Platform-admin / superuser bypass mirrors require_role's own escalation behavior so the
        # released-edit path is no stricter than the /release endpoint it shadows.
        privileged = current_user.is_superuser or current_user.role == UserRole.PLATFORM_ADMIN
        if not privileged and current_user.role not in (UserRole.ADMIN, UserRole.MANAGER):
            raise HTTPException(
                status_code=403,
                detail="Editing a released routing's time standards requires the Admin or Manager role.",
            )
        non_time = changed_fields - TIME_STANDARD_FIELDS
        if non_time:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Released routing: only time standards (setup, run/unit, move, queue, cycle) "
                    "can be edited — create a new revision to change the process."
                ),
            )
    # draft routings proceed with all fields applied.

    # Snapshot old values for the changed fields before mutating, for the audit trail.
    old_values = {f: getattr(operation, f) for f in changed_fields}

    # Verify work center if changing (only reachable on draft -- released would have 400'd above).
    if "work_center_id" in update_data:
        work_center = (
            db.query(WorkCenter)
            .filter(
                WorkCenter.id == update_data["work_center_id"],
                WorkCenter.company_id == company_id,
            )
            .first()
        )
        if not work_center:
            raise HTTPException(status_code=404, detail="Work center not found")

    # Verify the process sheet only when it actually CHANGES (a change is only reachable on
    # draft — process_sheet_id is a structural field, so a released routing would have 400'd
    # above). An unchanged echo of the current value in a full-payload PUT must NOT re-validate:
    # the attached sheet may have been obsoleted or soft-deleted since attach, and e.g. a
    # released-routing time-standards edit legitimately echoes it. Explicit null detaches.
    new_process_sheet_id = update_data.get("process_sheet_id")
    if new_process_sheet_id is not None and new_process_sheet_id != operation.process_sheet_id:
        _validate_process_sheet_attach(db, company_id, new_process_sheet_id)

    for field, value in update_data.items():
        setattr(operation, field, value)

    # Recalculate totals
    calculate_routing_totals(routing, db)

    if changed_fields:
        audit.log_update(
            "routing_operation",
            operation.id,
            operation.operation_number or operation.name or str(operation.id),
            old_values=old_values,
            new_values={f: update_data[f] for f in changed_fields},
        )

        # Re-stamp the routing's approval signature when live (released) content is edited in
        # place, so it reflects who last changed the production time standards. The original
        # release date (effective_date) and the revision letter stay put -- this is an in-place
        # edit, not a new revision. Draft edits do NOT re-stamp (the routing is not yet approved).
        if routing.status == "released":
            routing.approved_by = current_user.id
            routing.approved_at = datetime.utcnow()

    db.commit()

    operation = (
        db.query(RoutingOperation)
        .options(joinedload(RoutingOperation.work_center))
        .filter(RoutingOperation.id == operation_id)
        .first()
    )

    return operation


@router.delete("/{routing_id}/operations/{operation_id}")
def delete_operation(
    routing_id: int,
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete an operation from a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    operation = (
        db.query(RoutingOperation)
        .filter(RoutingOperation.id == operation_id, RoutingOperation.routing_id == routing_id)
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # Audit the hard delete (RoutingOperation has no SoftDeleteMixin) before removing the row.
    # log_delete serializes the model immediately, so passing the live instance captures its
    # full old state while it is still attached.
    op_identifier = operation.operation_number or operation.name or str(operation.id)
    audit.log_delete("routing_operation", operation.id, op_identifier, old_values=operation, soft_delete=False)

    db.delete(operation)

    # Recalculate totals
    calculate_routing_totals(routing, db)

    db.commit()

    return {"message": "Operation deleted"}


@router.post("/{routing_id}/operations/reorder")
def reorder_operations(
    routing_id: int,
    operation_order: List[dict],  # [{"id": 1, "sequence": 10}, {"id": 2, "sequence": 20}]
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Reorder operations in a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    for item in operation_order:
        operation = (
            db.query(RoutingOperation)
            .filter(RoutingOperation.id == item["id"], RoutingOperation.routing_id == routing_id)
            .first()
        )
        if operation:
            operation.sequence = item["sequence"]
            operation.operation_number = f"Op {item['sequence']}"

    db.commit()

    return {"message": "Operations reordered"}


@router.post("/{routing_id}/copy")
def copy_routing(
    routing_id: int,
    target_part_id: int,
    new_revision: str = "A",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Copy a routing to another part or create new revision"""
    source = (
        db.query(Routing)
        .options(joinedload(Routing.operations))
        .filter(Routing.id == routing_id, Routing.company_id == company_id)
        .first()
    )

    if not source:
        raise HTTPException(status_code=404, detail="Source routing not found")

    # Check target part exists
    target_part = db.query(Part).filter(Part.id == target_part_id, Part.company_id == company_id).first()
    if not target_part:
        raise HTTPException(status_code=404, detail="Target part not found")

    # Create new routing
    new_routing = Routing(
        part_id=target_part_id,
        revision=new_revision,
        description=source.description,
        status="draft",
        created_by=current_user.id,
    )
    new_routing.company_id = company_id
    db.add(new_routing)
    db.flush()

    # Copy operations
    for op in source.operations:
        new_op = RoutingOperation(
            routing_id=new_routing.id,
            company_id=company_id,
            sequence=op.sequence,
            operation_number=op.operation_number,
            name=op.name,
            description=op.description,
            work_center_id=op.work_center_id,
            setup_hours=op.setup_hours,
            run_hours_per_unit=op.run_hours_per_unit,
            move_hours=op.move_hours,
            queue_hours=op.queue_hours,
            cycle_time_seconds=op.cycle_time_seconds,
            pieces_per_cycle=op.pieces_per_cycle,
            labor_rate_override=op.labor_rate_override,
            overhead_rate=op.overhead_rate,
            is_inspection_point=op.is_inspection_point,
            inspection_instructions=op.inspection_instructions,
            work_instructions=op.work_instructions,
            setup_instructions=op.setup_instructions,
            tooling_requirements=op.tooling_requirements,
            fixture_requirements=op.fixture_requirements,
            is_outside_operation=op.is_outside_operation,
            vendor_id=op.vendor_id,
            outside_cost=op.outside_cost,
            outside_lead_days=op.outside_lead_days,
            process_sheet_id=op.process_sheet_id,
        )
        db.add(new_op)

    # Calculate totals
    db.flush()
    db.refresh(new_routing)
    calculate_routing_totals(new_routing, db)

    audit.log_create(
        "routing",
        new_routing.id,
        target_part.part_number,
        new_values=new_routing,
        extra_data={"copied_from": routing_id},
    )

    db.commit()

    return {"message": "Routing copied", "new_routing_id": new_routing.id}

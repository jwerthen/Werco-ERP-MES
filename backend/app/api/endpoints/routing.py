import os
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.api.deps import get_current_user, get_current_company_id, require_role
from app.models.user import User, UserRole
from app.models.routing import Routing, RoutingOperation
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.schemas.routing import (
    RoutingCreate, RoutingUpdate, RoutingResponse, RoutingListResponse,
    RoutingOperationCreate, RoutingOperationUpdate, RoutingOperationResponse,
    PartSummary, WorkCenterSummary
)
from app.schemas.routing_generation import (
    RoutingGenerationResult, RoutingCreateFromGeneration,
    DrawingExtractionInfo, ProposedOperation,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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


ALLOWED_DRAWING_EXTENSIONS = {".pdf", ".dxf", ".step", ".stp"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


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
    existing = db.query(Routing).filter(
        Routing.part_id == part_id, Routing.is_active == True
    ).first()
    if existing:
        existing_routing_warning = (
            f"Part already has an active routing (Rev {existing.revision}, status: {existing.status}). "
            "Creating a new routing will require deactivating the existing one first."
        )

    # Build work_centers_by_type lookup from active work centers
    active_wcs = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    work_centers_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for wc in active_wcs:
        wc_type = wc.work_center_type
        if wc_type not in work_centers_by_type:
            work_centers_by_type[wc_type] = []
        work_centers_by_type[wc_type].append({"id": wc.id, "name": wc.name, "code": wc.code})

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
        drawing_text = (
            f"DXF flat pattern for part {part.part_number} ({part.name}).\n"
            + "\n".join(parts_desc)
        )

    elif ext in (".step", ".stp"):
        from app.services.rfq_parsing_service import parse_step_fallback
        geometry = parse_step_fallback(str(file_path), file.filename or safe_name)
        drawing_text = (
            f"STEP file for part {part.part_number} ({part.name})."
        )
        if geometry.get("warning"):
            warnings.append(geometry["warning"])

    # Generate the draft routing
    from app.services.routing_generation_service import generate_draft_routing
    gen_result = generate_draft_routing(
        drawing_text=drawing_text,
        geometry=geometry,
        work_centers_by_type=work_centers_by_type,
        is_ocr=is_ocr,
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

    return RoutingGenerationResult(
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

    # Check for existing active routing
    existing = db.query(Routing).filter(
        Routing.part_id == data.part_id, Routing.is_active == True
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Part already has an active routing (Rev {existing.revision}). Deactivate it first or create a new revision.",
        )

    # Validate all work_center_ids exist
    for op in data.operations:
        wc = db.query(WorkCenter).filter(WorkCenter.id == op.work_center_id).first()
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
    for op_data in data.operations:
        op_dict = op_data.model_dump()
        if not op_dict.get("operation_number"):
            op_dict["operation_number"] = f"Op {op_dict['sequence']}"
        operation = RoutingOperation(routing_id=routing.id, **op_dict)
        db.add(operation)

    db.flush()
    db.refresh(routing)
    calculate_routing_totals(routing, db)
    db.commit()

    # Reload with relationships for response
    routing = (
        db.query(Routing)
        .options(
            joinedload(Routing.part),
            joinedload(Routing.operations).joinedload(RoutingOperation.work_center),
        )
        .filter(Routing.id == routing.id)
        .first()
    )

    return routing


@router.get("/", response_model=List[RoutingListResponse])
def list_routings(
    skip: int = 0,
    limit: int = 100,
    part_id: Optional[int] = None,
    status: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """List all routings with optional filtering"""
    query = db.query(Routing).filter(Routing.company_id == company_id).options(joinedload(Routing.part), joinedload(Routing.operations))
    
    if active_only:
        query = query.filter(Routing.is_active == True)
    
    if part_id:
        query = query.filter(Routing.part_id == part_id)
    
    if status:
        query = query.filter(Routing.status == status)
    
    routings = query.order_by(Routing.created_at.desc()).offset(skip).limit(limit).all()
    
    result = []
    for r in routings:
        result.append(RoutingListResponse(
            id=r.id,
            part_id=r.part_id,
            part=PartSummary(
                id=r.part.id,
                part_number=r.part.part_number,
                name=r.part.name,
                part_type=r.part.part_type.value
            ) if r.part else None,
            revision=r.revision,
            status=r.status,
            is_active=r.is_active,
            total_setup_hours=r.total_setup_hours,
            total_run_hours_per_unit=r.total_run_hours_per_unit,
            operation_count=len([op for op in r.operations if op.is_active]),
            created_at=r.created_at
        ))
    
    return result


@router.post("/", response_model=RoutingResponse)
def create_routing(
    routing_in: RoutingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id)
):
    """Create a new routing for a part"""
    # Check part exists
    part = db.query(Part).filter(Part.id == routing_in.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Check for existing active routing
    existing = db.query(Routing).filter(
        Routing.part_id == routing_in.part_id,
        Routing.is_active == True
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Part already has an active routing (Rev {existing.revision}). Deactivate it first or create a new revision."
        )
    
    routing = Routing(
        **routing_in.model_dump(),
        created_by=current_user.id
    )
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
    company_id: int = Depends(get_current_company_id)
):
    """Get routing details with operations"""
    routing = db.query(Routing).options(
        joinedload(Routing.part),
        joinedload(Routing.operations).joinedload(RoutingOperation.work_center)
    ).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")
    
    return routing


@router.get("/by-part/{part_id}", response_model=Optional[RoutingResponse])
def get_routing_by_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """Get the active routing for a part"""
    routing = db.query(Routing).options(
        joinedload(Routing.part),
        joinedload(Routing.operations).joinedload(RoutingOperation.work_center)
    ).filter(
        Routing.part_id == part_id,
        Routing.company_id == company_id,
        Routing.is_active == True
    ).first()
    
    return routing


@router.put("/{routing_id}", response_model=RoutingResponse)
def update_routing(
    routing_id: int,
    routing_in: RoutingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id)
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
    
    return db.query(Routing).options(
        joinedload(Routing.part),
        joinedload(Routing.operations).joinedload(RoutingOperation.work_center)
    ).filter(Routing.id == routing_id).first()


@router.post("/{routing_id}/release")
def release_routing(
    routing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id)
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
    
    db.commit()
    
    return {"message": "Routing released", "routing_id": routing_id}


@router.delete("/{routing_id}")
def delete_routing(
    routing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id)
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
    company_id: int = Depends(get_current_company_id)
):
    """Add an operation to a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    # Verify work center exists
    work_center = db.query(WorkCenter).filter(WorkCenter.id == operation_in.work_center_id).first()
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")
    
    # Auto-generate operation number if not provided
    op_data = operation_in.model_dump()
    if not op_data.get('operation_number'):
        op_data['operation_number'] = f"Op {operation_in.sequence}"
    
    operation = RoutingOperation(
        routing_id=routing_id,
        **op_data
    )
    db.add(operation)
    
    # Recalculate totals
    db.flush()
    db.refresh(routing)
    calculate_routing_totals(routing, db)
    
    db.commit()
    db.refresh(operation)
    
    # Load work center for response
    operation = db.query(RoutingOperation).options(
        joinedload(RoutingOperation.work_center)
    ).filter(RoutingOperation.id == operation.id).first()
    
    return operation


@router.put("/{routing_id}/operations/{operation_id}", response_model=RoutingOperationResponse)
def update_operation(
    routing_id: int,
    operation_id: int,
    operation_in: RoutingOperationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id)
):
    """Update an operation"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    operation = db.query(RoutingOperation).filter(
        RoutingOperation.id == operation_id,
        RoutingOperation.routing_id == routing_id
    ).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    update_data = operation_in.model_dump(exclude_unset=True)
    
    # Verify work center if changing
    if "work_center_id" in update_data:
        work_center = db.query(WorkCenter).filter(WorkCenter.id == update_data["work_center_id"]).first()
        if not work_center:
            raise HTTPException(status_code=404, detail="Work center not found")
    
    for field, value in update_data.items():
        setattr(operation, field, value)
    
    # Recalculate totals
    calculate_routing_totals(routing, db)
    
    db.commit()
    
    operation = db.query(RoutingOperation).options(
        joinedload(RoutingOperation.work_center)
    ).filter(RoutingOperation.id == operation_id).first()
    
    return operation


@router.delete("/{routing_id}/operations/{operation_id}")
def delete_operation(
    routing_id: int,
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id)
):
    """Delete an operation from a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")

    operation = db.query(RoutingOperation).filter(
        RoutingOperation.id == operation_id,
        RoutingOperation.routing_id == routing_id
    ).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
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
    company_id: int = Depends(get_current_company_id)
):
    """Reorder operations in a routing"""
    routing = db.query(Routing).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    if not routing:
        raise HTTPException(status_code=404, detail="Routing not found")

    if routing.status == "released":
        raise HTTPException(status_code=400, detail="Cannot modify released routing")
    
    for item in operation_order:
        operation = db.query(RoutingOperation).filter(
            RoutingOperation.id == item["id"],
            RoutingOperation.routing_id == routing_id
        ).first()
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
    company_id: int = Depends(get_current_company_id)
):
    """Copy a routing to another part or create new revision"""
    source = db.query(Routing).options(
        joinedload(Routing.operations)
    ).filter(Routing.id == routing_id, Routing.company_id == company_id).first()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source routing not found")
    
    # Check target part exists
    target_part = db.query(Part).filter(Part.id == target_part_id).first()
    if not target_part:
        raise HTTPException(status_code=404, detail="Target part not found")
    
    # Create new routing
    new_routing = Routing(
        part_id=target_part_id,
        revision=new_revision,
        description=source.description,
        status="draft",
        created_by=current_user.id
    )
    new_routing.company_id = company_id
    db.add(new_routing)
    db.flush()
    
    # Copy operations
    for op in source.operations:
        new_op = RoutingOperation(
            routing_id=new_routing.id,
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
            outside_lead_days=op.outside_lead_days
        )
        db.add(new_op)
    
    # Calculate totals
    db.flush()
    db.refresh(new_routing)
    calculate_routing_totals(new_routing, db)
    
    db.commit()
    
    return {"message": "Routing copied", "new_routing_id": new_routing.id}

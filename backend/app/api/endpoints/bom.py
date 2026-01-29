from typing import List, Optional, Set, Dict, Any, Tuple
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File, Form
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.bom import BOM, BOMItem, BOMItemType, BOMLineType
from app.models.part import Part, PartType, UnitOfMeasure
from app.schemas.bom import (
    BOMCreate, BOMUpdate, BOMResponse,
    BOMItemCreate, BOMItemUpdate, BOMItemResponse,
    BOMExploded, BOMItemWithChildren, BOMFlattened, BOMFlatItem,
    ComponentPartInfo, PartInfo
)
from app.schemas.bom_import import (
    BOMImportResponse,
    BOMImportPreviewResponse,
    BOMImportCommitRequest,
    BOMImportAssembly,
    BOMImportItem,
)
from app.services.pdf_service import extract_text_from_document, save_uploaded_document, SUPPORTED_EXTENSIONS
from app.services.llm_service import extract_bom_data_with_llm
from app.services.part_number_service import generate_werco_part_number

router = APIRouter()


def get_component_part_info(part: Part, db: Session) -> ComponentPartInfo:
    """Build component part info with has_bom flag - handles NULL values defensively"""
    has_bom = db.query(BOM).filter(BOM.part_id == part.id, BOM.is_active == True).first() is not None
    return ComponentPartInfo(
        id=part.id,
        part_number=part.part_number or "",
        name=part.name or "",
        revision=part.revision or "A",
        part_type=part.part_type.value if part.part_type else "manufactured",
        has_bom=has_bom
    )


def build_bom_item_response(
    item: BOMItem,
    db: Session,
    has_bom_by_part_id: Optional[dict] = None
) -> BOMItemResponse:
    """Build BOM item response with part info - handles NULL values defensively"""
    # Handle component_part safely - it might be None if the part was deleted
    component_info = None
    if item.component_part:
        try:
            if has_bom_by_part_id is not None:
                component_info = ComponentPartInfo(
                    id=item.component_part.id,
                    part_number=item.component_part.part_number or "",
                    name=item.component_part.name or "",
                    revision=item.component_part.revision or "A",
                    part_type=item.component_part.part_type.value if item.component_part.part_type else "manufactured",
                    has_bom=has_bom_by_part_id.get(item.component_part.id, False)
                )
            else:
                component_info = get_component_part_info(item.component_part, db)
        except Exception:
            pass  # Silently handle any errors getting component info
    
    return BOMItemResponse(
        id=item.id,
        bom_id=item.bom_id,
        component_part_id=item.component_part_id,
        item_number=item.item_number if item.item_number is not None else 10,
        quantity=item.quantity if item.quantity is not None else 1.0,
        item_type=item.item_type if item.item_type else BOMItemType.MAKE,
        line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
        unit_of_measure=item.unit_of_measure or "each",
        reference_designator=item.reference_designator,
        find_number=item.find_number,
        notes=item.notes,
        torque_spec=item.torque_spec,
        installation_notes=item.installation_notes,
        work_center_id=item.work_center_id,
        operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
        scrap_factor=item.scrap_factor if item.scrap_factor is not None else 0.0,
        lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
        is_optional=item.is_optional if item.is_optional is not None else False,
        is_alternate=item.is_alternate if item.is_alternate is not None else False,
        alternate_group=item.alternate_group,
        component_part=component_info,
        created_at=item.created_at,
        updated_at=item.updated_at
    )


def _normalize_uom(value: Optional[str]) -> str:
    if not value:
        return UnitOfMeasure.EACH.value
    val = value.strip().lower()
    mapping = {
        "ea": "each",
        "each": "each",
        "pcs": "each",
        "pc": "each",
        "lb": "pounds",
        "lbs": "pounds",
        "pound": "pounds",
        "ft": "feet",
        "feet": "feet",
        "in": "inches",
        "inch": "inches",
        "inches": "inches",
        "gal": "gallons",
        "gallon": "gallons",
        "l": "liters",
        "liter": "liters"
    }
    return mapping.get(val, val)


def _coerce_item_type(value: Optional[str]) -> str:
    if not value:
        return BOMItemType.BUY.value
    val = value.strip().lower()
    if val in {BOMItemType.MAKE.value, BOMItemType.BUY.value, BOMItemType.PHANTOM.value}:
        return val
    return BOMItemType.BUY.value


def _coerce_line_type(value: Optional[str]) -> str:
    if not value:
        return BOMLineType.COMPONENT.value
    val = value.strip().lower()
    if val in {
        BOMLineType.COMPONENT.value,
        BOMLineType.HARDWARE.value,
        BOMLineType.CONSUMABLE.value,
        BOMLineType.REFERENCE.value
    }:
        return val
    return BOMLineType.COMPONENT.value


def _classify_line_type(description: str, explicit: Optional[str]) -> str:
    if explicit:
        return _coerce_line_type(explicit)
    text = (description or "").lower()
    hardware_keywords = ["bolt", "screw", "washer", "nut", "fastener", "pin", "rivet", "clip", "stud", "standoff", "spacer"]
    consumable_keywords = ["adhesive", "loctite", "glue", "epoxy", "tape", "oil", "grease", "lubricant", "paint", "primer", "sealant"]
    reference_keywords = ["reference", "ref only", "for reference", "ref."]
    if any(k in text for k in hardware_keywords):
        return BOMLineType.HARDWARE.value
    if any(k in text for k in consumable_keywords):
        return BOMLineType.CONSUMABLE.value
    if any(k in text for k in reference_keywords):
        return BOMLineType.REFERENCE.value
    return BOMLineType.COMPONENT.value


def _infer_part_type(line_type: str, item_type: str, description: str) -> str:
    if line_type == BOMLineType.HARDWARE.value:
        return PartType.HARDWARE.value
    if line_type == BOMLineType.CONSUMABLE.value:
        return PartType.CONSUMABLE.value
    if line_type == BOMLineType.REFERENCE.value:
        return PartType.PURCHASED.value
    text = (description or "").lower()
    if item_type == BOMItemType.MAKE.value:
        if "assembly" in text or "assy" in text:
            return PartType.ASSEMBLY.value
        return PartType.MANUFACTURED.value
    if item_type == BOMItemType.PHANTOM.value:
        return PartType.ASSEMBLY.value
    return PartType.PURCHASED.value


def _safe_part_number(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip()


def _generate_fallback_part_number(prefix: str, index: int) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{timestamp}-{index:03d}"


def _ensure_part(
    db: Session,
    part_number: Optional[str],
    name: str,
    description: str,
    part_type: str,
    drawing_number: Optional[str],
    unit_of_measure: Optional[str],
    create_missing: bool,
    fallback_index: int
) -> Tuple[Optional[Part], Optional[str], bool]:
    if part_number:
        existing = db.query(Part).filter(Part.part_number == part_number).first()
        if existing:
            return existing, None, False
    if not create_missing:
        return None, part_number or name, False

    normalized_type = part_type if part_type in {p.value for p in PartType} else PartType.PURCHASED.value
    candidate_number = part_number
    if not candidate_number:
        if normalized_type in {PartType.RAW_MATERIAL.value, PartType.HARDWARE.value, PartType.CONSUMABLE.value}:
            candidate_number = generate_werco_part_number(description or name, normalized_type)
        if not candidate_number:
            candidate_number = _generate_fallback_part_number("AUTO", fallback_index)

    part = Part(
        part_number=candidate_number,
        revision="A",
        name=name or candidate_number,
        description=description,
        part_type=normalized_type,
        unit_of_measure=_normalize_uom(unit_of_measure),
        drawing_number=drawing_number
    )
    db.add(part)
    db.flush()
    return part, None, True


def _build_preview(extracted: Dict[str, Any]) -> Tuple[BOMImportAssembly, List[BOMImportItem], List[str], str]:
    warnings: List[str] = []
    items: List[BOMImportItem] = []
    assembly_data = extracted.get("assembly", {}) or {}
    assembly = BOMImportAssembly(
        part_number=_safe_part_number(assembly_data.get("part_number")) or _safe_part_number(assembly_data.get("drawing_number")),
        name=assembly_data.get("name"),
        revision=assembly_data.get("revision") or "A",
        description=assembly_data.get("description"),
        drawing_number=_safe_part_number(assembly_data.get("drawing_number")),
        part_type=assembly_data.get("part_type"),
    )

    for idx, item in enumerate(extracted.get("items", []) or [], start=1):
        line_number = int(item.get("line_number") or (idx * 10))
        description = (item.get("description") or "").strip()
        part_number = _safe_part_number(item.get("part_number"))
        line_type = _classify_line_type(description, item.get("line_type"))
        item_type = _coerce_item_type(item.get("item_type"))
        if not part_number:
            warnings.append(f"Line {line_number}: missing part number; will be generated if created.")
        if not description:
            warnings.append(f"Line {line_number}: missing description.")
        quantity = item.get("quantity")
        if quantity is None or float(quantity) <= 0:
            warnings.append(f"Line {line_number}: quantity not found or invalid; defaulting to 1.")
        items.append(BOMImportItem(
            line_number=line_number,
            part_number=part_number,
            description=description,
            quantity=float(quantity) if quantity and float(quantity) > 0 else 1.0,
            unit_of_measure=_normalize_uom(item.get("unit_of_measure")),
            item_type=item_type,
            line_type=line_type,
            reference_designator=item.get("reference_designator"),
            find_number=item.get("find_number"),
            notes=item.get("notes"),
        ))

    extraction_confidence = extracted.get("extraction_confidence", "low")
    return assembly, items, warnings, extraction_confidence


def _create_from_import_payload(
    payload: BOMImportCommitRequest,
    db: Session,
    current_user: User
) -> BOMImportResponse:
    items = payload.items or []
    doc_type = (payload.document_type or ("bom" if items else "part")).lower()
    if items and doc_type != "bom":
        doc_type = "bom"

    warnings: List[str] = []
    missing_parts: List[str] = []

    assembly = payload.assembly
    assembly_number = _safe_part_number(assembly.part_number) or _safe_part_number(assembly.drawing_number)
    if not assembly_number:
        assembly_number = _generate_fallback_part_number("ASSY", 1) if doc_type == "bom" else _generate_fallback_part_number("PART", 1)
        warnings.append("Assembly/part number not found; generated a temporary number.")

    assembly_name = (assembly.name or assembly.description or assembly_number).strip()
    assembly_description = (assembly.description or assembly.name or "").strip()
    assembly_revision = (assembly.revision or "A").strip()
    assembly_drawing = _safe_part_number(assembly.drawing_number)
    assembly_part_type = (assembly.part_type or ("assembly" if doc_type == "bom" else "manufactured")).strip().lower()
    if assembly_part_type not in {p.value for p in PartType}:
        assembly_part_type = PartType.ASSEMBLY.value if doc_type == "bom" else PartType.MANUFACTURED.value

    existing_part = db.query(Part).filter(Part.part_number == assembly_number).first()
    if existing_part:
        assembly_part = existing_part
    else:
        assembly_part = Part(
            part_number=assembly_number,
            revision=assembly_revision,
            name=assembly_name,
            description=assembly_description,
            part_type=assembly_part_type,
            unit_of_measure=UnitOfMeasure.EACH.value,
            drawing_number=assembly_drawing,
            created_by=current_user.id
        )
        db.add(assembly_part)
        db.flush()

    created_parts = 0 if existing_part else 1
    created_bom_items = 0
    bom_id: Optional[int] = None

    if doc_type == "part" and not items:
        db.commit()
        return BOMImportResponse(
            document_type="part",
            assembly_part_id=assembly_part.id,
            assembly_part_number=assembly_part.part_number,
            bom_id=None,
            created_parts=created_parts,
            created_bom_items=0,
            extraction_confidence="medium",
            warnings=warnings
        )

    existing_bom = db.query(BOM).filter(BOM.part_id == assembly_part.id, BOM.is_active == True).first()
    if existing_bom:
        raise HTTPException(status_code=400, detail="A BOM already exists for this assembly part")

    bom = BOM(
        part_id=assembly_part.id,
        revision=assembly_revision or "A",
        description=assembly_description,
        status="draft",
        bom_type="standard",
        created_by=current_user.id
    )
    db.add(bom)
    db.flush()
    bom_id = bom.id

    next_line = 10
    for idx, item in enumerate(items, start=1):
        item_number = int(item.line_number or next_line)
        next_line = item_number + 10
        description = (item.description or "").strip()
        item_part_number = _safe_part_number(item.part_number)
        line_type = _classify_line_type(description, item.line_type)
        item_type = _coerce_item_type(item.item_type)
        part_type = _infer_part_type(line_type, item_type, description)
        uom = item.unit_of_measure

        part_name = description or item_part_number or f"Item {item_number}"
        if not item_part_number:
            warnings.append(f"Line {item_number}: missing part number; generated automatically.")

        component_part, missing, was_created = _ensure_part(
            db,
            item_part_number,
            part_name,
            description,
            part_type,
            None,
            uom,
            payload.create_missing_parts,
            idx
        )
        if missing:
            missing_parts.append(missing)
            continue
        if was_created:
            created_parts += 1

        quantity = float(item.quantity or 1)
        bom_item = BOMItem(
            bom_id=bom.id,
            component_part_id=component_part.id,
            item_number=item_number,
            quantity=quantity if quantity > 0 else 1.0,
            item_type=item_type,
            line_type=line_type,
            unit_of_measure=_normalize_uom(uom),
            reference_designator=item.reference_designator,
            find_number=item.find_number,
            notes=item.notes
        )
        db.add(bom_item)
        created_bom_items += 1

    if missing_parts:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Missing parts: {', '.join(missing_parts)}")

    db.commit()

    return BOMImportResponse(
        document_type="bom",
        assembly_part_id=assembly_part.id,
        assembly_part_number=assembly_part.part_number,
        bom_id=bom_id,
        created_parts=created_parts,
        created_bom_items=created_bom_items,
        extraction_confidence="medium",
        warnings=warnings
    )


@router.post("/import/preview", response_model=BOMImportPreviewResponse)
async def import_bom_preview(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Upload a BOM or single-part drawing (PDF/DOC/DOCX) and return a preview for review.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="File name is required")

    ext = f".{file.filename.split('.')[-1]}".lower() if "." in file.filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type. Use PDF or Word documents.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    doc_path = save_uploaded_document(content, file.filename)
    extraction_result = extract_text_from_document(doc_path)
    if not extraction_result.text or len(extraction_result.text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Could not extract text from document")

    extracted = extract_bom_data_with_llm(extraction_result.text, is_ocr=extraction_result.is_ocr)
    if extracted.get("_error"):
        raise HTTPException(status_code=400, detail=extracted.get("_error"))

    assembly, items, warnings, confidence = _build_preview(extracted)
    doc_type = (extracted.get("document_type") or ("bom" if items else "part")).lower()
    if items and doc_type != "bom":
        doc_type = "bom"

    return BOMImportPreviewResponse(
        document_type=doc_type,
        assembly=assembly,
        items=items,
        extraction_confidence=confidence,
        warnings=warnings
    )


@router.post("/import/commit", response_model=BOMImportResponse, status_code=status.HTTP_201_CREATED)
def import_bom_commit(
    payload: BOMImportCommitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Commit a reviewed BOM/part import payload.
    """
    return _create_from_import_payload(payload, db, current_user)


@router.post("/import", response_model=BOMImportResponse, status_code=status.HTTP_201_CREATED)
async def import_bom_or_part(
    file: UploadFile = File(...),
    create_missing_parts: bool = Form(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Upload a BOM or single-part drawing (PDF/DOC/DOCX) and create parts/BOM items.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="File name is required")

    ext = f".{file.filename.split('.')[-1]}".lower() if "." in file.filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type. Use PDF or Word documents.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    doc_path = save_uploaded_document(content, file.filename)
    extraction_result = extract_text_from_document(doc_path)
    if not extraction_result.text or len(extraction_result.text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Could not extract text from document")

    extracted = extract_bom_data_with_llm(extraction_result.text, is_ocr=extraction_result.is_ocr)
    if extracted.get("_error"):
        raise HTTPException(status_code=400, detail=extracted.get("_error"))

    items = extracted.get("items", []) or []
    assembly = extracted.get("assembly", {}) or {}
    doc_type = (extracted.get("document_type") or ("bom" if items else "part")).lower()
    if items and doc_type != "bom":
        doc_type = "bom"

    warnings: List[str] = []
    missing_parts: List[str] = []

    assembly_number = _safe_part_number(assembly.get("part_number")) or _safe_part_number(assembly.get("drawing_number"))
    if not assembly_number:
        assembly_number = _generate_fallback_part_number("ASSY", 1) if doc_type == "bom" else _generate_fallback_part_number("PART", 1)
        warnings.append("Assembly/part number not found; generated a temporary number.")

    assembly_name = (assembly.get("name") or assembly.get("description") or assembly_number).strip()
    assembly_description = (assembly.get("description") or assembly.get("name") or "").strip()
    assembly_revision = (assembly.get("revision") or "A").strip()
    assembly_drawing = _safe_part_number(assembly.get("drawing_number"))
    assembly_part_type = (assembly.get("part_type") or ("assembly" if doc_type == "bom" else "manufactured")).strip().lower()
    if assembly_part_type not in {p.value for p in PartType}:
        assembly_part_type = PartType.ASSEMBLY.value if doc_type == "bom" else PartType.MANUFACTURED.value

    # Get or create assembly part
    existing_part = db.query(Part).filter(Part.part_number == assembly_number).first()
    if existing_part:
        assembly_part = existing_part
    else:
        assembly_part = Part(
            part_number=assembly_number,
            revision=assembly_revision,
            name=assembly_name,
            description=assembly_description,
            part_type=assembly_part_type,
            unit_of_measure=UnitOfMeasure.EACH.value,
            drawing_number=assembly_drawing,
            created_by=current_user.id
        )
        db.add(assembly_part)
        db.flush()

    created_parts = 0 if existing_part else 1
    created_bom_items = 0
    bom_id: Optional[int] = None

    if doc_type == "part" and not items:
        db.commit()
        return BOMImportResponse(
            document_type="part",
            assembly_part_id=assembly_part.id,
            assembly_part_number=assembly_part.part_number,
            bom_id=None,
            created_parts=created_parts,
            created_bom_items=0,
            extraction_confidence=extracted.get("extraction_confidence", "low"),
            warnings=warnings
        )

    # If BOM already exists for assembly part, block import
    existing_bom = db.query(BOM).filter(BOM.part_id == assembly_part.id, BOM.is_active == True).first()
    if existing_bom:
        raise HTTPException(status_code=400, detail="A BOM already exists for this assembly part")

    bom = BOM(
        part_id=assembly_part.id,
        revision=assembly_revision or "A",
        description=assembly_description,
        status="draft",
        bom_type="standard",
        created_by=current_user.id
    )
    db.add(bom)
    db.flush()
    bom_id = bom.id

    next_line = 10
    for idx, item in enumerate(items, start=1):
        item_number = int(item.get("line_number") or next_line)
        next_line = item_number + 10
        description = (item.get("description") or "").strip()
        item_part_number = _safe_part_number(item.get("part_number"))
        line_type = _classify_line_type(description, item.get("line_type"))
        item_type = _coerce_item_type(item.get("item_type"))
        part_type = _infer_part_type(line_type, item_type, description)
        uom = item.get("unit_of_measure")

        part_name = description or item_part_number or f"Item {item_number}"
        if not item_part_number:
            warnings.append(f"Line {item_number}: missing part number; generated automatically.")

        component_part, missing, was_created = _ensure_part(
            db,
            item_part_number,
            part_name,
            description,
            part_type,
            None,
            uom,
            create_missing_parts,
            idx
        )
        if missing:
            missing_parts.append(missing)
            continue

        if was_created:
            created_parts += 1

        quantity = float(item.get("quantity") or 1)
        bom_item = BOMItem(
            bom_id=bom.id,
            component_part_id=component_part.id,
            item_number=item_number,
            quantity=quantity if quantity > 0 else 1.0,
            item_type=item_type,
            line_type=line_type,
            unit_of_measure=_normalize_uom(uom),
            reference_designator=item.get("reference_designator"),
            find_number=item.get("find_number"),
            notes=item.get("notes")
        )
        db.add(bom_item)
        created_bom_items += 1

    if missing_parts:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Missing parts: {', '.join(missing_parts)}")

    db.commit()

    return BOMImportResponse(
        document_type="bom",
        assembly_part_id=assembly_part.id,
        assembly_part_number=assembly_part.part_number,
        bom_id=bom_id,
        created_parts=created_parts,
        created_bom_items=created_bom_items,
        extraction_confidence=extracted.get("extraction_confidence", "low"),
        warnings=warnings
    )


@router.get("/", response_model=List[BOMResponse])
def list_boms(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all BOMs"""
    # Use selectinload to avoid N+1 queries for parts and items
    query = db.query(BOM).options(
        selectinload(BOM.part),
        selectinload(BOM.items).selectinload(BOMItem.component_part),
    )
    
    if active_only:
        query = query.filter(BOM.is_active == True)
    
    if status:
        query = query.filter(BOM.status == status)
    
    boms = query.offset(skip).limit(limit).all()

    # Preload BOM existence for component parts to avoid per-item queries
    component_ids = {
        item.component_part_id
        for bom in boms
        for item in (bom.items or [])
        if item.component_part_id
    }
    has_bom_by_part_id = {}
    if component_ids:
        existing_boms = db.query(BOM.part_id).filter(
            BOM.part_id.in_(component_ids),
            BOM.is_active == True
        ).all()
        has_bom_by_part_id = {row.part_id: True for row in existing_boms}
    
    result = []
    for bom in boms:
        try:
            # Part is already loaded via selectinload
            part = bom.part
            
            # Build part info safely
            part_info = None
            if part:
                part_info = PartInfo(
                    id=part.id,
                    part_number=part.part_number or "",
                    name=part.name or "",
                    revision=part.revision or "A",
                    part_type=part.part_type.value if part.part_type else "manufactured"
                )
            
            # Items are already loaded via selectinload
            items = bom.items or []
            items_list = []
            for item in items:
                try:
                    # Component part is already loaded via selectinload
                    component = item.component_part
                    
                    component_info = None
                    if component:
                        has_bom = has_bom_by_part_id.get(component.id, False)
                        component_info = ComponentPartInfo(
                            id=component.id,
                            part_number=component.part_number or "",
                            name=component.name or "",
                            revision=component.revision or "A",
                            part_type=component.part_type.value if component.part_type else "manufactured",
                            has_bom=has_bom
                        )
                    
                    items_list.append(BOMItemResponse(
                        id=item.id,
                        bom_id=item.bom_id,
                        component_part_id=item.component_part_id,
                        item_number=item.item_number if item.item_number is not None else 10,
                        quantity=item.quantity if item.quantity is not None else 1.0,
                        item_type=item.item_type if item.item_type else BOMItemType.MAKE,
                        line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
                        unit_of_measure=item.unit_of_measure or "each",
                        reference_designator=item.reference_designator,
                        find_number=item.find_number,
                        notes=item.notes,
                        torque_spec=item.torque_spec,
                        installation_notes=item.installation_notes,
                        work_center_id=item.work_center_id,
                        operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
                        scrap_factor=item.scrap_factor if item.scrap_factor is not None else 0.0,
                        lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
                        is_optional=item.is_optional if item.is_optional is not None else False,
                        is_alternate=item.is_alternate if item.is_alternate is not None else False,
                        alternate_group=item.alternate_group,
                        component_part=component_info,
                        created_at=item.created_at,
                        updated_at=item.updated_at
                    ))
                except Exception:
                    pass  # Skip items that fail
            
            bom_response = BOMResponse(
                id=bom.id,
                part_id=bom.part_id,
                revision=bom.revision or "A",
                description=bom.description or "",
                bom_type=bom.bom_type or "standard",
                status=bom.status or "draft",
                is_active=bom.is_active if bom.is_active is not None else True,
                effective_date=bom.effective_date,
                created_at=bom.created_at,
                updated_at=bom.updated_at,
                part=part_info,
                items=items_list
            )
            result.append(bom_response)
        except Exception:
            pass  # Skip BOMs that fail
    
    return result


@router.post("/", response_model=BOMResponse)
def create_bom(
    bom_in: BOMCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new BOM for a part"""
    # Check if part exists
    part = db.query(Part).filter(Part.id == bom_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Check if BOM already exists for this part
    existing = db.query(BOM).filter(BOM.part_id == bom_in.part_id, BOM.is_active == True).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Active BOM already exists for part {part.part_number}. Deactivate it first or update the existing BOM."
        )
    
    # Create BOM
    bom = BOM(
        part_id=bom_in.part_id,
        revision=bom_in.revision,
        description=bom_in.description,
        bom_type=bom_in.bom_type,
        created_by=current_user.id
    )
    db.add(bom)
    db.flush()
    
    # Add items
    for item_data in bom_in.items:
        # Validate component part exists
        component = db.query(Part).filter(Part.id == item_data.component_part_id).first()
        if not component:
            raise HTTPException(status_code=400, detail=f"Component part ID {item_data.component_part_id} not found")
        
        # Check for circular reference
        if item_data.component_part_id == bom_in.part_id:
            raise HTTPException(status_code=400, detail="BOM cannot contain itself as a component")
        
        item = BOMItem(
            bom_id=bom.id,
            **item_data.model_dump()
        )
        db.add(item)
    
    db.commit()
    db.refresh(bom)
    
    # Return with full response
    return get_bom(bom.id, db, current_user)


@router.get("/{bom_id}", response_model=BOMResponse)
def get_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific BOM with all items"""
    try:
        bom = db.query(BOM).options(
            joinedload(BOM.part),
            joinedload(BOM.items).joinedload(BOMItem.component_part)
        ).filter(BOM.id == bom_id).first()
        
        if not bom:
            raise HTTPException(status_code=404, detail="BOM not found")
        
        # Safely get part_type - handle both enum and string values
        part_type_val = "manufactured"
        if bom.part and bom.part.part_type:
            if hasattr(bom.part.part_type, 'value'):
                part_type_val = bom.part.part_type.value
            else:
                part_type_val = str(bom.part.part_type)
        
        component_ids = {item.component_part_id for item in bom.items if item.component_part_id}
        has_bom_by_part_id = {}
        if component_ids:
            existing_boms = db.query(BOM.part_id).filter(
                BOM.part_id.in_(component_ids),
                BOM.is_active == True
            ).all()
            has_bom_by_part_id = {row.part_id: True for row in existing_boms}

        return BOMResponse(
            id=bom.id,
            part_id=bom.part_id,
            revision=bom.revision,
            description=bom.description,
            bom_type=bom.bom_type or "standard",
            status=bom.status,
            is_active=bom.is_active,
            effective_date=bom.effective_date,
            created_at=bom.created_at,
            updated_at=bom.updated_at,
            part=PartInfo(
                id=bom.part.id,
                part_number=bom.part.part_number or "",
                name=bom.part.name or "",
                revision=bom.part.revision or "A",
                part_type=part_type_val
            ) if bom.part else None,
            items=[build_bom_item_response(item, db, has_bom_by_part_id) for item in bom.items]
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500, detail=f"Error getting BOM: {str(e)}\n{traceback.format_exc()}")


@router.get("/by-part/{part_id}", response_model=BOMResponse)
def get_bom_by_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the active BOM for a part"""
    bom = db.query(BOM).options(
        joinedload(BOM.part),
        joinedload(BOM.items).joinedload(BOMItem.component_part)
    ).filter(BOM.part_id == part_id, BOM.is_active == True).first()
    
    if not bom:
        raise HTTPException(status_code=404, detail="No active BOM found for this part")
    
    return get_bom(bom.id, db, current_user)


@router.put("/{bom_id}", response_model=BOMResponse)
def update_bom(
    bom_id: int,
    bom_in: BOMUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a BOM"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    update_data = bom_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(bom, field, value)
    
    db.commit()
    db.refresh(bom)
    return get_bom(bom.id, db, current_user)


@router.post("/{bom_id}/release")
def release_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Release a BOM for production use"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    if bom.status == "released":
        raise HTTPException(status_code=400, detail="BOM is already released")
    
    if not bom.items:
        raise HTTPException(status_code=400, detail="Cannot release BOM with no items")
    
    bom.status = "released"
    bom.approved_by = current_user.id
    bom.approved_at = datetime.utcnow()
    bom.effective_date = datetime.utcnow()
    
    db.commit()
    return {"message": "BOM released", "bom_id": bom.id}


@router.post("/{bom_id}/unrelease")
def unrelease_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Unrelease a BOM to allow editing"""
    try:
        bom = db.query(BOM).filter(BOM.id == bom_id).first()
        if not bom:
            raise HTTPException(status_code=404, detail="BOM not found")
        if bom.status != "released":
            raise HTTPException(status_code=400, detail="BOM is not released")
        bom.status = "draft"
        bom.approved_by = None
        bom.approved_at = None
        db.commit()
        return {"message": "BOM unreleased", "bom_id": bom.id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        raise HTTPException(status_code=500, detail=f"Error unreleasing BOM: {str(e)}\n{traceback.format_exc()}")


@router.delete("/{bom_id}")
def delete_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Delete a BOM (only draft BOMs can be deleted)"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    if bom.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft BOMs can be deleted")
    
    # Delete all items first
    db.query(BOMItem).filter(BOMItem.bom_id == bom_id).delete()
    
    db.delete(bom)
    db.commit()
    return {"message": "BOM deleted"}


# BOM Item operations
@router.post("/{bom_id}/items", response_model=BOMItemResponse)
def add_bom_item(
    bom_id: int,
    item_in: BOMItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Add an item to a BOM"""
    try:
        bom = db.query(BOM).filter(BOM.id == bom_id).first()
        if not bom:
            raise HTTPException(status_code=404, detail="BOM not found")
        
        # Validate component exists
        component = db.query(Part).filter(Part.id == item_in.component_part_id).first()
        if not component:
            raise HTTPException(status_code=404, detail="Component part not found")
        
        # Check for circular reference
        if item_in.component_part_id == bom.part_id:
            raise HTTPException(status_code=400, detail="BOM cannot contain itself")
        
        # Check for deeper circular references
        if would_create_circular_reference(db, bom.part_id, item_in.component_part_id):
            raise HTTPException(
                status_code=400, 
                detail="Adding this component would create a circular reference in the BOM structure"
            )
        
        # Inherit customer_name from parent assembly if component doesn't have one
        parent_part = db.query(Part).filter(Part.id == bom.part_id).first()
        if parent_part and parent_part.customer_name and not component.customer_name:
            component.customer_name = parent_part.customer_name
        
        # Get item data and ensure enum values are lowercase for PostgreSQL
        item_data = item_in.model_dump()
        
        # Convert item_type to lowercase string
        if 'item_type' in item_data and item_data['item_type']:
            val = item_data['item_type']
            if hasattr(val, 'value'):
                item_data['item_type'] = val.value.lower()
            elif isinstance(val, str):
                item_data['item_type'] = val.lower()
        
        # Convert line_type to lowercase string
        if 'line_type' in item_data and item_data['line_type']:
            val = item_data['line_type']
            if hasattr(val, 'value'):
                item_data['line_type'] = val.value.lower()
            elif isinstance(val, str):
                item_data['line_type'] = val.lower()
        
        item = BOMItem(bom_id=bom_id, **item_data)
        db.add(item)
        db.commit()
        db.refresh(item)
        
        # Build response manually to avoid joinedload issues
        component_info = None
        if component:
            has_bom = db.query(BOM).filter(BOM.part_id == component.id, BOM.is_active == True).first() is not None
            component_info = ComponentPartInfo(
                id=component.id,
                part_number=component.part_number or "",
                name=component.name or "",
                revision=component.revision or "A",
                part_type=component.part_type.value if component.part_type else "manufactured",
                has_bom=has_bom
            )
        
        return BOMItemResponse(
            id=item.id,
            bom_id=item.bom_id,
            component_part_id=item.component_part_id,
            item_number=item.item_number,
            quantity=item.quantity,
            item_type=item.item_type,
            line_type=item.line_type,
            unit_of_measure=item.unit_of_measure or "each",
            reference_designator=item.reference_designator,
            find_number=item.find_number,
            notes=item.notes,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes,
            work_center_id=item.work_center_id,
            operation_sequence=item.operation_sequence or 10,
            scrap_factor=item.scrap_factor or 0.0,
            lead_time_offset=item.lead_time_offset or 0,
            is_optional=item.is_optional or False,
            is_alternate=item.is_alternate or False,
            alternate_group=item.alternate_group,
            component_part=component_info,
            created_at=item.created_at,
            updated_at=item.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f"Error adding BOM item: {str(e)}\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=error_detail)


@router.put("/items/{item_id}", response_model=BOMItemResponse)
def update_bom_item(
    item_id: int,
    item_in: BOMItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a BOM item"""
    item = db.query(BOMItem).options(joinedload(BOMItem.component_part)).filter(BOMItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")
    
    update_data = item_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(item, field, value)
    
    db.commit()
    db.refresh(item)
    return build_bom_item_response(item, db)


@router.delete("/items/{item_id}")
def delete_bom_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Delete a BOM item"""
    item = db.query(BOMItem).filter(BOMItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")
    
    db.delete(item)
    db.commit()
    return {"message": "BOM item deleted"}


# Multi-level BOM operations
def would_create_circular_reference(db: Session, parent_part_id: int, component_part_id: int, visited: Set[int] = None) -> bool:
    """Check if adding component would create a circular reference"""
    if visited is None:
        visited = set()
    
    if component_part_id in visited:
        return True
    
    if component_part_id == parent_part_id:
        return True
    
    visited.add(component_part_id)
    
    # Get the component's BOM
    component_bom = db.query(BOM).filter(
        BOM.part_id == component_part_id, 
        BOM.is_active == True
    ).first()
    
    if not component_bom:
        return False
    
    # Check each child
    for item in component_bom.items:
        if would_create_circular_reference(db, parent_part_id, item.component_part_id, visited.copy()):
            return True
    
    return False


def explode_bom_recursive(
    db: Session, 
    bom_id: int, 
    parent_qty: float = 1.0, 
    level: int = 0, 
    max_levels: int = 20,
    visited: Set[int] = None
) -> List[BOMItemWithChildren]:
    """Recursively explode a BOM to get all levels"""
    if visited is None:
        visited = set()
    
    if level >= max_levels:
        return []
    
    bom = db.query(BOM).options(
        joinedload(BOM.items).joinedload(BOMItem.component_part)
    ).filter(BOM.id == bom_id).first()
    
    if not bom:
        return []
    
    result = []
    for item in bom.items:
        if item.component_part_id in visited:
            continue  # Skip to prevent infinite loops
        
        # Handle NULL values defensively
        qty = item.quantity or 1.0
        scrap = item.scrap_factor if item.scrap_factor is not None else 0.0
        extended_qty = qty * parent_qty * (1 + scrap)
        
        # Check if component has its own BOM
        component_bom = db.query(BOM).filter(
            BOM.part_id == item.component_part_id,
            BOM.is_active == True
        ).first()
        
        children = []
        item_type = item.item_type or BOMItemType.MAKE
        if component_bom and item_type != BOMItemType.BUY:
            new_visited = visited.copy()
            new_visited.add(item.component_part_id)
            children = explode_bom_recursive(
                db, 
                component_bom.id, 
                extended_qty, 
                level + 1, 
                max_levels,
                new_visited
            )
        
        item_response = BOMItemWithChildren(
            id=item.id,
            bom_id=item.bom_id,
            component_part_id=item.component_part_id,
            item_number=item.item_number,
            quantity=qty,
            item_type=item_type,
            line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
            unit_of_measure=item.unit_of_measure or "each",
            reference_designator=item.reference_designator,
            find_number=item.find_number,
            notes=item.notes,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes,
            work_center_id=item.work_center_id,
            operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
            scrap_factor=scrap,
            lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
            is_optional=item.is_optional or False,
            is_alternate=item.is_alternate or False,
            alternate_group=item.alternate_group,
            component_part=get_component_part_info(item.component_part, db) if item.component_part else None,
            created_at=item.created_at,
            updated_at=item.updated_at,
            children=children,
            level=level,
            extended_quantity=extended_qty
        )
        result.append(item_response)
    
    return result


def get_max_level(items: List[BOMItemWithChildren], current_max: int = 0) -> int:
    """Get the maximum nesting level in exploded BOM"""
    for item in items:
        current_max = max(current_max, item.level)
        if item.children:
            current_max = get_max_level(item.children, current_max)
    return current_max


@router.get("/{bom_id}/explode", response_model=BOMExploded)
def explode_bom(
    bom_id: int,
    max_levels: int = Query(default=10, le=20, description="Maximum levels to explode"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Explode a BOM to show all levels (multi-level BOM)"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    items = explode_bom_recursive(db, bom_id, 1.0, 0, max_levels)
    total_levels = get_max_level(items) + 1 if items else 0
    
    return BOMExploded(
        bom_id=bom.id,
        part_id=bom.part_id,
        part_number=bom.part.part_number,
        part_name=bom.part.name,
        revision=bom.revision,
        total_levels=total_levels,
        items=items
    )


def flatten_bom_items(
    items: List[BOMItemWithChildren], 
    flat_list: List[BOMFlatItem],
    parent_qty: float = 1.0
):
    """Flatten nested BOM items into a single list"""
    for item in items:
        flat_item = BOMFlatItem(
            level=item.level,
            item_number=item.item_number,
            find_number=item.find_number,
            part_id=item.component_part_id,
            part_number=item.component_part.part_number if item.component_part else "",
            part_name=item.component_part.name if item.component_part else "",
            part_type=item.component_part.part_type.value if item.component_part else "",
            item_type=item.item_type,
            line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
            quantity_per=item.quantity,
            extended_quantity=item.extended_quantity,
            unit_of_measure=item.unit_of_measure,
            scrap_factor=item.scrap_factor,
            lead_time_offset=item.lead_time_offset,
            is_optional=item.is_optional,
            is_alternate=item.is_alternate,
            has_children=len(item.children) > 0,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes
        )
        flat_list.append(flat_item)
        
        if item.children:
            flatten_bom_items(item.children, flat_list, item.extended_quantity)


@router.get("/{bom_id}/flatten", response_model=BOMFlattened)
def flatten_bom(
    bom_id: int,
    max_levels: int = Query(default=10, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a flattened view of a multi-level BOM (for reports/MRP)"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    exploded = explode_bom_recursive(db, bom_id, 1.0, 0, max_levels)
    
    flat_items: List[BOMFlatItem] = []
    flatten_bom_items(exploded, flat_items)
    
    unique_parts = set(item.part_id for item in flat_items)
    
    return BOMFlattened(
        bom_id=bom.id,
        part_number=bom.part.part_number,
        part_name=bom.part.name,
        revision=bom.revision,
        total_items=len(flat_items),
        total_unique_parts=len(unique_parts),
        items=flat_items
    )


@router.get("/{bom_id}/where-used")
def where_used(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Find all parent assemblies that use this BOM's part"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    # Find all BOM items that reference this part
    usages = db.query(BOMItem).options(
        joinedload(BOMItem.bom).joinedload(BOM.part)
    ).filter(BOMItem.component_part_id == bom.part_id).all()
    
    result = []
    for usage in usages:
        if usage.bom and usage.bom.part:
            result.append({
                "parent_part_id": usage.bom.part_id,
                "parent_part_number": usage.bom.part.part_number,
                "parent_part_name": usage.bom.part.name,
                "bom_id": usage.bom_id,
                "quantity_used": usage.quantity,
                "item_type": usage.item_type.value
            })
    
    return {
        "part_id": bom.part_id,
        "part_number": bom.part.part_number,
        "used_in": result
    }
